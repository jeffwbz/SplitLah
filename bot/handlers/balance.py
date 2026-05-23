"""
/balances, /simplify, /history, /currency handlers — all trip-scoped.
"""
from __future__ import annotations

import logging
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import config
from bot.currency import get_all_currencies, is_currency_supported, search_currencies
from bot.database import (
    count_expenses,
    get_active_trip_id,
    get_db,
    get_expense_history,
    get_member_expense_shares,
    get_net_balances,
    get_trip,
    get_trips_in_chat,
    set_active_trip_id,
    set_trip_currency,
)
from bot.debt import simplify_debts
from bot.formatters import fmt_balances, fmt_money, fmt_simplified
from bot.handlers.common import cancel_all_flows, register_context, safe_edit, silent_answer

logger = logging.getLogger(__name__)

CURRENCY_PICK, CURRENCY_CUSTOM, CURRENCY_SELECT = range(3)

PAGE_SIZE = 10


def _cur_k(chat_id: int) -> str:
    return f"cur_ctx_{chat_id}"


# ---------------------------------------------------------------------------
# Shared trip resolution
# ---------------------------------------------------------------------------

async def _get_active_trip(chat_id: int) -> dict | None:
    async with get_db() as db:
        active_id = await get_active_trip_id(db, chat_id)
        if active_id:
            trip = await get_trip(db, active_id)
            if trip and trip["chat_id"] == chat_id:
                return trip
    return None


