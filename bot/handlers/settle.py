"""
/settle — record a payment.

All callbacks are standalone (registered in group=-1 in main.py) so they
can't be swallowed by other ConversationHandlers' silent_answer fallbacks.
Trip/member IDs are encoded directly in callback_data; no session state needed.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, CommandHandler, ContextTypes

from bot.database import (
    create_settlement,
    get_active_trip_id,
    get_db,
    get_member_expense_shares,
    get_net_balances,
    get_trip,
    get_trips_in_chat,
    set_active_trip_id,
)
from bot.debt import simplify_debts
from bot.formatters import display_name, fmt_money
from bot.handlers.common import register_context

logger = logging.getLogger(__name__)


def _transactions_keyboard(transactions, member_map, base_currency, trip_id) -> InlineKeyboardMarkup:
    buttons = []
    for from_id, to_id, amount in transactions:
        frm = display_name(member_map[from_id])
        to = display_name(member_map[to_id])
        buttons.append([InlineKeyboardButton(
            f"💰 {frm} → {to} · {fmt_money(amount, base_currency)}",
            callback_data=f"stl_{from_id}_{to_id}_{trip_id}",
        )])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="stlcnl")])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Entry command
# ---------------------------------------------------------------------------

async def cmd_settle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_context(update, context)
    chat = update.effective_chat

    trip = None
    async with get_db() as db:
        active_id = await get_active_trip_id(db, chat.id)
        if active_id:
            t = await get_trip(db, active_id)
            if t and t["chat_id"] == chat.id:
                trip = t

    if not trip:
        async with get_db() as db:
            trips = await get_trips_in_chat(db, chat.id)
        if not trips:
            await update.message.reply_text("No trips yet. Use /newtrip to create one.")
            return
        if len(trips) == 1:
            trip = trips[0]
            async with get_db() as db:
                await set_active_trip_id(db, chat.id, trip["id"])
        else:
            buttons = [[InlineKeyboardButton(t["name"], callback_data=f"sw_trip_{t['id']}")] for t in trips]
            await update.message.reply_text("Select a trip:", reply_markup=InlineKeyboardMarkup(buttons))
            return

    async with get_db() as db:
        balances = await get_net_balances(db, trip["id"])

    member_map = {r["id"]: r for r in balances}
    balance_map = {r["id"]: r["net"] for r in balances}
    transactions = simplify_debts(balance_map)

    if not transactions:
        await update.message.reply_text(
            f"*{trip['name']} · Settle up*\n\n_All settled up! 🎉_",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"*{trip['name']} · Settle up*\n\nPick a payment:",
        parse_mode="Markdown",
        reply_markup=_transactions_keyboard(transactions, member_map, trip["base_currency"], trip["id"]),
    )


# ---------------------------------------------------------------------------
# Standalone callbacks (registered in group=-1)
# ---------------------------------------------------------------------------

async def stl_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """stl_{from_id}_{to_id}_{trip_id} — show detail for a specific payment."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    from_id, to_id, trip_id = int(parts[1]), int(parts[2]), int(parts[3])

    async with get_db() as db:
        trip = await get_trip(db, trip_id)
        balances = await get_net_balances(db, trip_id) if trip else []
        expenses = await get_member_expense_shares(db, trip_id, from_id) if trip else []

    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.answer("Trip not found.", show_alert=True)
        raise ApplicationHandlerStop

    member_map = {r["id"]: r for r in balances}
    balance_map = {r["id"]: r["net"] for r in balances}
    transactions = simplify_debts(balance_map)
    amount = next((amt for f, t, amt in transactions if f == from_id and t == to_id), None)

    if amount is None:
        await query.answer("This debt no longer exists.", show_alert=True)
        raise ApplicationHandlerStop

    base_currency = trip["base_currency"]
    frm = display_name(member_map[from_id])
    to = display_name(member_map[to_id])

    lines = [f"*{frm} → {to}*", ""]
    expense_sum = 0.0
    if expenses:
        lines.append(f"_{frm}'s expense shares:_")
        for exp in expenses:
            lines.append(
                f"  • {exp['description']} — {fmt_money(exp['share_amount'], base_currency)}"
                f" _(paid by {exp['payer_name']})_"
            )
        expense_sum = sum(e["share_amount"] for e in expenses)
        lines.append("")

    offset = expense_sum - amount
    if offset > 0.005:
        lines += [
            f"Share total: {fmt_money(expense_sum, base_currency)}",
            f"Offset by others' debts: -{fmt_money(offset, base_currency)}",
            "",
            f"💸 *You owe: {fmt_money(amount, base_currency)}*",
        ]
    else:
        lines.append(f"💸 *You owe: {fmt_money(amount, base_currency)}*")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ Confirm {fmt_money(amount, base_currency)}",
                callback_data=f"sconf_{from_id}_{to_id}_{trip_id}",
            )],
            [InlineKeyboardButton("← Back", callback_data=f"stlback_{trip_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="stlcnl")],
        ]),
    )
    raise ApplicationHandlerStop


async def stl_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """sconf_{from_id}_{to_id}_{trip_id} — record the settlement."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    from_id, to_id, trip_id = int(parts[1]), int(parts[2]), int(parts[3])

    async with get_db() as db:
        trip = await get_trip(db, trip_id)
        balances = await get_net_balances(db, trip_id) if trip else []

    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.edit_message_text("Trip not found.")
        raise ApplicationHandlerStop

    member_map = {r["id"]: r for r in balances}
    balance_map = {r["id"]: r["net"] for r in balances}
    transactions = simplify_debts(balance_map)
    amount = next((amt for f, t, amt in transactions if f == from_id and t == to_id), None)

    if amount is None:
        await query.edit_message_text("This debt no longer exists — balances may have changed.")
        raise ApplicationHandlerStop

    frm = display_name(member_map[from_id])
    to = display_name(member_map[to_id])

    async with get_db() as db:
        await create_settlement(
            db,
            trip_id=trip_id,
            from_member_id=from_id,
            to_member_id=to_id,
            amount=amount,
            currency=trip["base_currency"],
        )

    await query.edit_message_text(
        f"✅ *{frm}* paid *{to}* {fmt_money(amount, trip['base_currency'])}.",
        parse_mode="Markdown",
    )
    raise ApplicationHandlerStop


async def stl_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """stlback_{trip_id} — return to payment list."""
    query = update.callback_query
    await query.answer()
    trip_id = int(query.data.split("_")[1])

    async with get_db() as db:
        trip = await get_trip(db, trip_id)
        balances = await get_net_balances(db, trip_id) if trip else []

    if not trip or trip["chat_id"] != update.effective_chat.id:
        await query.edit_message_text("Trip not found.")
        raise ApplicationHandlerStop

    member_map = {r["id"]: r for r in balances}
    balance_map = {r["id"]: r["net"] for r in balances}
    transactions = simplify_debts(balance_map)

    if not transactions:
        await query.edit_message_text(
            f"*{trip['name']} · Settle up*\n\n_All settled up! 🎉_",
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop

    await query.edit_message_text(
        f"*{trip['name']} · Settle up*\n\nPick a payment:",
        parse_mode="Markdown",
        reply_markup=_transactions_keyboard(transactions, member_map, trip["base_currency"], trip_id),
    )
    raise ApplicationHandlerStop


async def stl_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """stlcnl — cancel settle flow."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Cancelled.")
    raise ApplicationHandlerStop


def build_settle_handler() -> CommandHandler:
    return CommandHandler("settle", cmd_settle)
