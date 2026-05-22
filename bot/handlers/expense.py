"""
Guided expense-entry conversation — trip-centric, with ← Back at every step.

Flow
----
TRIP_SELECT   → pick trip (skipped if only one / active already set)
DESC          → description text
AMOUNT        → amount text
CURRENCY      → currency button (or "Other…")
CURRENCY_TXT  → typed currency code
PAYER         → who paid (trip member button)
SPLIT_MODE    → Equal / Ratio / Percentage / Exact
PARTICIPANTS  → multi-select trip members
SPLIT_VALS    → typed values (ratio / pct / exact only)
CONFIRM       → confirm or cancel

Back navigation
---------------
Each keyboard step has a ← Back button (callback: "exp_back").
The ConversationHandler routes the same pattern to a different handler
per state, so each Back press re-renders the previous step in-place.
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

from bot import config
from bot.currency import convert, is_currency_supported, resolve_alias
from bot.database import (
    create_expense,
    get_active_trip_id,
    get_db,
    get_trip,
    get_trip_currencies,
    get_trip_members,
    get_trips_in_chat,
    set_active_trip_id,
)
from bot.formatters import display_name, fmt_expense_summary, fmt_money
from bot.handlers.common import register_context, safe_edit, silent_answer
from bot.splits import calculate_shares, parse_split_values

logger = logging.getLogger(__name__)

(
    TRIP_SELECT,
    DESC,
    AMOUNT,
    CURRENCY,
    CURRENCY_TXT,
    PAYER,
    SPLIT_MODE,
    PARTICIPANTS,
    SPLIT_VALS,
    CONFIRM,
) = range(10)

_KEY = "expense_ctx"


def _k(chat_id: int) -> str:
    return f"{_KEY}_{chat_id}"


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def _back_cancel_row() -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("← Back", callback_data="exp_back"),
        InlineKeyboardButton("❌ Cancel", callback_data="exp_cancel"),
    ]


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="exp_cancel")]])


def _back_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([_back_cancel_row()])


def _currency_keyboard(recent: list[str] | None = None) -> InlineKeyboardMarkup:
    rows = []
    if recent:
        extra = [c for c in recent if c not in config.SUPPORTED_CURRENCIES]
        if extra:
            rows.append([InlineKeyboardButton(f"🕐 {c}", callback_data=f"cur_{c}") for c in extra[:4]])
    rows += [
        [InlineKeyboardButton(c, callback_data=f"cur_{c}") for c in config.SUPPORTED_CURRENCIES[i:i+4]]
        for i in range(0, len(config.SUPPORTED_CURRENCIES), 4)
    ]
    rows.append([InlineKeyboardButton("Other…", callback_data="cur_other")])
    rows.append(_back_cancel_row())
    return InlineKeyboardMarkup(rows)


def _members_keyboard(members: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(display_name(m), callback_data=f"pay_{m['id']}")] for m in members]
    rows.append(_back_cancel_row())
    return InlineKeyboardMarkup(rows)


def _split_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Equal", callback_data="sp_equal"),
            InlineKeyboardButton("Ratio", callback_data="sp_ratio"),
        ],
        [
            InlineKeyboardButton("Percentage", callback_data="sp_percentage"),
            InlineKeyboardButton("Exact", callback_data="sp_exact"),
        ],
        _back_cancel_row(),
    ])


def _participants_keyboard(members: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(members), 2):
        row = []
        for m in members[i:i+2]:
            tick = "✅ " if m["id"] in selected else ""
            row.append(InlineKeyboardButton(f"{tick}{display_name(m)}", callback_data=f"tog_{m['id']}"))
        rows.append(row)
    n = len(selected)
    rows.append([
        InlineKeyboardButton(f"✔ Done ({n} selected)" if n else "✔ Done", callback_data="pdone"),
    ])
    rows.append(_back_cancel_row())
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="conf_yes")],
        _back_cancel_row(),
    ])


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def _build_summary_text(ctx: dict) -> str:
    member_map = {m["id"]: m for m in ctx["members"]}
    payer = member_map[ctx["paid_by_member"]]
    share_rows = [{**member_map[mid], "share_amount": amt} for mid, amt in ctx["shares"].items()]
    expense_display = {
        "description": ctx["description"],
        "amount": ctx["amount"],
        "currency": ctx["currency"],
        "amount_base": ctx["amount_base"],
        "fx_rate": ctx["fx_rate"],
        "split_mode": ctx["split_mode"],
        "payer": payer,
    }
    return fmt_expense_summary(expense_display, share_rows, ctx["base_currency"]) + "\n\nLooks right?"


# ---------------------------------------------------------------------------
# Trip resolution helpers
# ---------------------------------------------------------------------------

async def _resolve_active_trip(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict | None:
    async with get_db() as db:
        active_id = await get_active_trip_id(db, chat_id)
        if active_id:
            trip = await get_trip(db, active_id)
            if trip and trip["chat_id"] == chat_id:
                return trip
    return None


def _init_ctx(context: ContextTypes.DEFAULT_TYPE, chat_id: int, trip: dict) -> dict:
    ctx: dict = {
        "trip_id": trip["id"],
        "trip_name": trip["name"],
        "base_currency": trip["base_currency"],
        "bot_msg_id": None,
        "members": [],
        "recent_currencies": [],
        "selected_participants": set(),
        "description": None,
        "amount": None,
        "currency": None,
        "amount_base": None,
        "fx_rate": 1.0,
        "paid_by_member": None,
        "split_mode": None,
        "participants": [],
        "shares": {},
    }
    context.user_data[_k(chat_id)] = ctx
    return ctx


# ---------------------------------------------------------------------------
# Entry — /add
# ---------------------------------------------------------------------------

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await register_context(update, context)
    chat = update.effective_chat

    old_ctx = context.user_data.get(_k(chat.id))
    if old_ctx and old_ctx.get("bot_msg_id"):
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id, message_id=old_ctx["bot_msg_id"], text="Cancelled."
            )
        except Exception:
            pass

    async with get_db() as db:
        trips = await get_trips_in_chat(db, chat.id)

    if not trips:
        await update.message.reply_text("No trips yet. Use /newtrip to create one.")
        return ConversationHandler.END

    active = await _resolve_active_trip(context, chat.id)
    if active:
        return await _start_expense_for_trip(update, context, active)
    if len(trips) == 1:
        async with get_db() as db:
            await set_active_trip_id(db, chat.id, trips[0]["id"])
        return await _start_expense_for_trip(update, context, trips[0])

    buttons = [
        [InlineKeyboardButton(t["name"], callback_data=f"seltrip_{t['id']}")]
        for t in trips
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="exp_cancel")])
    await update.message.reply_text(
        "Which trip is this expense for?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TRIP_SELECT


async def select_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    trip_id = int(query.data.split("_")[1])

    async with get_db() as db:
        await set_active_trip_id(db, update.effective_chat.id, trip_id)
        trip = await get_trip(db, trip_id)
        members = await get_trip_members(db, trip_id)
        recent = await get_trip_currencies(db, trip_id)

    if len(members) < 2:
        await query.edit_message_text(
            f"*{trip['name']}* needs at least 2 members. Use /trips to add some.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    ctx = _init_ctx(context, update.effective_chat.id, trip)
    ctx["bot_msg_id"] = query.message.message_id
    ctx["members"] = members
    ctx["recent_currencies"] = recent

    await query.edit_message_text(
        f"*{trip['name']}*\n\nWhat's this expense for?",
        parse_mode="Markdown",
        reply_markup=_cancel_kb(),
    )
    return DESC


async def _start_expense_for_trip(
    update: Update, context: ContextTypes.DEFAULT_TYPE, trip: dict
) -> int:
    chat = update.effective_chat
    async with get_db() as db:
        members = await get_trip_members(db, trip["id"])
        recent = await get_trip_currencies(db, trip["id"])

    if len(members) < 2:
        await update.message.reply_text(
            f"*{trip['name']}* needs at least 2 members. Use /trips to add some.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    ctx = _init_ctx(context, chat.id, trip)
    ctx["members"] = members
    ctx["recent_currencies"] = recent

    msg = await update.message.reply_text(
        f"*{trip['name']}*\n\nWhat's this expense for?",
        parse_mode="Markdown",
        reply_markup=_cancel_kb(),
    )
    ctx["bot_msg_id"] = msg.message_id
    return DESC


# ---------------------------------------------------------------------------
# Step 2 — description
# ---------------------------------------------------------------------------

async def got_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        await update.message.reply_text("Something went wrong. Use /add to start again.")
        return ConversationHandler.END
    desc = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    if len(desc) > 200:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"*{ctx['trip_name']}*\n\nDescription too long (max 200 chars). Try again:",
            parse_mode="Markdown",
            reply_markup=_cancel_kb(),
        )
        return DESC

    if not desc:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"*{ctx['trip_name']}*\n\nDescription can't be empty. Try again:",
            parse_mode="Markdown",
            reply_markup=_cancel_kb(),
        )
        return DESC

    ctx["description"] = desc
    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        f"_{desc}_\n\nAmount?",
        parse_mode="Markdown",
        reply_markup=_back_cancel_kb(),
    )
    return AMOUNT


# ---------------------------------------------------------------------------
# Step 3 — amount
# ---------------------------------------------------------------------------

async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    raw = update.message.text.strip().replace(",", "")
    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "Enter a positive number (e.g. `42.50`):",
            parse_mode="Markdown",
            reply_markup=_back_cancel_kb(),
        )
        return AMOUNT

    ctx["amount"] = amount
    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        f"*{amount:.2f}* — currency?",
        parse_mode="Markdown",
        reply_markup=_currency_keyboard(ctx.get("recent_currencies")),
    )
    return CURRENCY


# ---------------------------------------------------------------------------
# Step 4 — currency
# ---------------------------------------------------------------------------

async def got_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    token = query.data.split("_", 1)[1]

    if token == "other":
        await query.edit_message_text(
            "Currency code?  _(e.g. HKD, KRW, TWD)_",
            parse_mode="Markdown",
            reply_markup=_back_cancel_kb(),
        )
        return CURRENCY_TXT

    await query.edit_message_text("Checking rate…")
    return await _resolve_currency(context, chat.id, ctx, token)


async def got_currency_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    raw = update.message.text.strip().upper()
    try:
        await update.message.delete()
    except Exception:
        pass

    if not (2 <= len(raw) <= 5 and raw.isalpha()):
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "Invalid code — enter 2–5 letters  _(e.g. HKD, KRW)_:",
            parse_mode="Markdown",
            reply_markup=_back_cancel_kb(),
        )
        return CURRENCY_TXT

    currency = resolve_alias(raw)

    if not await is_currency_supported(currency):
        hint = f"  _(did you mean {currency}?)_" if currency != raw else ""
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"`{raw}` not recognised{hint} — try another:",
            parse_mode="Markdown",
            reply_markup=_back_cancel_kb(),
        )
        return CURRENCY_TXT

    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        "Checking rate…",
    )
    return await _resolve_currency(context, chat.id, ctx, currency)


async def _resolve_currency(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, ctx: dict, currency: str
) -> int:
    ctx["currency"] = currency
    note = ""
    if currency == ctx["base_currency"]:
        ctx["amount_base"] = ctx["amount"]
        ctx["fx_rate"] = 1.0
    else:
        try:
            amount_base, fx_rate = await convert(ctx["amount"], currency, ctx["base_currency"])
            ctx["amount_base"] = amount_base
            ctx["fx_rate"] = fx_rate
            note = (
                f"\n_{fmt_money(ctx['amount'], currency)} ≈ "
                f"{fmt_money(amount_base, ctx['base_currency'])}"
                f" (1 {currency} = {fx_rate:.4f} {ctx['base_currency']})_"
            )
        except Exception as exc:
            logger.warning("FX error for %s: %s", currency, exc)
            ctx["bot_msg_id"] = await safe_edit(
                context, chat_id, ctx["bot_msg_id"],
                f"Couldn't get a rate for *{currency}* — try another code:",
                parse_mode="Markdown",
                reply_markup=_back_cancel_kb(),
            )
            return CURRENCY_TXT

    ctx["bot_msg_id"] = await safe_edit(
        context, chat_id, ctx["bot_msg_id"],
        f"*{currency}*{note}\n\nWho paid?",
        parse_mode="Markdown",
        reply_markup=_members_keyboard(ctx["members"]),
    )
    return PAYER


# ---------------------------------------------------------------------------
# Step 5 — payer
# ---------------------------------------------------------------------------

async def got_payer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]

    ctx["paid_by_member"] = int(query.data.split("_")[1])
    member_map = {m["id"]: m for m in ctx["members"]}
    payer = member_map[ctx["paid_by_member"]]

    await query.edit_message_text(
        f"Paid by *{display_name(payer)}*\n\nHow to split?",
        parse_mode="Markdown",
        reply_markup=_split_mode_keyboard(),
    )
    return SPLIT_MODE


# ---------------------------------------------------------------------------
# Step 6 — split mode
# ---------------------------------------------------------------------------

async def got_split_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]

    ctx["split_mode"] = query.data.split("_", 1)[1]
    ctx["selected_participants"] = {m["id"] for m in ctx["members"]}

    labels = {
        "equal": "Equal split", "ratio": "Ratio split",
        "percentage": "Percentage split", "exact": "Exact amounts",
    }
    await query.edit_message_text(
        f"*{labels[ctx['split_mode']]}*\n\nWho's included?",
        parse_mode="Markdown",
        reply_markup=_participants_keyboard(ctx["members"], ctx["selected_participants"]),
    )
    return PARTICIPANTS


# ---------------------------------------------------------------------------
# Step 7 — participant multi-select
# ---------------------------------------------------------------------------

async def toggle_participant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]

    uid = int(query.data.split("_")[1])
    sel: set[int] = ctx["selected_participants"]
    sel.discard(uid) if uid in sel else sel.add(uid)

    await query.edit_message_reply_markup(
        reply_markup=_participants_keyboard(ctx["members"], sel)
    )
    return PARTICIPANTS


async def done_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]

    if not ctx["selected_participants"]:
        await query.answer("Select at least one person.", show_alert=True)
        return PARTICIPANTS

    participants = [m["id"] for m in ctx["members"] if m["id"] in ctx["selected_participants"]]
    ctx["participants"] = participants

    if ctx["split_mode"] == "equal":
        ctx["shares"] = calculate_shares(ctx["amount_base"], participants, "equal")
        await query.edit_message_text(
            _build_summary_text(ctx), parse_mode="Markdown", reply_markup=_confirm_keyboard()
        )
        return CONFIRM

    mode_prompts = {
        "ratio": "_Ratios, space-separated  (e.g._ `2 1 1`_)_",
        "percentage": "_Percentages, must total 100  (e.g._ `50 30 20`_)_",
        "exact": (
            f"_Exact amounts in {ctx['base_currency']} summing to "
            f"{fmt_money(ctx['amount_base'], ctx['base_currency'])}  (e.g._ `20.00 15.50`_)_"
        ),
    }
    member_map = {m["id"]: m for m in ctx["members"]}
    names = "  ·  ".join(display_name(member_map[mid]) for mid in participants)

    await query.edit_message_text(
        f"*{names}*\n\n{mode_prompts[ctx['split_mode']]}",
        parse_mode="Markdown",
        reply_markup=_back_cancel_kb(),
    )
    return SPLIT_VALS


# ---------------------------------------------------------------------------
# Step 8 — split values (ratio / percentage / exact)
# ---------------------------------------------------------------------------

async def got_split_values(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    raw_text = update.message.text
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        values = parse_split_values(raw_text, len(ctx["participants"]))
        ctx["shares"] = calculate_shares(ctx["amount_base"], ctx["participants"], ctx["split_mode"], values)
    except ValueError as exc:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"{exc} — try again:",
            reply_markup=_back_cancel_kb(),
        )
        return SPLIT_VALS

    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        _build_summary_text(ctx),
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(),
    )
    return CONFIRM


# ---------------------------------------------------------------------------
# Step 9 — confirm & save
# ---------------------------------------------------------------------------

async def confirm_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]

    async with get_db() as db:
        expense_id = await create_expense(
            db,
            trip_id=ctx["trip_id"],
            paid_by_member=ctx["paid_by_member"],
            description=ctx["description"],
            amount=ctx["amount"],
            currency=ctx["currency"],
            amount_base=ctx["amount_base"],
            base_currency=ctx["base_currency"],
            fx_rate=ctx["fx_rate"],
            split_mode=ctx["split_mode"],
            created_by=update.effective_user.id,
            shares=ctx["shares"],
        )

    member_map = {m["id"]: m for m in ctx["members"]}
    payer = member_map[ctx["paid_by_member"]]
    share_rows = [
        {**member_map[mid], "share_amount": amt}
        for mid, amt in ctx["shares"].items()
    ]
    expense_display = {
        "description": ctx["description"],
        "amount": ctx["amount"],
        "currency": ctx["currency"],
        "amount_base": ctx["amount_base"],
        "fx_rate": ctx["fx_rate"],
        "split_mode": ctx["split_mode"],
        "payer": payer,
    }
    summary = fmt_expense_summary(expense_display, share_rows, ctx["base_currency"])

    context.user_data.pop(_k(chat.id), None)
    await query.edit_message_text(
        f"✅ *Expense #{expense_id} saved*\n\n{summary}",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ← Back handlers (one per state they're triggered from)
# ---------------------------------------------------------------------------

async def _back_to_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from AMOUNT → re-show description prompt."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    curr = ctx.get("description") or ""
    hint = f"\n_currently: {curr}_" if curr else ""
    await query.edit_message_text(
        f"*{ctx['trip_name']}*{hint}\n\nWhat's this expense for?",
        parse_mode="Markdown",
        reply_markup=_cancel_kb(),
    )
    return DESC


async def _back_to_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from CURRENCY or CURRENCY_TXT → re-show amount prompt."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    curr = ctx.get("amount")
    hint = f" _({curr:.2f})_" if curr else ""
    await query.edit_message_text(
        f"_{ctx['description']}_{hint}\n\nAmount?",
        parse_mode="Markdown",
        reply_markup=_back_cancel_kb(),
    )
    return AMOUNT


async def _back_to_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from PAYER → re-show currency keyboard."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    await query.edit_message_text(
        f"*{ctx['amount']:.2f}* — currency?",
        parse_mode="Markdown",
        reply_markup=_currency_keyboard(ctx.get("recent_currencies")),
    )
    return CURRENCY


async def _back_to_payer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from SPLIT_MODE → re-show payer keyboard."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    currency = ctx.get("currency", "")
    note = ""
    if currency and currency != ctx["base_currency"] and ctx.get("amount_base"):
        note = (
            f"\n_{fmt_money(ctx['amount'], currency)} ≈ "
            f"{fmt_money(ctx['amount_base'], ctx['base_currency'])}"
            f" (1 {currency} = {ctx['fx_rate']:.4f} {ctx['base_currency']})_"
        )
    await query.edit_message_text(
        f"*{currency or '?'}*{note}\n\nWho paid?",
        parse_mode="Markdown",
        reply_markup=_members_keyboard(ctx["members"]),
    )
    return PAYER


async def _back_to_split_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from PARTICIPANTS → re-show split mode keyboard."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    member_map = {m["id"]: m for m in ctx["members"]}
    payer_id = ctx.get("paid_by_member")
    payer = member_map.get(payer_id, ctx["members"][0]) if payer_id else ctx["members"][0]
    await query.edit_message_text(
        f"Paid by *{display_name(payer)}*\n\nHow to split?",
        parse_mode="Markdown",
        reply_markup=_split_mode_keyboard(),
    )
    return SPLIT_MODE


async def _back_to_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from SPLIT_VALS or CONFIRM (equal) → re-show participant selector."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    labels = {
        "equal": "Equal split", "ratio": "Ratio split",
        "percentage": "Percentage split", "exact": "Exact amounts",
    }
    await query.edit_message_text(
        f"*{labels[ctx['split_mode']]}*\n\nWho's included?",
        parse_mode="Markdown",
        reply_markup=_participants_keyboard(ctx["members"], ctx["selected_participants"]),
    )
    return PARTICIPANTS


async def _back_to_split_vals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from CONFIRM (non-equal) → re-show split value prompt."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]

    mode_prompts = {
        "ratio": "_Ratios, space-separated  (e.g._ `2 1 1`_)_",
        "percentage": "_Percentages, must total 100  (e.g._ `50 30 20`_)_",
        "exact": (
            f"_Exact amounts in {ctx['base_currency']} summing to "
            f"{fmt_money(ctx['amount_base'], ctx['base_currency'])}  (e.g._ `20.00 15.50`_)_"
        ),
    }
    member_map = {m["id"]: m for m in ctx["members"]}
    names = "  ·  ".join(display_name(member_map[mid]) for mid in ctx["participants"])

    hint = ""
    if ctx.get("shares") and ctx["participants"]:
        vals = " ".join(
            str(round(ctx["shares"][mid], 2))
            for mid in ctx["participants"]
            if mid in ctx["shares"]
        )
        if vals:
            hint = f"  _(previously: {vals})_"

    await query.edit_message_text(
        f"*{names}*{hint}\n\n{mode_prompts[ctx['split_mode']]}",
        parse_mode="Markdown",
        reply_markup=_back_cancel_kb(),
    )
    return SPLIT_VALS


async def _back_from_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back from CONFIRM — goes to PARTICIPANTS (equal) or SPLIT_VALS (other)."""
    chat = update.effective_chat
    ctx = context.user_data[_k(chat.id)]
    if ctx.get("split_mode") == "equal":
        return await _back_to_participants(update, context)
    return await _back_to_split_vals(update, context)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