async def _require_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Return the active trip or send an error / prompt. Returns None on failure."""
    chat = update.effective_chat

    trip = await _get_active_trip(chat.id)
    if trip:
        return trip

    async with get_db() as db:
        trips = await get_trips_in_chat(db, chat.id)

    if not trips:
        await update.message.reply_text("No trips yet. Use /newtrip to create one.")
        return None

    if len(trips) == 1:
        async with get_db() as db:
            await set_active_trip_id(db, chat.id, trips[0]["id"])
        return trips[0]

    # Multiple trips, none active — ask user to switch
    buttons = [
        [InlineKeyboardButton(t["name"], callback_data=f"sw_trip_{t['id']}")]
        for t in trips
    ]
    await update.message.reply_text("Select a trip:", reply_markup=InlineKeyboardMarkup(buttons))
    return None


# ---------------------------------------------------------------------------
# /balances
# ---------------------------------------------------------------------------

async def cmd_balances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_context(update, context)
    chat = update.effective_chat
    logger.debug("cmd_balances: user=%s chat=%s", update.effective_user.id, chat.id)

    trip = await _require_trip(update, context)
    if not trip:
        return

    async with get_db() as db:
        balances = await get_net_balances(db, trip["id"])

    await update.message.reply_text(
        fmt_balances(balances, trip["name"], trip["base_currency"]),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /simplify
# ---------------------------------------------------------------------------

async def _load_simplify_data(trip_id: int):
    """Fetch all data needed to render the /simplify view."""
    async with get_db() as db:
        trip = await get_trip(db, trip_id)
        if not trip:
            return None, None, None, None
        balances = await get_net_balances(db, trip_id)
        balance_map = {r["id"]: r["net"] for r in balances}
        member_map = {r["id"]: r for r in balances}
        transactions = simplify_debts(balance_map)
        breakdown: dict[int, list[dict]] = {}
        seen: set[int] = set()
        for debtor_id, _, _ in transactions:
            if debtor_id not in seen:
                seen.add(debtor_id)
                exps = await get_member_expense_shares(db, trip_id, debtor_id)
                if exps:
                    breakdown[debtor_id] = exps
    return trip, transactions, member_map, breakdown


def _build_simplify_buttons(
    trip_id: int,
    transactions: list,
    member_map: dict,
    breakdown: dict[int, list],
    expanded_debtors: set[int],
) -> InlineKeyboardMarkup | None:
    all_buttons: list[list[InlineKeyboardButton]] = []
    seen: set[int] = set()
    debtors_with_expenses = set(breakdown.keys())

    for debtor_id, _, _ in transactions:
        if debtor_id in seen:
            continue
        seen.add(debtor_id)
        debtor = member_map[debtor_id]
        if debtor_id in debtors_with_expenses and debtor_id not in expanded_debtors:
            all_buttons.append([InlineKeyboardButton(
                f"Show expenses · {debtor['display_name']}",
                callback_data=f"simp_more_{trip_id}_{debtor_id}",
            )])
        if debtor.get("telegram_user_id"):
            all_buttons.append([InlineKeyboardButton(
                f"👋 Nudge {debtor['display_name']}",
                callback_data=f"nudge_{debtor_id}_{trip_id}",
            )])

    if debtors_with_expenses:
        if debtors_with_expenses.issubset(expanded_debtors):
            all_buttons.append([InlineKeyboardButton(
                "▲ Collapse all",
                callback_data=f"simp_collapse_{trip_id}",
            )])
        else:
            all_buttons.append([InlineKeyboardButton(
                "▼ Expand all",
                callback_data=f"simp_expand_{trip_id}",
            )])

    return InlineKeyboardMarkup(all_buttons) if all_buttons else None


async def cmd_simplify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_context(update, context)
    chat = update.effective_chat
    logger.debug("cmd_simplify: user=%s chat=%s", update.effective_user.id, chat.id)

    trip = await _require_trip(update, context)
    if not trip:
        return

    async with get_db() as db:
        balances = await get_net_balances(db, trip["id"])
        balance_map = {r["id"]: r["net"] for r in balances}
        member_map = {r["id"]: r for r in balances}
        transactions = simplify_debts(balance_map)
        breakdown: dict[int, list[dict]] = {}
        seen: set[int] = set()
        for debtor_id, _, _ in transactions:
            if debtor_id not in seen:
                seen.add(debtor_id)
                exps = await get_member_expense_shares(db, trip["id"], debtor_id)
                if exps:
                    breakdown[debtor_id] = exps

    markup = _build_simplify_buttons(trip["id"], transactions, member_map, breakdown, set())
    await update.message.reply_text(
        fmt_simplified(transactions, member_map, trip["name"], trip["base_currency"]),
        parse_mode="Markdown",
        reply_markup=markup,
    )


async def simp_more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """simp_more_{trip_id}_{debtor_id} — expand one debtor's expense breakdown."""
    query = update.callback_query
    parts = query.data.split("_")
    trip_id, debtor_id = int(parts[2]), int(parts[3])
    logger.debug("simp_more_callback: trip=%s debtor=%s", trip_id, debtor_id)

    trip, transactions, member_map, breakdown = await _load_simplify_data(trip_id)
    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.answer("Trip not found.", show_alert=True)
        raise ApplicationHandlerStop
    if debtor_id not in member_map:
        await query.answer("Member not found.", show_alert=True)
        raise ApplicationHandlerStop

    await query.answer()
    expanded_debtors = {debtor_id}
    markup = _build_simplify_buttons(trip_id, transactions, member_map, breakdown, expanded_debtors)
    await query.edit_message_text(
        fmt_simplified(
            transactions, member_map, trip["name"], trip["base_currency"],
            breakdown=breakdown, expanded_debtors=expanded_debtors,
        ),
        parse_mode="Markdown",
        reply_markup=markup,
    )
    raise ApplicationHandlerStop


async def simp_expand_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """simp_expand_{trip_id} — expand all debtors."""
    query = update.callback_query
    trip_id = int(query.data.split("_")[2])
    logger.debug("simp_expand_all_callback: trip=%s", trip_id)

    trip, transactions, member_map, breakdown = await _load_simplify_data(trip_id)
    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.answer("Trip not found.", show_alert=True)
        raise ApplicationHandlerStop

    await query.answer()
    expanded_debtors = set(breakdown.keys())
    markup = _build_simplify_buttons(trip_id, transactions, member_map, breakdown, expanded_debtors)
    await query.edit_message_text(
        fmt_simplified(
            transactions, member_map, trip["name"], trip["base_currency"],
            breakdown=breakdown, expanded_debtors=expanded_debtors,
        ),
        parse_mode="Markdown",
        reply_markup=markup,
    )
    raise ApplicationHandlerStop


