"""
SplitLah — Telegram expense-splitting bot.
"""
from __future__ import annotations

import logging
import sys

# Use the OS certificate store so SSL works out of the box on Windows.
if sys.platform == "win32":
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass

import telegram
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    PersistenceInput,
    filters,
)

from bot.config import BOT_TOKEN
from bot.database import init_db
from bot.handlers.balance import (
    build_currency_handler,
    cmd_balances,
    cmd_history,
    cmd_simplify,
    history_page_callback,
    nudge_callback,
    simp_collapse_callback,
    simp_expand_all_callback,
    simp_more_callback,
)
from bot.handlers.common import cmd_cancel, cmd_help, register_context, stale_callback
from bot.handlers.expense import build_expense_handler
from bot.handlers.expense_actions import build_expense_action_handler
from bot.handlers.onboarding import build_start_handler, ob_done_callback
from bot.handlers.settle import (
    build_settle_handler,
    stl_back_callback,
    stl_cancel_callback,
    stl_confirm_callback,
    stl_pick_callback,
)
from bot.handlers.settimezone import build_settimezone_handler
from bot.handlers.trip import build_edit_trip_handler, build_trip_handler, cmd_trips, switch_trip_callback

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    logger.info(
        "Starting SplitLah — Python %s, python-telegram-bot %s",
        sys.version.split()[0],
        telegram.__version__,
    )
    await init_db()
    await application.bot.set_my_commands([
        BotCommand("newtrip", "Create a new trip"),
        BotCommand("trips", "List trips · tap ✏️ to edit"),
        BotCommand("add", "Record a new expense"),
        BotCommand("balances", "See who owes what"),
        BotCommand("simplify", "Minimises the number of payments to settle all debts"),
        BotCommand("settle", "Record a payment"),
        BotCommand("history", "Browse & manage past expenses"),
        BotCommand("currency", "Change trip base currency"),
        BotCommand("help", "Show all commands"),
        BotCommand("settimezone", "Set your display timezone"),
        BotCommand("cancel", "Cancel current operation"),
    ])
    await application.bot.set_my_description(
        "SplitLah — split group expenses, hassle-free.\n\n"
        "• Create trips for holidays, housemates, or any outing\n"
        "• Log expenses in any currency with live FX conversion\n"
        "• Split equally, by ratio, percentage, or exact amount\n"
        "• /simplify — figures out the fewest payments needed to settle all debts\n"
        "• Nudge friends who owe you\n"
        "• Works in group chats and private chats\n\n"
        "Free. No ads. No subscriptions."
    )
    await application.bot.set_my_short_description(
        "Track shared expenses and settle up with the fewest payments. Free, no ads."
    )
    logger.info("SplitLah ready.")


def main() -> None:
    persistence = PicklePersistence(
        filepath="bot_data.pkl",
        store_data=PersistenceInput(user_data=True, bot_data=False, chat_data=False, callback_data=False),
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(_post_init)
        .build()
    )

    # ------------------------------------------------------------------
    # ConversationHandlers (must come first — they are greedy)
    # ------------------------------------------------------------------
    # Build as named variables so we can store references for cancel_all_flows
    # to clear internal ConversationHandler state (_conversations) on flow cancellation.
    _start_h = build_start_handler()
    _trip_h = build_trip_handler()
    _edit_trip_h = build_edit_trip_handler()
    _expense_h = build_expense_handler()
    _expense_act_h = build_expense_action_handler()
    _settle_h = build_settle_handler()
    _currency_h = build_currency_handler()
    _settimezone_h = build_settimezone_handler()

    app.bot_data["conv_handlers"] = [
        _start_h, _trip_h, _edit_trip_h, _expense_h, _expense_act_h,
        _settle_h, _currency_h, _settimezone_h,
    ]

    app.add_handler(_start_h)
    app.add_handler(_trip_h)
    app.add_handler(_edit_trip_h)
    app.add_handler(_expense_h)
    app.add_handler(_expense_act_h)
    app.add_handler(_settle_h)
    app.add_handler(_currency_h)
    app.add_handler(_settimezone_h)

    # ------------------------------------------------------------------
    # Plain command handlers
    # ------------------------------------------------------------------
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("trips", cmd_trips))
    app.add_handler(CommandHandler("balances", cmd_balances))
    app.add_handler(CommandHandler("simplify", cmd_simplify))
    app.add_handler(CommandHandler("history", cmd_history))

    # ------------------------------------------------------------------
    # group=-1 callbacks: run before ConversationHandler silent_answer fallbacks
    # can swallow them when another conversation is active
    # ------------------------------------------------------------------

    # Trip switching and onboarding completion
    app.add_handler(CallbackQueryHandler(switch_trip_callback, pattern=r"^sw_trip_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(ob_done_callback, pattern=r"^ob_done$"), group=-1)

    # History navigation and nudge
    app.add_handler(CallbackQueryHandler(history_page_callback, pattern=r"^hist_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(nudge_callback, pattern=r"^nudge_\d+_\d+$"), group=-1)

    # Simplify expand/collapse
    app.add_handler(CallbackQueryHandler(simp_more_callback, pattern=r"^simp_more_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(simp_expand_all_callback, pattern=r"^simp_expand_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(simp_collapse_callback, pattern=r"^simp_collapse_\d+$"), group=-1)

    # Settle flow (stateless — all state in callback_data)
    app.add_handler(CallbackQueryHandler(stl_pick_callback, pattern=r"^stl_\d+_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(stl_confirm_callback, pattern=r"^sconf_\d+_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(stl_back_callback, pattern=r"^stlback_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(stl_cancel_callback, pattern=r"^stlcnl$"), group=-1)

    # ------------------------------------------------------------------
    # Auto-register users who send messages in groups
    # ------------------------------------------------------------------
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
            register_context,
        )
    )

    # ------------------------------------------------------------------
    # Catch-all: dismiss stale / orphaned callback queries
    # ------------------------------------------------------------------
    app.add_handler(CallbackQueryHandler(stale_callback))

    logger.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
