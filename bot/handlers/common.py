"""
Registration middleware and basic commands.
Works in both group chats and private chats.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.database import ensure_member, get_db, upsert_group, upsert_user

logger = logging.getLogger(__name__)


async def register_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upsert user (and group membership if in a group) on every interaction."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or user.is_bot:
        return

    async with get_db() as db:
        await upsert_user(db, user.id, user.username, user.first_name, user.last_name)
        if chat and chat.type in ("group", "supergroup"):
            await upsert_group(db, chat.id, chat.title or "Group")
            await ensure_member(db, chat.id, user.id)


_HELP_TEXT = (
    "*SplitLah*\n"
    "\n"
    "*Trips*\n"
    "/newtrip — Create a trip\n"
    "/trips — Manage trips\n"
    "\n"
    "*Expenses*\n"
    "/add — Log an expense\n"
    "/history — Browse & edit expenses\n"
    "\n"
    "*Balances*\n"
    "/balances — Net balance per member\n"
    "/simplify — Minimise payments to settle all debts\n"
    "/settle — Record a payment\n"
    "\n"
    "*Other*\n"
    "/currency — Change base currency\n"
    "/settimezone — Set your timezone\n"
    "/help — Show all commands"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_context(update, context)
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


async def cancel_all_flows(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Pop and dismiss every active text-input flow for this (user, chat)."""
    for key in [
        f"trip_ctx_{chat_id}",
        f"edit_trip_ctx_{chat_id}",
        f"expense_ctx_{chat_id}",
        f"exp_act_ctx_{chat_id}",
        f"cur_ctx_{chat_id}",
    ]:
        old = context.user_data.pop(key, None)
        if old and isinstance(old, dict) and old.get("bot_msg_id"):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=old["bot_msg_id"], text="Cancelled."
                )
            except Exception:
                pass
    # settimezone stores just a message_id int
    stz_msg_id = context.user_data.pop(f"stz_msg_id_{chat_id}", None)
    if stz_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=stz_msg_id, text="Cancelled."
            )
        except Exception:
            pass


async def stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("This menu has expired — start a new command.", show_alert=True)


async def silent_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dismiss a callback query silently (no alert) while a conversation is active."""
    if update.callback_query:
        await update.callback_query.answer()


async def safe_edit(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int | None,
    text: str,
    parse_mode: str | None = None,
    reply_markup=None,
) -> int:
    """Edit a message in place; fall back to send_message if it's unreachable.

    Swallows 'Message is not modified' silently.
    Returns the message_id that now displays the content.
    """
    if message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return message_id
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return message_id
        except Exception:
            pass
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )
    return msg.message_id
