"""
Trip management: /newtrip, /trips, switch_trip, edit-trip flow.

/newtrip flow
-------------
TRIP_NAME           → user types a name
TRIP_CURRENCY       → user picks base currency (or searches)
TRIP_CURRENCY_SEARCH → user types a currency query
TRIP_CURRENCY_RESULTS → user picks from search results
TRIP_MEMBERS        → toggle Telegram group members + add virtual members
TRIP_ADD_NAME       → user types a virtual member's name
TRIP_CONFIRM        → confirm creation

Edit trip flow
--------------
EDIT_MENU           → rename | add member | remove member | clear history | delete trip
EDIT_NAME           → user types new trip name
EDIT_ADD_VNAME      → user types virtual member name
EDIT_REMOVE         → pick a member to remove
EDIT_CONFIRM_CLEAR  → confirm clearing all history
EDIT_CONFIRM_DELETE → confirm deleting the trip
"""
from __future__ import annotations

import logging

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
from bot.currency import search_currencies
from bot.database import (
    add_trip_member,
    clear_trip_expenses,
    create_trip,
    delete_trip_by_id,
    ensure_member,
    get_active_trip_id,
    get_db,
    get_group_telegram_members,
    get_member_expense_count,
    get_trip,
    get_trip_members,
    get_trips_in_chat,
    remove_trip_member,
    rename_trip,
    set_active_trip_id,
    upsert_group,
    upsert_user,
)
from bot.formatters import user_display_name
from bot.handlers.common import cancel_all_flows, register_context, safe_edit, silent_answer

logger = logging.getLogger(__name__)

TRIP_NAME, TRIP_CURRENCY, TRIP_MEMBERS, TRIP_ADD_NAME, TRIP_CONFIRM = range(5)
EDIT_MENU, EDIT_NAME, EDIT_ADD_VNAME, EDIT_REMOVE, EDIT_CONFIRM_CLEAR, EDIT_CONFIRM_DELETE = range(5, 11)
TRIP_CURRENCY_SEARCH, TRIP_CURRENCY_RESULTS = range(11, 13)


def _k(chat_id: int) -> str:
    return f"trip_ctx_{chat_id}"


def _ek(chat_id: int) -> str:
    return f"edit_trip_ctx_{chat_id}"


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _currency_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(c, callback_data=f"tcur_{c}") for c in config.SUPPORTED_CURRENCIES[i:i + 4]]
        for i in range(0, len(config.SUPPORTED_CURRENCIES), 4)
    ]
    rows.append([
        InlineKeyboardButton("Other…", callback_data="tcur_other"),
        InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _members_keyboard(
    telegram_members: list[dict],
    selected_telegram: set[int],
    virtual_members: list[str],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for i in range(0, len(telegram_members), 2):
        row = []
        for m in telegram_members[i:i + 2]:
            tick = "✅ " if m["id"] in selected_telegram else ""
            row.append(InlineKeyboardButton(f"{tick}{user_display_name(m)}", callback_data=f"ttog_{m['id']}"))
        rows.append(row)

    # Virtual members already added — display only (no toggle)
    for name in virtual_members:
        rows.append([InlineKeyboardButton(f"👤 {name}", callback_data="trip_noop")])

    n_total = len(selected_telegram) + len(virtual_members)
    add_label = "➕ Add non-group member" if telegram_members else "➕ Add member"
    rows.append([
        InlineKeyboardButton(add_label, callback_data="trip_addname"),
        InlineKeyboardButton(f"✔ Done ({n_total} selected)", callback_data="trip_members_done"),
    ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")])
    return InlineKeyboardMarkup(rows)


def _edit_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Rename", callback_data="emenu_rename"),
            InlineKeyboardButton("➕ Add member", callback_data="emenu_add"),
        ],
        [InlineKeyboardButton("🗑 Remove member", callback_data="emenu_remove")],
        [
            InlineKeyboardButton("🧹 Clear history", callback_data="emenu_clearhistory"),
            InlineKeyboardButton("❌ Delete trip", callback_data="emenu_deletetrip"),
        ],
        [InlineKeyboardButton("✅ Done", callback_data="emenu_done")],
    ])


# ---------------------------------------------------------------------------
# Entry — /newtrip
# ---------------------------------------------------------------------------

async def cmd_newtrip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await register_context(update, context)
    chat = update.effective_chat
    user = update.effective_user
    logger.debug("cmd_newtrip: user=%s chat=%s", user.id, chat.id)

    await cancel_all_flows(context, chat.id)

    telegram_members: list[dict] = []
    if chat.type in ("group", "supergroup"):
        async with get_db() as db:
            telegram_members = await get_group_telegram_members(db, chat.id)

    ctx: dict = {
        "chat_id": chat.id,
        "creator_id": user.id,
        "name": None,
        "base_currency": config.DEFAULT_CURRENCY,
        "bot_msg_id": None,
        "telegram_members": telegram_members,
        "selected_telegram": {m["id"] for m in telegram_members} | {user.id},
        "virtual_members": [],
    }
    context.user_data[_k(chat.id)] = ctx

    msg = await update.message.reply_text(
        "*New trip*\n\nName?  _(e.g. Bali 2025, House)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")]]),
    )
    ctx["bot_msg_id"] = msg.message_id
    return TRIP_NAME