async def simp_collapse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """simp_collapse_{trip_id} — collapse all debtors."""
    query = update.callback_query
    trip_id = int(query.data.split("_")[2])
    logger.debug("simp_collapse_callback: trip=%s", trip_id)

    trip, transactions, member_map, breakdown = await _load_simplify_data(trip_id)
    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.answer("Trip not found.", show_alert=True)
        raise ApplicationHandlerStop

    await query.answer()
    markup = _build_simplify_buttons(trip_id, transactions, member_map, breakdown, set())
    await query.edit_message_text(
        fmt_simplified(transactions, member_map, trip["name"], trip["base_currency"]),
        parse_mode="Markdown",
        reply_markup=markup,
    )
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_context(update, context)
    chat = update.effective_chat
    logger.debug("cmd_history: user=%s chat=%s", update.effective_user.id, chat.id)

    trip = await _require_trip(update, context)
    if not trip:
        return
    await _send_history_page(update, context, trip, page=0, edit=False)


async def history_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """hist_{trip_id}_{page} — navigate to a different history page."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    trip_id, page = int(parts[1]), int(parts[2])
    logger.debug("history_page_callback: trip=%s page=%s", trip_id, page)

    async with get_db() as db:
        trip = await get_trip(db, trip_id)

    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.edit_message_text("This trip no longer exists.")
        raise ApplicationHandlerStop

    await _send_history_page(update, context, trip, page=page, edit=True)
    raise ApplicationHandlerStop


async def _send_history_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    trip: dict,
    page: int,
    edit: bool,
    edit_msg_id: int | None = None,
) -> None:
    async with get_db() as db:
        total = await count_expenses(db, trip["id"])
        expenses = await get_expense_history(db, trip["id"], limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    header = f"*{trip['name']} · History* _({page + 1}/{total_pages})_"
    if not expenses:
        header += "\n\n_No expenses yet._"

    rows: list[list[InlineKeyboardButton]] = []
    for exp in expenses:
        desc = exp["description"]
        short = desc[:26] + "…" if len(desc) > 26 else desc
        label = f"#{exp['id']} {short} — {fmt_money(exp['amount_base'], trip['base_currency'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"exp_act_{exp['id']}_{trip['id']}_{page}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Prev", callback_data=f"hist_{trip['id']}_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next →", callback_data=f"hist_{trip['id']}_{page + 1}"))
    if nav:
        rows.append(nav)

    markup = InlineKeyboardMarkup(rows) if rows else None

    if edit:
        await update.callback_query.edit_message_text(header, parse_mode="Markdown", reply_markup=markup)
    elif edit_msg_id:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=edit_msg_id,
            text=header,
            parse_mode="Markdown",
            reply_markup=markup,
        )
    else:
        await update.message.reply_text(header, parse_mode="Markdown", reply_markup=markup)


# ---------------------------------------------------------------------------
# Nudge
# ---------------------------------------------------------------------------

async def nudge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """nudge_{debtor_id}_{trip_id} — send a public nudge message in the chat."""
    import html as _html

    query = update.callback_query
    _, debtor_id_str, trip_id_str = query.data.split("_")
    debtor_id, trip_id = int(debtor_id_str), int(trip_id_str)
    logger.debug("nudge_callback: debtor=%s trip=%s", debtor_id, trip_id)

    async with get_db() as db:
        balances = await get_net_balances(db, trip_id)
        trip = await get_trip(db, trip_id)
        expense_shares = await get_member_expense_shares(db, trip_id, debtor_id)

    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.answer("Trip not found.", show_alert=True)
        raise ApplicationHandlerStop

    member_map = {r["id"]: r for r in balances}
    balance_map = {r["id"]: r["net"] for r in balances}
    debtor = member_map.get(debtor_id)

    if not debtor or not debtor.get("telegram_user_id"):
        await query.answer(
            "This member has no Telegram account linked — can't nudge.", show_alert=True
        )
        raise ApplicationHandlerStop

    transactions = simplify_debts(balance_map)
    total_owed = sum(amt for d, _, amt in transactions if d == debtor_id)

    if total_owed <= 0.005:
        await query.answer("This debt has already been settled.", show_alert=True)
        raise ApplicationHandlerStop

    await query.answer()

    uid = debtor["telegram_user_id"]
    username = debtor.get("username")
    if username:
        mention = f"@{_html.escape(username)}"
    else:
        mention = f'<a href="tg://user?id={uid}">{_html.escape(debtor["display_name"])}</a>'

    base_currency = trip["base_currency"]
    lines = [
        f"👋 {mention} — you owe <b>{_html.escape(fmt_money(total_owed, base_currency))}</b>"
        f" in <b>{_html.escape(trip['name'])}</b>",
    ]

    if expense_shares:
        lines.append("")
        _MAX = 15
        for exp in expense_shares[:_MAX]:
            share_str = _html.escape(fmt_money(exp["share_amount"], base_currency))
            desc = _html.escape(exp["description"])
            payer = _html.escape(exp["payer_name"])
            lines.append(f"  #{exp['id']} {desc} — {share_str} share <i>(paid by {payer})</i>")
        if len(expense_shares) > _MAX:
            lines.append(f"  <i>…and {len(expense_shares) - _MAX} more</i>")

    await query.message.reply_text("\n".join(lines), parse_mode="HTML")
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# /currency — change active trip's base currency
# ---------------------------------------------------------------------------

def _cur_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(c, callback_data=f"setcur_{trip_id}_{c}") for c in config.SUPPORTED_CURRENCIES[i:i + 4]]
        for i in range(0, len(config.SUPPORTED_CURRENCIES), 4)
    ]
    rows.append([
        InlineKeyboardButton("Other…", callback_data=f"setcur_{trip_id}_other"),
        InlineKeyboardButton("❌ Cancel", callback_data="cur_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


async def cmd_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await register_context(update, context)
    chat = update.effective_chat
    logger.debug("cmd_currency: user=%s chat=%s", update.effective_user.id, chat.id)

    await cancel_all_flows(context, chat.id)
    trip = await _require_trip(update, context)
    if not trip:
        return ConversationHandler.END

    msg = await update.message.reply_text(
        f"*{trip['name']}*\n\nBase currency?",
        parse_mode="Markdown",
        reply_markup=_cur_keyboard(trip["id"]),
    )
    context.user_data[_cur_k(chat.id)] = {
        "trip_id": trip["id"],
        "bot_msg_id": msg.message_id,
    }
    return CURRENCY_PICK


async def set_currency_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """setcur_{trip_id}_{currency} — set a common currency directly."""
    query = update.callback_query
    _, trip_id_str, currency = query.data.split("_", 2)
    trip_id = int(trip_id_str)
    chat = update.effective_chat

    async with get_db() as db:
        trip = await get_trip(db, trip_id)
        if not trip or trip["chat_id"] != chat.id:
            await query.answer("Trip not found.", show_alert=True)
            return ConversationHandler.END
        await set_trip_currency(db, trip_id, currency)

    context.user_data.pop(_cur_k(chat.id), None)
    await query.answer()
    await query.edit_message_text(
        f"✅ Base currency set to *{currency}*.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def ask_custom_base_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """setcur_{trip_id}_other — user wants to search for a currency."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    _, trip_id_str, _ = query.data.split("_", 2)

    ctx = context.user_data.setdefault(_cur_k(chat.id), {})
    ctx["trip_id"] = int(trip_id_str)
    ctx["bot_msg_id"] = query.message.message_id

    await query.edit_message_text(
        "Search by code or name:  _(e.g. taiwan, NTD, HKD)_",
        parse_mode="Markdown",
    )
    return CURRENCY_CUSTOM


