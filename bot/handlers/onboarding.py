"""
/start — first-time onboarding: timezone → currency → trip name.
Returning users (chat already has trips) get a welcome-back message.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import config
from bot.database import (
    add_trip_member,
    create_trip,
    get_db,
    get_trips_in_chat,
    set_user_timezone,
    upsert_group,
    upsert_user,
)
from bot.formatters import user_display_name
from bot.handlers.common import safe_edit, silent_answer

ONBOARD_TZ, ONBOARD_CURRENCY, ONBOARD_TRIP_NAME = range(3)

_KEY = "ob_ctx"

_POPULAR: list[tuple[str, str]] = [
    ("SGT  UTC+8",     "Asia/Singapore"),
    ("MYT  UTC+8",     "Asia/Kuala_Lumpur"),
    ("HKT  UTC+8",     "Asia/Hong_Kong"),
    ("CST  UTC+8",     "Asia/Shanghai"),
    ("JST  UTC+9",     "Asia/Tokyo"),
    ("KST  UTC+9",     "Asia/Seoul"),
    ("ICT  UTC+7",     "Asia/Bangkok"),
    ("WIB  UTC+7",     "Asia/Jakarta"),
    ("IST  UTC+5:30",  "Asia/Kolkata"),
    ("GST  UTC+4",     "Asia/Dubai"),
    ("UTC",            "UTC"),
    ("GMT/BST",        "Europe/London"),
    ("CET/CEST",       "Europe/Paris"),
    ("EST/EDT",        "America/New_York"),
    ("PST/PDT",        "America/Los_Angeles"),
    ("AEST/AEDT",      "Australia/Sydney"),
]


def _tz_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(_POPULAR), 2):
        row = [
            InlineKeyboardButton(label, callback_data=f"ob_tz_{iana}")
            for label, iana in _POPULAR[i:i+2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton("Skip →", callback_data="ob_skip_tz")])
    return InlineKeyboardMarkup(rows)


def _currency_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(c, callback_data=f"ob_cur_{c}") for c in config.SUPPORTED_CURRENCIES[i:i+4]]
        for i in range(0, len(config.SUPPORTED_CURRENCIES), 4)
    ]
    rows.append([InlineKeyboardButton("Skip (SGD)", callback_data="ob_skip_cur")])
    return InlineKeyboardMarkup(rows)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ob_cancel")]])


# ---------------------------------------------------------------------------
# Entry — /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    chat = update.effective_chat

    async with get_db() as db:
        await upsert_user(db, user.id, user.username, user.first_name, user.last_name)
        if chat.type in ("group", "supergroup"):
            await upsert_group(db, chat.id, chat.title or "Group")
        trips = await get_trips_in_chat(db, chat.id)

    if trips:
        active_id = context.chat_data.get("active_trip_id")
        active = next((t for t in trips if t["id"] == active_id), trips[0])
        await update.message.reply_text(
            f"👋 Welcome back!\n\nActive trip: *{active['name']}*\n/add to log an expense · /balances · /help",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    context.user_data[_KEY] = {
        "chat_id": chat.id,
        "creator_id": user.id,
        "tz": None,
        "tz_label": None,
        "currency": config.DEFAULT_CURRENCY,
        "bot_msg_id": None,
    }

    msg = await update.message.reply_text(
        "👋 *Welcome to SplitLah!*\n\n"
        "*Step 1 of 3 — Timezone*\n"
        "Pick your timezone so timestamps show in your local time:",
        parse_mode="Markdown",
        reply_markup=_tz_keyboard(),
    )
    context.user_data[_KEY]["bot_msg_id"] = msg.message_id
    return ONBOARD_TZ


# ---------------------------------------------------------------------------
# Step 1 — timezone
# ---------------------------------------------------------------------------

async def onboard_pick_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    iana = query.data[len("ob_tz_"):]
    ctx = context.user_data[_KEY]
    ctx["tz"] = iana
    ctx["tz_label"] = next((lbl for lbl, iname in _POPULAR if iname == iana), iana)

    async with get_db() as db:
        await set_user_timezone(db, update.effective_user.id, iana)

    now_str = datetime.now(ZoneInfo(iana)).strftime("%H:%M")

    await query.edit_message_text(
        f"👋 *Welcome to SplitLah!*\n\n"
        f"✅ Timezone: *{ctx['tz_label']}* ({now_str})\n\n"
        f"*Step 2 of 3 — Base currency*\n"
        f"Pick the main currency for your trip:",
        parse_mode="Markdown",
        reply_markup=_currency_keyboard(),
    )
    return ONBOARD_CURRENCY


async def onboard_skip_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        f"👋 *Welcome to SplitLah!*\n\n"
        f"*Step 2 of 3 — Base currency*\n"
        f"Pick the main currency for your trip:",
        parse_mode="Markdown",
        reply_markup=_currency_keyboard(),
    )
    return ONBOARD_CURRENCY


# ---------------------------------------------------------------------------
# Step 2 — currency
# ---------------------------------------------------------------------------

async def onboard_pick_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx = context.user_data[_KEY]
    ctx["currency"] = query.data[len("ob_cur_"):]
    return await _ask_trip_name(query, ctx)


async def onboard_skip_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _ask_trip_name(query, context.user_data[_KEY])


async def _ask_trip_name(query, ctx: dict) -> int:
    tz_line = f"✅ Timezone: {ctx['tz_label']}\n" if ctx.get("tz_label") else ""
    await query.edit_message_text(
        f"👋 *Welcome to SplitLah!*\n\n"
        f"{tz_line}"
        f"✅ Currency: *{ctx['currency']}*\n\n"
        f"*Step 3 of 3 — Your first trip*\n"
        f"Name your trip:\n"
        f"_(e.g. Bali 2025, House expenses, Weekend trip)_",
        parse_mode="Markdown",
        reply_markup=_cancel_kb(),
    )
    return ONBOARD_TRIP_NAME


# ---------------------------------------------------------------------------
# Step 3 — trip name
# ---------------------------------------------------------------------------

async def onboard_got_trip_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_KEY, {})
    name = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    bot_msg_id = ctx.get("bot_msg_id")

    if not name:
        new_id = await safe_edit(
            context, chat.id, bot_msg_id,
            "Name can't be empty — what would you like to call it?",
            reply_markup=_cancel_kb(),
        )
        ctx["bot_msg_id"] = new_id
        return ONBOARD_TRIP_NAME

    if len(name) > 100:
        new_id = await safe_edit(
            context, chat.id, bot_msg_id,
            "Name is too long (max 100 characters). Try again:",
            reply_markup=_cancel_kb(),
        )
        ctx["bot_msg_id"] = new_id
        return ONBOARD_TRIP_NAME

    currency = ctx.get("currency", config.DEFAULT_CURRENCY)
    creator_id = ctx.get("creator_id", update.effective_user.id)
    user = update.effective_user

    async with get_db() as db:
        trip_id = await create_trip(
            db,
            name=name,
            chat_id=chat.id,
            base_currency=currency,
            created_by=creator_id,
        )
        display = user_display_name({
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
        })
        await add_trip_member(db, trip_id, display_name=display, telegram_user_id=creator_id)

    context.chat_data["active_trip_id"] = trip_id
    context.user_data.pop(_KEY, None)

    tz_line = f"🕐 Timezone: {ctx['tz_label']}\n" if ctx.get("tz_label") else ""
    await safe_edit(
        context, chat.id, bot_msg_id,
        f"🎉 *All set!*\n\n"
        f"✅ Trip: *{name}*\n"
        f"💰 Currency: {currency}\n"
        f"{tz_line}\n"
        f"Here's what to do next:\n"
        f"• /trips to add more members\n"
        f"• /add to log your first expense\n"
        f"• /help for all commands",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

async def cancel_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_KEY, None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "Setup cancelled. Send /start anytime to try again."
        )
    else:
        await update.message.reply_text("Setup cancelled. Send /start anytime to try again.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Build handler
# ---------------------------------------------------------------------------

def build_start_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ONBOARD_TZ: [
                CallbackQueryHandler(onboard_pick_tz, pattern=r"^ob_tz_"),
                CallbackQueryHandler(onboard_skip_tz, pattern=r"^ob_skip_tz$"),
                CallbackQueryHandler(cancel_onboarding, pattern=r"^ob_cancel$"),
            ],
            ONBOARD_CURRENCY: [
                CallbackQueryHandler(onboard_pick_currency, pattern=r"^ob_cur_[A-Z]+$"),
                CallbackQueryHandler(onboard_skip_currency, pattern=r"^ob_skip_cur$"),
                CallbackQueryHandler(cancel_onboarding, pattern=r"^ob_cancel$"),
            ],
            ONBOARD_TRIP_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_got_trip_name),
                CallbackQueryHandler(cancel_onboarding, pattern=r"^ob_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_onboarding),
            CallbackQueryHandler(silent_answer),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