# ---------------------------------------------------------------------------
# Step 1 — name
# ---------------------------------------------------------------------------

async def got_trip_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        return ConversationHandler.END

    name = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")]])

    if not name:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "*New trip*\n\nName can't be empty. Try again:",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        return TRIP_NAME

    if len(name) > 100:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "*New trip*\n\nName is too long (max 100 characters). Try again:",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        return TRIP_NAME

    # Reject names matching a group member's display name or first name.
    if chat.type in ("group", "supergroup"):
        async with get_db() as db:
            group_members = await get_group_telegram_members(db, chat.id)
        name_lower = name.lower()
        if any(
            name_lower in (user_display_name(m).lower(), (m.get("first_name") or "").lower())
            for m in group_members
        ):
            logger.warning("got_trip_name: rejected name=%r — matches a member name in chat=%s", name, chat.id)
            ctx["bot_msg_id"] = await safe_edit(
                context, chat.id, ctx["bot_msg_id"],
                "*New trip*\n\n⚠️ Trip names can't match a member's name. "
                "Try something like _Bali 2025_ or _House expenses_:",
                parse_mode="Markdown",
                reply_markup=cancel_kb,
            )
            return TRIP_NAME

    ctx["name"] = name
    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        f"*{name}*\n\nBase currency?",
        parse_mode="Markdown",
        reply_markup=_currency_keyboard(),
    )
    return TRIP_CURRENCY


# ---------------------------------------------------------------------------
# Step 2 — currency
# ---------------------------------------------------------------------------

async def got_trip_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    code = query.data.split("_", 1)[1]
    return await _apply_trip_currency(update, context, code)


async def got_trip_currency_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Search by code or name:  _(e.g. taiwan, NTD, HKD)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="tcur_back"),
             InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")],
        ]),
    )
    return TRIP_CURRENCY_SEARCH


async def trip_currency_search_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        await query.answer("Session expired. Use /newtrip to start again.", show_alert=True)
        return ConversationHandler.END
    await query.edit_message_text(
        f"*{ctx['name']}*\n\nBase currency?",
        parse_mode="Markdown",
        reply_markup=_currency_keyboard(),
    )
    return TRIP_CURRENCY


async def got_trip_currency_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        return ConversationHandler.END

    query_text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    results = await search_currencies(query_text)

    if not results:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"No results for *{query_text}*. Try again:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Back", callback_data="tcur_back"),
                 InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")],
            ]),
        )
        return TRIP_CURRENCY_SEARCH

    if len(results) == 1:
        code, _ = results[0]
        return await _apply_trip_currency(update, context, code)

    rows = [
        [InlineKeyboardButton(f"{code} — {name}", callback_data=f"tcursel_{code}")]
        for code, name in results
    ]
    rows.append([
        InlineKeyboardButton("Search again", callback_data="tcur_back_to_search"),
        InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel"),
    ])
    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        f"{len(results)} results:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return TRIP_CURRENCY_RESULTS