async def cancel_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.pop(_k(chat.id), None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.")
    else:
        bot_msg_id = ctx.get("bot_msg_id") if ctx else None
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


# ---------------------------------------------------------------------------
# Build handler
# ---------------------------------------------------------------------------

def build_expense_handler() -> ConversationHandler:
    back = CallbackQueryHandler  # alias for readability below

    return ConversationHandler(
        entry_points=[
            CommandHandler("add", cmd_add),
            CommandHandler("newexpense", cmd_add),
        ],
        states={
            TRIP_SELECT: [
                CallbackQueryHandler(select_trip, pattern=r"^seltrip_"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_description),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount),
                back(_back_to_desc, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            CURRENCY: [
                CallbackQueryHandler(got_currency, pattern=r"^cur_"),
                back(_back_to_amount, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            CURRENCY_TXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_currency_text),
                back(_back_to_amount, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            PAYER: [
                CallbackQueryHandler(got_payer, pattern=r"^pay_"),
                back(_back_to_currency, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            SPLIT_MODE: [
                CallbackQueryHandler(got_split_mode, pattern=r"^sp_"),
                back(_back_to_payer, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            PARTICIPANTS: [
                CallbackQueryHandler(toggle_participant, pattern=r"^tog_"),
                CallbackQueryHandler(done_participants, pattern=r"^pdone$"),
                back(_back_to_split_mode, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            SPLIT_VALS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_split_values),
                back(_back_to_participants, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_expense, pattern=r"^conf_yes$"),
                back(_back_from_confirm, pattern=r"^exp_back$"),
                CallbackQueryHandler(cancel_expense, pattern=r"^exp_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_expense),
            CommandHandler("add", cmd_add),
            CallbackQueryHandler(silent_answer),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