async def got_custom_base_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    cur_ctx = context.user_data.get(_cur_k(chat.id), {})
    trip_id = cur_ctx.get("trip_id")
    bot_msg_id = cur_ctx.get("bot_msg_id")

    if not trip_id:
        await update.message.reply_text("Something went wrong. Use /currency to try again.")
        return ConversationHandler.END

    query_text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    results = await search_currencies(query_text)

    if not results:
        new_id = await safe_edit(
            context, chat.id, bot_msg_id,
            f"No results for *{query_text}*. Try again:",
            parse_mode="Markdown",
        )
        cur_ctx["bot_msg_id"] = new_id
        return CURRENCY_CUSTOM

    if len(results) == 1:
        code, name = results[0]
        return await _apply_base_currency(update, context, trip_id, code, name)

    rows = [
        [InlineKeyboardButton(f"{code} — {name}", callback_data=f"cursel_{trip_id}_{code}")]
        for code, name in results
    ]
    rows.append([
        InlineKeyboardButton("Search again", callback_data="cur_search_again"),
        InlineKeyboardButton("❌ Cancel", callback_data="cur_cancel"),
    ])
    new_id = await safe_edit(
        context, chat.id, bot_msg_id,
        f"{len(results)} results:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    cur_ctx["bot_msg_id"] = new_id
    return CURRENCY_SELECT


async def _apply_base_currency(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    trip_id: int, code: str, name: str,
) -> int:
    chat = update.effective_chat
    cur_ctx = context.user_data.pop(_cur_k(chat.id), {})
    bot_msg_id = cur_ctx.get("bot_msg_id")

    async with get_db() as db:
        trip = await get_trip(db, trip_id)
        if not trip or trip["chat_id"] != chat.id:
            msg = update.message or (update.callback_query.message if update.callback_query else None)
            if msg:
                await msg.reply_text("Trip not found.")
            return ConversationHandler.END
        await set_trip_currency(db, trip_id, code)

    text = f"✅ Base currency set to *{code}* ({name})."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    elif bot_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id, message_id=bot_msg_id, text=text, parse_mode="Markdown"
            )
        except Exception:
            await update.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END