async def got_trip_currency_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, code = query.data.split("_", 1)  # tcursel_TWD
    return await _apply_trip_currency(update, context, code)


async def trip_currency_search_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Search by code or name:  _(e.g. taiwan, NTD, HKD)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="tcur_back"),
             InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")],
        ]),
    )
    return TRIP_CURRENCY_SEARCH


async def _apply_trip_currency(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        if update.callback_query:
            await update.callback_query.answer("Session expired. Use /newtrip to start again.", show_alert=True)
        else:
            await update.message.reply_text("Session expired. Use /newtrip to start again.")
        return ConversationHandler.END

    ctx["base_currency"] = code
    if ctx["telegram_members"]:
        text = (
            f"*{ctx['name']}* · {code}\n\n"
            "👥 Group members are all pre-selected — deselect if needed, "
            "or add someone not in the group:"
        )
    else:
        text = f"*{ctx['name']}* · {code}\n\nAdd members:"

    markup = _members_keyboard(ctx["telegram_members"], ctx["selected_telegram"], ctx["virtual_members"])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            text, parse_mode="Markdown", reply_markup=markup,
        )
    return TRIP_MEMBERS


# ---------------------------------------------------------------------------
# Step 3a — toggle a Telegram group member
# ---------------------------------------------------------------------------

async def toggle_telegram_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        await query.answer("Session expired. Use /newtrip to start again.", show_alert=True)
        return ConversationHandler.END

    uid = int(query.data.split("_")[1])
    if uid == ctx["creator_id"]:
        await query.answer("You're always included.", show_alert=True)
        return TRIP_MEMBERS

    selected: set[int] = ctx["selected_telegram"]
    if uid in selected:
        selected.discard(uid)
    else:
        selected.add(uid)

    await query.edit_message_reply_markup(
        reply_markup=_members_keyboard(ctx["telegram_members"], ctx["selected_telegram"], ctx["virtual_members"])
    )
    return TRIP_MEMBERS


# ---------------------------------------------------------------------------
# Step 3b — virtual member name input
# ---------------------------------------------------------------------------

async def ask_virtual_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Name?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")]]),
    )
    return TRIP_ADD_NAME


async def got_virtual_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        return ConversationHandler.END

    name = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel")]])

    if not name:
        ctx["bot_msg_id"] = await safe_edit(context, chat.id, ctx["bot_msg_id"], "Name?", reply_markup=cancel_kb)
        return TRIP_ADD_NAME

    if len(name) > 64:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "Name is too long (max 64 characters). Try again:", reply_markup=cancel_kb,
        )
        return TRIP_ADD_NAME

    try:
        float(name)
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "Please enter a name, not a number. Try again:", reply_markup=cancel_kb,
        )
        return TRIP_ADD_NAME
    except ValueError:
        pass

    ctx["virtual_members"].append(name)
    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        f"*{ctx['name']}* · {ctx['base_currency']}\n\nAdd members:",
        parse_mode="Markdown",
        reply_markup=_members_keyboard(ctx["telegram_members"], ctx["selected_telegram"], ctx["virtual_members"]),
    )
    return TRIP_MEMBERS


# ---------------------------------------------------------------------------
# Step 3c — done selecting members
# ---------------------------------------------------------------------------

async def done_trip_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        await query.answer("Session expired. Use /newtrip to start again.", show_alert=True)
        return ConversationHandler.END

    n_total = len(ctx["selected_telegram"]) + len(ctx["virtual_members"])
    if n_total < 1:
        await query.answer("Add at least one member.", show_alert=True)
        return TRIP_MEMBERS

    members_preview: list[str] = []
    for uid in ctx["selected_telegram"]:
        m = next((m for m in ctx["telegram_members"] if m["id"] == uid), None)
        if m:
            members_preview.append(user_display_name(m))
        elif uid == ctx["creator_id"]:
            members_preview.append("You")
    for name in ctx["virtual_members"]:
        members_preview.append(name)

    preview_text = ", ".join(members_preview)
    await query.edit_message_text(
        f"*{ctx['name']}* · {ctx['base_currency']}\n"
        f"{n_total} members: {preview_text}\n\n"
        "Create this trip?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Create", callback_data="trip_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="trip_cancel"),
            ]
        ]),
    )
    return TRIP_CONFIRM


