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

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
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
from bot.handlers.common import cmd_help, register_context, stale_callback
from bot.handlers.onboarding import build_start_handler, ob_done_callback
from bot.handlers.expense import build_expense_handler
from bot.handlers.expense_actions import build_expense_action_handler
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
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # Conversation handlers first (they're greedy)
    app.add_handler(build_start_handler())
    app.add_handler(build_trip_handler())
    app.add_handler(build_edit_trip_handler())
    app.add_handler(build_expense_handler())
    app.add_handler(build_expense_action_handler())
    app.add_handler(build_settle_handler())
    app.add_handler(build_currency_handler())
    app.add_handler(build_settimezone_handler())

    # Plain command handlers
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("trips", cmd_trips))
    app.add_handler(CommandHandler("balances", cmd_balances))
    app.add_handler(CommandHandler("simplify", cmd_simplify))
    app.add_handler(CommandHandler("history", cmd_history))

    # sw_trip_ must run before any conversation's silent_answer fallback can swallow it
    app.add_handler(CallbackQueryHandler(switch_trip_callback, pattern=r"^sw_trip_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(ob_done_callback, pattern=r"^ob_done$"), group=-1)

    # Non-conversation callback handlers — registered in group=-1 so ConversationHandler
    # fallbacks (silent_answer) can't swallow them when another conversation is active
    app.add_handler(CallbackQueryHandler(history_page_callback, pattern=r"^hist_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(nudge_callback, pattern=r"^nudge_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(simp_more_callback, pattern=r"^simp_more_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(simp_expand_all_callback, pattern=r"^simp_expand_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(simp_collapse_callback, pattern=r"^simp_collapse_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(stl_pick_callback, pattern=r"^stl_\d+_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(stl_confirm_callback, pattern=r"^sconf_\d+_\d+_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(stl_back_callback, pattern=r"^stlback_\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(stl_cancel_callback, pattern=r"^stlcnl$"), group=-1)

    # Auto-register users who send any message (groups only)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
            register_context,
        )
    )

    # Catch-all for stale / orphaned callbacks
    app.add_handler(CallbackQueryHandler(stale_callback))

    logger.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
