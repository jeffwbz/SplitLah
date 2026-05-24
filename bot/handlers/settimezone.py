"""
/settimezone — let users set their personal display timezone.

Defaults to SGT (UTC+8) if not set.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.database import get_db, set_user_timezone
from bot.handlers.common import CONV_ENTRY_EXCL, cancel_all_flows, register_context, safe_edit, silent_answer

logger = logging.getLogger(__name__)

TZ_PICK, TZ_CUSTOM = range(2)


def _stz_k(chat_id: int) -> str:
    return f"stz_msg_id_{chat_id}"


# (button label, IANA name)
_POPULAR: list[tuple[str, str]] = [
    ("SGT UTC+8",    "Asia/Singapore"),
    ("MYT UTC+8",    "Asia/Kuala_Lumpur"),
    ("HKT UTC+8",    "Asia/Hong_Kong"),
    ("CST UTC+8",    "Asia/Shanghai"),
    ("JST UTC+9",    "Asia/Tokyo"),
    ("KST UTC+9",    "Asia/Seoul"),
    ("ICT UTC+7",    "Asia/Bangkok"),
    ("WIB UTC+7",    "Asia/Jakarta"),
    ("IST UTC+5:30", "Asia/Kolkata"),
    ("GST UTC+4",    "Asia/Dubai"),
    ("UTC",          "UTC"),
    ("GMT/BST",      "Europe/London"),
    ("CET/CEST",     "Europe/Paris"),
    ("EST/EDT",      "America/New_York"),
    ("PST/PDT",      "America/Los_Angeles"),
    ("AEST/AEDT",    "Australia/Sydney"),
]


def _tz_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(_POPULAR), 2):
        row = [
            InlineKeyboardButton(label, callback_data=f"tz_{iana}")
            for label, iana in _POPULAR[i:i + 2]
        ]
        rows.append(row)
    rows.append([
        InlineKeyboardButton("Other…", callback_data="stz_other"),
        InlineKeyboardButton("❌ Cancel", callback_data="stz_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _saved_text(iana: str) -> str:
    tz = ZoneInfo(iana)
    now = datetime.now(tz)
    return f"✅ Timezone set to *{iana}*\nLocal time: *{now.strftime('%H:%M')}*"


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

async def cmd_settimezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await register_context(update, context)
    chat = update.effective_chat
    user = update.effective_user
    logger.debug("cmd_settimezone: user=%s chat=%s", user.id, chat.id)

    await cancel_all_flows(context, chat.id, user_id=user.id)

    msg = await update.message.reply_text(
        "Choose your timezone:",
        reply_markup=_tz_keyboard(),
    )
    context.user_data[_stz_k(chat.id)] = msg.message_id
    return TZ_PICK


# ---------------------------------------------------------------------------
# TZ_PICK handlers
# ---------------------------------------------------------------------------

async def pick_popular_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    iana = query.data[3:]  # strip "tz_" prefix
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.debug("pick_popular_tz: user=%s tz=%s", user_id, iana)

    async with get_db() as db:
        await set_user_timezone(db, user_id, iana)

    context.user_data.pop(_stz_k(chat_id), None)
    await query.edit_message_text(_saved_text(iana), parse_mode="Markdown")
    return ConversationHandler.END


async def ask_custom_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data[_stz_k(update.effective_chat.id)] = query.message.message_id
    await query.edit_message_text(
        "Enter an IANA timezone name:\n"
        "_(e.g._ `Europe/Berlin`_,_ `America/Chicago`_,_ `Pacific/Auckland`_)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="stz_back"),
             InlineKeyboardButton("❌ Cancel", callback_data="stz_cancel")],
        ]),
    )
    return TZ_CUSTOM


# ---------------------------------------------------------------------------
# TZ_CUSTOM handlers
# ---------------------------------------------------------------------------

async def got_custom_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    user = update.effective_user
    tz_str = update.message.text.strip()
    msg_id = context.user_data.get(_stz_k(chat.id))

    try:
        await update.message.delete()
    except Exception:
        pass

    if msg_id is None:
        await update.message.reply_text("Session expired. Use /settimezone to try again.")
        return ConversationHandler.END

    try:
        ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        logger.debug("got_custom_tz: unrecognised tz=%r for user=%s", tz_str, user.id)
        new_id = await safe_edit(
            context, chat.id, msg_id,
            f"`{tz_str}` not recognised. Try again:\n"
            "_(e.g._ `Europe/Berlin`_,_ `America/Chicago`_,_ `Pacific/Auckland`_)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Back", callback_data="stz_back"),
                 InlineKeyboardButton("❌ Cancel", callback_data="stz_cancel")],
            ]),
        )
        context.user_data[_stz_k(chat.id)] = new_id
        return TZ_CUSTOM

    async with get_db() as db:
        await set_user_timezone(db, user.id, tz_str)

    context.user_data.pop(_stz_k(chat.id), None)
    await safe_edit(context, chat.id, msg_id, _saved_text(tz_str), parse_mode="Markdown")
    return ConversationHandler.END


async def back_to_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data[_stz_k(update.effective_chat.id)] = query.message.message_id
    await query.edit_message_text("Choose your timezone:", reply_markup=_tz_keyboard())
    return TZ_PICK


async def cancel_settimezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_stz_k(update.effective_chat.id), None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.")
    else:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Build handler
# ---------------------------------------------------------------------------

def build_settimezone_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("settimezone", cmd_settimezone)],
        name="settimezone",
        persistent=True,
        states={
            TZ_PICK: [
                CallbackQueryHandler(pick_popular_tz, pattern=r"^tz_"),
                CallbackQueryHandler(ask_custom_tz, pattern=r"^stz_other$"),
                CallbackQueryHandler(cancel_settimezone, pattern=r"^stz_cancel$"),
            ],
            TZ_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom_tz),
                CallbackQueryHandler(back_to_list, pattern=r"^stz_back$"),
                CallbackQueryHandler(cancel_settimezone, pattern=r"^stz_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_settimezone),
            CallbackQueryHandler(silent_answer, pattern=CONV_ENTRY_EXCL),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