# ---------------------------------------------------------------------------
# Step 4 — confirm & save
# ---------------------------------------------------------------------------

async def confirm_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_k(chat.id))
    if ctx is None:
        await query.answer("Session expired. Use /newtrip to start again.", show_alert=True)
        return ConversationHandler.END

    user = update.effective_user

    async with get_db() as db:
        trip_id = await create_trip(
            db,
            name=ctx["name"],
            chat_id=ctx["chat_id"],
            base_currency=ctx["base_currency"],
            created_by=ctx["creator_id"],
        )

        for uid in ctx["selected_telegram"]:
            telegram_user = next((m for m in ctx["telegram_members"] if m["id"] == uid), None)
            if telegram_user:
                dname = user_display_name(telegram_user)
            elif uid == ctx["creator_id"]:
                dname = user_display_name({
                    "id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                })
            else:
                dname = f"User#{uid}"
            await add_trip_member(db, trip_id, display_name=dname, telegram_user_id=uid)

        for name in ctx["virtual_members"]:
            await add_trip_member(db, trip_id, display_name=name, telegram_user_id=None)

    async with get_db() as db:
        await set_active_trip_id(db, ctx["chat_id"], trip_id)

    context.user_data.pop(_k(chat.id), None)
    await query.edit_message_text(
        f"✅ *{ctx['name']}* created. Use /add to log your first expense.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /trips — list and switch
# ---------------------------------------------------------------------------

async def cmd_trips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_context(update, context)
    chat = update.effective_chat
    await cancel_all_flows(context, chat.id)
    logger.debug("cmd_trips: user=%s chat=%s", update.effective_user.id, chat.id)

    async with get_db() as db:
        active_id = await get_active_trip_id(db, chat.id)
        trips = await get_trips_in_chat(db, chat.id)

    if not trips:
        await update.message.reply_text("No trips yet. Use /newtrip to create one.")
        return

    lines = ["*Trips*", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for t in trips:
        marker = "✅ " if t["id"] == active_id else ""
        prefix = "✅ " if t["id"] == active_id else "• "
        lines.append(f"{prefix}*{t['name']}* · {t['base_currency']}")
        buttons.append([
            InlineKeyboardButton(f"{marker}{t['name']}", callback_data=f"sw_trip_{t['id']}"),
            InlineKeyboardButton("✏️", callback_data=f"edit_trip_{t['id']}"),
        ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def switch_trip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """sw_trip_{trip_id} — make the given trip active."""
    query = update.callback_query
    trip_id = int(query.data.split("_")[2])
    chat_id = update.effective_chat.id
    logger.debug("switch_trip_callback: trip=%s chat=%s", trip_id, chat_id)

    async with get_db() as db:
        trip = await get_trip(db, trip_id)
        if not trip or trip["chat_id"] != chat_id:
            await query.answer("Trip not found.", show_alert=True)
            raise ApplicationHandlerStop
        await set_active_trip_id(db, chat_id, trip_id)

    await query.answer(f"Switched to {trip['name']}.")
    await query.edit_message_text(
        f"✅ *{trip['name']}* is now active.",
        parse_mode="Markdown",
    )
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Edit trip — entry
# ---------------------------------------------------------------------------

async def edit_trip_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # already answered by a ConversationHandler silent_answer fallback — safe to ignore
    chat = update.effective_chat
    trip_id = int(query.data.split("_")[2])
    logger.debug("edit_trip_entry: trip=%s chat=%s user=%s", trip_id, chat.id, update.effective_user.id)

    await cancel_all_flows(context, chat.id)

    async with get_db() as db:
        trip = await get_trip(db, trip_id)

    if not trip or trip["chat_id"] != chat.id:
        await query.edit_message_text("Trip not found.")
        return ConversationHandler.END

    context.user_data[_ek(chat.id)] = {
        "trip_id": trip_id,
        "trip_name": trip["name"],
        "bot_msg_id": query.message.message_id,
    }

    await query.edit_message_text(
        f"*{trip['name']}*",
        parse_mode="Markdown",
        reply_markup=_edit_menu_keyboard(),
    )
    return EDIT_MENU


# ---------------------------------------------------------------------------
# Edit menu — re-show helper
# ---------------------------------------------------------------------------

async def _show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, *, send: bool = False) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        if update.callback_query:
            await update.callback_query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    text = f"*{ctx['trip_name']}*"
    if send:
        # Called from a text-input handler — edit the stored bot message
        bot_msg_id = ctx.get("bot_msg_id")
        if bot_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat.id,
                    message_id=bot_msg_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=_edit_menu_keyboard(),
                )
                return EDIT_MENU
            except Exception as exc:
                logger.debug("_show_edit_menu: failed to edit msg=%s: %s", bot_msg_id, exc)
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_edit_menu_keyboard())
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=_edit_menu_keyboard()
        )
    return EDIT_MENU


# ---------------------------------------------------------------------------
# Edit menu actions
# ---------------------------------------------------------------------------

async def edit_menu_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        f"New name for *{ctx['trip_name']}*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")]]),
    )
    return EDIT_NAME


