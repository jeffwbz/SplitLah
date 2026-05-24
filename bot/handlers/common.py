"""
Registration middleware, flow cancellation, and basic commands.
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


# Callbacks that are ConversationHandler entry points — must NOT be swallowed by
# silent_answer fallbacks when another flow is active, so they can interrupt and
# start their own flow (which calls cancel_all_flows to clean up the old one).
CONV_ENTRY_EXCL = r"^(?!edit_trip_\d|exp_act_\d)"


async def cancel_all_flows(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int | None = None,
) -> None:
    """Cancel every active text-input flow for this (user, chat). Called at every command entry.

    Passing user_id also clears each ConversationHandler's internal _conversations state,
    preventing stale state from consuming the next text message the user sends.
    """
    # Keys that store a dict with a bot_msg_id field
    dict_keys = [
        f"ob_ctx_{chat_id}",
        f"trip_ctx_{chat_id}",
        f"edit_trip_ctx_{chat_id}",
        f"expense_ctx_{chat_id}",
        f"exp_act_ctx_{chat_id}",
        f"cur_ctx_{chat_id}",
    ]
    for key in dict_keys:
        old = context.user_data.pop(key, None)
        if old and isinstance(old, dict):
            msg_id = old.get("bot_msg_id")
            if msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id, text="⚠️ Previous action cancelled."
                    )
                except Exception:
                    pass  # message may be deleted or too old — not important

    # settimezone stores just a message_id int
    stz_id = context.user_data.pop(f"stz_msg_id_{chat_id}", None)
    if stz_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=stz_id, text="⚠️ Previous action cancelled."
            )
        except Exception:
            pass

    # Clear each ConversationHandler's internal state for this (user, chat) key.
    # Without this, a stale handler stays in e.g. TRIP_ADD_NAME and silently consumes
    # the next text message the user sends, even though user_data was already wiped.
    if user_id is not None:
        conv_key = (user_id, chat_id)
        for handler in context.bot_data.get("conv_handlers", []):
            try:
                handler._conversations.pop(conv_key, None)
                # Also remove from persistence so cleared state isn't reloaded on restart.
                persistence = getattr(context.application, "persistence", None)
                handler_name = getattr(handler, "name", None)
                if persistence is not None and handler_name:
                    await persistence.update_conversation(handler_name, conv_key, None)
            except Exception:
                pass


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
            logger.debug("safe_edit: BadRequest editing msg=%s in chat=%s: %s", message_id, chat_id, exc)
        except Exception as exc:
            logger.debug("safe_edit: failed to edit msg=%s in chat=%s: %s", message_id, chat_id, exc)

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )
    return msg.message_id


async def silent_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dismiss a callback query silently (no alert) while a conversation is active."""
    if update.callback_query:
        await update.callback_query.answer()


async def stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for callback queries that no handler claimed — show an expiry notice."""
    query = update.callback_query
    await query.answer("This menu has expired — start a new command.", show_alert=True)


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
    logger.debug("cmd_help: user=%s chat=%s", update.effective_user.id, update.effective_chat.id)
    await register_context(update, context)
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any active flow and send a simple confirmation."""
    chat = update.effective_chat
    user = update.effective_user
    logger.debug("cmd_cancel: user=%s chat=%s", user.id if user else None, chat.id)
    await cancel_all_flows(context, chat.id, user_id=user.id if user else None)
    await update.message.reply_text("Cancelled.")
