"""
Expense detail/edit/delete — triggered by tapping an expense row in /history.

Flow
----
Entry: exp_act_{expense_id}_{trip_id}_{page}  (callback from history buttons)
EXP_ACT        → detail view: edit description | delete | back to history
EXP_EDIT_DESC  → text input for new description
EXP_CONFIRM_DEL → confirm deletion
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.database import (
    delete_expense,
    get_db,
    get_expense_by_id,
    get_expense_shares,
    get_trip,
    get_user_timezone,
    update_expense_description,
)
from bot.formatters import display_name, fmt_datetime, fmt_money, fmt_split_mode, resolve_tz
from bot.handlers.common import CONV_ENTRY_EXCL, safe_edit, silent_answer

logger = logging.getLogger(__name__)

EXP_ACT, EXP_EDIT_DESC, EXP_CONFIRM_DEL = range(3)


def _k(chat_id: int) -> str:
    return f"exp_act_ctx_{chat_id}"


def _action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit description", callback_data="expact_editdesc"),
            InlineKeyboardButton("🗑 Delete", callback_data="expact_delete"),
        ],
        [InlineKeyboardButton("← Back to history", callback_data="expact_back")],
    ])


def _back_to_detail_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="expact_back_to_detail")]])


def _build_detail_text(exp: dict, base_currency: str, shares: list[dict], tz=None) -> str:
    lines = [f"*#{exp['id']} {exp['description']}*", ""]
    if exp["currency"] != base_currency:
        lines.append(
            f"{fmt_money(exp['amount'], exp['currency'])}"
            f" _(≈ {fmt_money(exp['amount_base'], base_currency)})_"
        )
    else:
        lines.append(fmt_money(exp["amount_base"], base_currency))
    lines.append(f"💳 Paid by *{exp['payer_name']}* · {fmt_split_mode(exp['split_mode'])}")
    if shares:
        lines.append("")
        for s in shares:
            lines.append(f"  {display_name(s)} · {fmt_money(s['share_amount'], base_currency)}")
    lines += ["", f"_Added {fmt_datetime(exp['created_at'], tz=tz)}_"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry — exp_act_{expense_id}_{trip_id}_{page}
# ---------------------------------------------------------------------------

async def expense_action_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    # Answer immediately to dismiss the loading spinner before any DB work.
    try:
        await query.answer()
    except Exception:
        pass  # already answered by a ConversationHandler silent_answer fallback — safe to ignore

    chat = update.effective_chat
    user = update.effective_user
    logger.debug("expense_action_entry: data=%r user=%s chat=%s", query.data, user.id, chat.id)

    parts = query.data.split("_")
    expense_id, trip_id, page = int(parts[2]), int(parts[3]), int(parts[4])

    async with get_db() as db:
        exp = await get_expense_by_id(db, expense_id)
        trip = await get_trip(db, trip_id)
        shares = await get_expense_shares(db, expense_id) if exp else []
        tz = resolve_tz(await get_user_timezone(db, user.id))

    # Verify ownership: expense must belong to the requested trip, and trip must belong to this chat
    if not exp or not trip or exp["trip_id"] != trip_id or trip["chat_id"] != chat.id:
        try:
            await query.edit_message_text("Expense not found.")
        except Exception:
            pass
        return ConversationHandler.END

    context.user_data[_k(chat.id)] = {
        "expense_id": expense_id,
        "trip_id": trip_id,
        "page": page,
        "base_currency": trip["base_currency"],
        "bot_msg_id": query.message.message_id,
    }

    await query.edit_message_text(
        _build_detail_text(exp, trip["base_currency"], shares, tz=tz),
        parse_mode="Markdown",
        reply_markup=_action_keyboard(),
    )
    return EXP_ACT


# ---------------------------------------------------------------------------
# EXP_ACT handlers
# ---------------------------------------------------------------------------

async def expact_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the history page."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.pop(_k(chat.id), {})
    trip_id = ctx.get("trip_id")

    if not trip_id:
        await query.edit_message_text("Session expired. Use /history to browse expenses.")
        return ConversationHandler.END

    async with get_db() as db:
        trip = await get_trip(db, trip_id)

    if not trip:
        await query.edit_message_text("This trip no longer exists.")
        return ConversationHandler.END

    from bot.handlers.balance import _send_history_page
    await _send_history_page(update, context, trip, page=ctx.get("page", 0), edit=True)
    return ConversationHandler.END


async def expact_editdesc_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if not ctx:
        await query.answer("Session expired. Use /history to browse expenses.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    async with get_db() as db:
        exp = await get_expense_by_id(db, ctx["expense_id"])
    current = exp["description"] if exp else ""

    await query.edit_message_text(
        f"New description?\n_Currently: {current}_",
        parse_mode="Markdown",
        reply_markup=_back_to_detail_kb(),
    )
    return EXP_EDIT_DESC


async def expact_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if not ctx:
        await query.answer("Session expired. Use /history to browse expenses.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    async with get_db() as db:
        exp = await get_expense_by_id(db, ctx["expense_id"])
    desc = exp["description"] if exp else "this expense"

    await query.edit_message_text(
        f"Delete *#{ctx['expense_id']} {desc}*?\n\n_This can't be undone._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Delete", callback_data="expact_confirm_del")],
            [InlineKeyboardButton("← Back", callback_data="expact_back_to_detail")],
        ]),
    )
    return EXP_CONFIRM_DEL


# ---------------------------------------------------------------------------
# EXP_EDIT_DESC handlers
# ---------------------------------------------------------------------------

async def expact_got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    desc = update.message.text.strip()
    bot_msg_id = ctx.get("bot_msg_id") if ctx else None

    try:
        await update.message.delete()
    except Exception:
        pass

    if not ctx or not ctx.get("expense_id"):
        if bot_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat.id, message_id=bot_msg_id,
                    text="Session expired. Use /history to browse expenses."
                )
            except Exception:
                await update.message.reply_text("Session expired. Use /history to browse expenses.")
        else:
            await update.message.reply_text("Session expired. Use /history to browse expenses.")
        return ConversationHandler.END

    if not desc:
        async with get_db() as db:
            exp = await get_expense_by_id(db, ctx["expense_id"])
        current = exp["description"] if exp else ""
        new_id = await safe_edit(
            context, chat.id, bot_msg_id,
            f"New description?\n_Currently: {current}_",
            parse_mode="Markdown",
            reply_markup=_back_to_detail_kb(),
        )
        ctx["bot_msg_id"] = new_id
        return EXP_EDIT_DESC

    if len(desc) > 200:
        new_id = await safe_edit(
            context, chat.id, bot_msg_id,
            "Description is too long (max 200 characters). Try again:",
            reply_markup=_back_to_detail_kb(),
        )
        ctx["bot_msg_id"] = new_id
        return EXP_EDIT_DESC

    try:
        async with get_db() as db:
            await update_expense_description(db, ctx["expense_id"], desc)
            trip = await get_trip(db, ctx["trip_id"])
    except Exception as exc:
        logger.exception("expact_got_desc: DB error expense=%s: %s", ctx["expense_id"], exc)
        await update.message.reply_text("Something went wrong. Please try again.")
        return ConversationHandler.END

    context.user_data.pop(_k(chat.id), None)

    if not trip:
        if bot_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat.id, message_id=bot_msg_id, text="✅ Updated."
                )
            except Exception:
                await update.message.reply_text("✅ Updated.")
        else:
            await update.message.reply_text("✅ Updated.")
        return ConversationHandler.END

    from bot.handlers.balance import _send_history_page
    await _send_history_page(
        update, context, trip, page=ctx.get("page", 0), edit=False, edit_msg_id=bot_msg_id
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# EXP_CONFIRM_DEL handlers
# ---------------------------------------------------------------------------

async def expact_confirm_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.pop(_k(chat.id), {})
    expense_id = ctx.get("expense_id")
    trip_id = ctx.get("trip_id")

    if not expense_id:
        await query.edit_message_text("Session expired. Use /history to browse expenses.")
        return ConversationHandler.END

    try:
        async with get_db() as db:
            await delete_expense(db, expense_id)
            trip = await get_trip(db, trip_id)
    except Exception as exc:
        logger.exception("expact_confirm_del: DB error expense=%s: %s", expense_id, exc)
        await query.edit_message_text("Something went wrong. Please try again.")
        return ConversationHandler.END

    if not trip:
        await query.edit_message_text("Expense deleted.")
        return ConversationHandler.END

    from bot.handlers.balance import _send_history_page
    await _send_history_page(update, context, trip, page=ctx.get("page", 0), edit=True)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Shared: back to detail view
# ---------------------------------------------------------------------------

async def expact_back_to_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    user = update.effective_user
    ctx = context.user_data.get(_k(chat.id))

    if not ctx:
        await query.edit_message_text("Session expired. Use /history to browse expenses.")
        return ConversationHandler.END

    async with get_db() as db:
        exp = await get_expense_by_id(db, ctx["expense_id"])
        shares = await get_expense_shares(db, ctx["expense_id"]) if exp else []
        tz = resolve_tz(await get_user_timezone(db, user.id))

    if not exp:
        await query.answer("Expense no longer exists.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        _build_detail_text(exp, ctx["base_currency"], shares, tz=tz),
        parse_mode="Markdown",
        reply_markup=_action_keyboard(),
    )
    return EXP_ACT


async def expact_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    context.user_data.pop(_k(chat.id), None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Build handler
# ---------------------------------------------------------------------------

def build_expense_action_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(expense_action_entry, pattern=r"^exp_act_\d+_\d+_\d+$"),
        ],
        name="expense_action",
        persistent=True,
        states={
            EXP_ACT: [
                CallbackQueryHandler(expact_editdesc_entry, pattern=r"^expact_editdesc$"),
                CallbackQueryHandler(expact_delete, pattern=r"^expact_delete$"),
                CallbackQueryHandler(expact_back, pattern=r"^expact_back$"),
            ],
            EXP_EDIT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, expact_got_desc),
                CallbackQueryHandler(expact_back_to_detail, pattern=r"^expact_back_to_detail$"),
            ],
            EXP_CONFIRM_DEL: [
                CallbackQueryHandler(expact_confirm_del, pattern=r"^expact_confirm_del$"),
                CallbackQueryHandler(expact_back_to_detail, pattern=r"^expact_back_to_detail$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", expact_cancel),
            CallbackQueryHandler(silent_answer, pattern=CONV_ENTRY_EXCL),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