async def edit_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        return ConversationHandler.END

    name = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")]])

    if not name:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"New name for *{ctx['trip_name']}*:\n\n_Name can't be empty._",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        return EDIT_NAME

    if len(name) > 100:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"New name for *{ctx['trip_name']}*:\n\n_Name is too long (max 100 characters)._",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        return EDIT_NAME

    # Reject names matching any trip member's display name.
    async with get_db() as db:
        trip_members = await get_trip_members(db, ctx["trip_id"])
    if any(name.lower() == m["display_name"].lower() for m in trip_members):
        logger.warning("edit_got_name: rejected name=%r — matches a member name in trip=%s", name, ctx["trip_id"])
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            f"New name for *{ctx['trip_name']}*:\n\n"
            "⚠️ Trip names can't match a member's name. Try a different name:",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )
        return EDIT_NAME

    async with get_db() as db:
        await rename_trip(db, ctx["trip_id"], name)

    ctx["trip_name"] = name
    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        f"*{ctx['trip_name']}*",
        parse_mode="Markdown",
        reply_markup=_edit_menu_keyboard(),
    )
    return EDIT_MENU


async def edit_menu_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if context.user_data.get(_ek(update.effective_chat.id)) is None:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        "Enter their name:\n\n_For someone who isn't in this group chat._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="emenu_back"),
             InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")],
        ]),
    )
    return EDIT_ADD_VNAME


async def edit_got_vname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        return ConversationHandler.END

    name = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    back_cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("← Back", callback_data="emenu_back"),
         InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")],
    ])

    if not name:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "Member's name:\n\n_Name can't be empty._",
            parse_mode="Markdown", reply_markup=back_cancel_kb,
        )
        return EDIT_ADD_VNAME

    if len(name) > 64:
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "Member's name:\n\n_Name is too long (max 64 characters)._",
            parse_mode="Markdown", reply_markup=back_cancel_kb,
        )
        return EDIT_ADD_VNAME

    try:
        float(name)
        ctx["bot_msg_id"] = await safe_edit(
            context, chat.id, ctx["bot_msg_id"],
            "Please enter a name, not a number. Try again:",
            reply_markup=back_cancel_kb,
        )
        return EDIT_ADD_VNAME
    except ValueError:
        pass

    async with get_db() as db:
        await add_trip_member(db, ctx["trip_id"], display_name=name, telegram_user_id=None)

    ctx["bot_msg_id"] = await safe_edit(
        context, chat.id, ctx["bot_msg_id"],
        f"✅ *{name}* added!\n\nType another name, or tap Done:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done", callback_data="emenu_add_done")],
        ]),
    )
    return EDIT_ADD_VNAME