async def select_searched_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """cursel_{trip_id}_{code} — user picked a currency from search results."""
    query = update.callback_query
    await query.answer()
    _, trip_id_str, code = query.data.split("_", 2)
    trip_id = int(trip_id_str)
    all_cur = await get_all_currencies()
    name = all_cur.get(code, code)
    return await _apply_base_currency(update, context, trip_id, code, name)


async def cur_search_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Search by code or name:  _(e.g. taiwan, NTD, HKD)_",
        parse_mode="Markdown",
    )
    return CURRENCY_CUSTOM


async def cancel_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    cur_ctx = context.user_data.pop(_cur_k(chat.id), None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.")
    else:
        bot_msg_id = cur_ctx.get("bot_msg_id") if cur_ctx else None
        if bot_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat.id, message_id=bot_msg_id, text="Cancelled."
                )
                try:
                    await update.message.delete()
                except Exception:
                    pass
            except Exception:
                await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_currency_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("currency", cmd_currency)],
        states={
            CURRENCY_PICK: [
                CallbackQueryHandler(set_currency_callback, pattern=r"^setcur_\d+_[A-Z]+$"),
                CallbackQueryHandler(ask_custom_base_currency, pattern=r"^setcur_\d+_other$"),
                CallbackQueryHandler(cancel_currency, pattern=r"^cur_cancel$"),
            ],
            CURRENCY_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom_base_currency),
                CallbackQueryHandler(cancel_currency, pattern=r"^cur_cancel$"),
            ],
            CURRENCY_SELECT: [
                CallbackQueryHandler(select_searched_currency, pattern=r"^cursel_\d+_[A-Z]+$"),
                CallbackQueryHandler(cur_search_again, pattern=r"^cur_search_again$"),
                CallbackQueryHandler(cancel_currency, pattern=r"^cur_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_currency),
            CallbackQueryHandler(silent_answer),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