async def edit_add_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _show_edit_menu(update, context)


async def edit_menu_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    async with get_db() as db:
        members = await get_trip_members(db, ctx["trip_id"])
        expense_counts = {m["id"]: await get_member_expense_count(db, m["id"]) for m in members}

    rows: list[list[InlineKeyboardButton]] = []
    for m in members:
        count = expense_counts[m["id"]]
        if count > 0:
            rows.append([InlineKeyboardButton(
                f"🚫 {m['display_name']} (has expenses)",
                callback_data="edit_noop",
            )])
        else:
            rows.append([InlineKeyboardButton(
                f"🗑 {m['display_name']}",
                callback_data=f"edel_{m['id']}",
            )])
    rows.append([InlineKeyboardButton("← Back", callback_data="emenu_back")])

    await query.edit_message_text(
        f"Remove a member from *{ctx['trip_name']}*:\n"
        "_Members with expenses can't be removed._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return EDIT_REMOVE


async def edit_remove_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    member_id = int(query.data.split("_")[1])

    async with get_db() as db:
        members_before = await get_trip_members(db, ctx["trip_id"])
        if len(members_before) <= 1:
            await query.answer("Trips need at least one member.", show_alert=True)
            return EDIT_REMOVE
        success = await remove_trip_member(db, member_id)

    if not success:
        await query.answer("Can't remove members with expenses.", show_alert=True)
        return EDIT_REMOVE

    return await _show_edit_menu(update, context)


async def edit_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("Can't remove members with expenses.", show_alert=True)
    return EDIT_REMOVE


async def edit_menu_clearhistory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        f"Clear all expenses for *{ctx['trip_name']}*?\n\n"
        "_Members are kept. This can't be undone._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, clear history", callback_data="emenu_confirm_clear")],
            [InlineKeyboardButton("← Back", callback_data="emenu_back")],
        ]),
    )
    return EDIT_CONFIRM_CLEAR


async def edit_confirm_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.pop(_ek(chat.id), {})

    trip_id = ctx.get("trip_id")
    if not trip_id:
        await query.edit_message_text("Session expired.")
        return ConversationHandler.END

    async with get_db() as db:
        await clear_trip_expenses(db, trip_id)

    await query.edit_message_text(
        f"✅ *{ctx.get('trip_name', 'Trip')}* history cleared.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def edit_menu_deletetrip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.get(_ek(chat.id))
    if ctx is None:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        f"Delete *{ctx['trip_name']}*?\n\n"
        "_Expenses, members and settlements will all be deleted. This can't be undone._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Delete", callback_data="emenu_confirm_delete")],
            [InlineKeyboardButton("← Back", callback_data="emenu_back")],
        ]),
    )
    return EDIT_CONFIRM_DELETE


async def edit_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    ctx = context.user_data.pop(_ek(chat.id), {})

    trip_id = ctx.get("trip_id")
    if not trip_id:
        await query.edit_message_text("Session expired.")
        return ConversationHandler.END

    async with get_db() as db:
        await delete_trip_by_id(db, trip_id)
        # Clear active trip pointer if this was the active one
        current_active = await get_active_trip_id(db, chat.id)
        if current_active == trip_id:
            await set_active_trip_id(db, chat.id, None)

    await query.edit_message_text(
        f"✅ *{ctx.get('trip_name', 'Trip')}* deleted.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def edit_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop(_ek(update.effective_chat.id), None)
    await query.edit_message_text("Done.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Cancel helpers
# ---------------------------------------------------------------------------

async def cancel_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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


async def cancel_edit_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    ctx = context.user_data.pop(_ek(chat.id), None)
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


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return TRIP_MEMBERS


# ---------------------------------------------------------------------------
# Build handlers
# ---------------------------------------------------------------------------

def build_trip_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("newtrip", cmd_newtrip)],
        states={
            TRIP_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_trip_name),
                CallbackQueryHandler(cancel_trip, pattern=r"^trip_cancel$"),
            ],
            TRIP_CURRENCY: [
                CallbackQueryHandler(got_trip_currency_other, pattern=r"^tcur_other$"),
                CallbackQueryHandler(got_trip_currency, pattern=r"^tcur_[A-Z]+$"),
                CallbackQueryHandler(cancel_trip, pattern=r"^trip_cancel$"),
            ],
            TRIP_CURRENCY_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_trip_currency_search),
                CallbackQueryHandler(trip_currency_search_back, pattern=r"^tcur_back$"),
                CallbackQueryHandler(cancel_trip, pattern=r"^trip_cancel$"),
            ],
            TRIP_CURRENCY_RESULTS: [
                CallbackQueryHandler(got_trip_currency_select, pattern=r"^tcursel_"),
                CallbackQueryHandler(trip_currency_search_again, pattern=r"^tcur_back_to_search$"),
                CallbackQueryHandler(cancel_trip, pattern=r"^trip_cancel$"),
            ],
            TRIP_MEMBERS: [
                CallbackQueryHandler(toggle_telegram_member, pattern=r"^ttog_"),
                CallbackQueryHandler(ask_virtual_name, pattern=r"^trip_addname$"),
                CallbackQueryHandler(done_trip_members, pattern=r"^trip_members_done$"),
                CallbackQueryHandler(noop_callback, pattern=r"^trip_noop$"),
                CallbackQueryHandler(cancel_trip, pattern=r"^trip_cancel$"),
            ],
            TRIP_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_virtual_name),
                CallbackQueryHandler(cancel_trip, pattern=r"^trip_cancel$"),
            ],
            TRIP_CONFIRM: [
                CallbackQueryHandler(confirm_trip, pattern=r"^trip_confirm$"),
                CallbackQueryHandler(cancel_trip, pattern=r"^trip_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_trip),
            CommandHandler("newtrip", cmd_newtrip),
            CallbackQueryHandler(silent_answer),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


def build_edit_trip_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_trip_entry, pattern=r"^edit_trip_\d+$")],
        states={
            EDIT_MENU: [
                CallbackQueryHandler(edit_menu_rename, pattern=r"^emenu_rename$"),
                CallbackQueryHandler(edit_menu_add, pattern=r"^emenu_add$"),
                CallbackQueryHandler(edit_menu_remove, pattern=r"^emenu_remove$"),
                CallbackQueryHandler(edit_menu_clearhistory, pattern=r"^emenu_clearhistory$"),
                CallbackQueryHandler(edit_menu_deletetrip, pattern=r"^emenu_deletetrip$"),
                CallbackQueryHandler(edit_done, pattern=r"^emenu_done$"),
            ],
            EDIT_CONFIRM_CLEAR: [
                CallbackQueryHandler(edit_confirm_clear, pattern=r"^emenu_confirm_clear$"),
                CallbackQueryHandler(_show_edit_menu, pattern=r"^emenu_back$"),
            ],
            EDIT_CONFIRM_DELETE: [
                CallbackQueryHandler(edit_confirm_delete, pattern=r"^emenu_confirm_delete$"),
                CallbackQueryHandler(_show_edit_menu, pattern=r"^emenu_back$"),
            ],
            EDIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_got_name),
                CallbackQueryHandler(cancel_edit_trip, pattern=r"^edit_cancel$"),
            ],
            EDIT_ADD_VNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_got_vname),
                CallbackQueryHandler(edit_add_done, pattern=r"^emenu_add_done$"),
                CallbackQueryHandler(_show_edit_menu, pattern=r"^emenu_back$"),
                CallbackQueryHandler(cancel_edit_trip, pattern=r"^edit_cancel$"),
            ],
            EDIT_REMOVE: [
                CallbackQueryHandler(edit_remove_member, pattern=r"^edel_\d+$"),
                CallbackQueryHandler(edit_noop, pattern=r"^edit_noop$"),
                CallbackQueryHandler(_show_edit_menu, pattern=r"^emenu_back$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_edit_trip),
            CallbackQueryHandler(cancel_edit_trip, pattern=r"^edit_cancel$"),
            CallbackQueryHandler(silent_answer),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
