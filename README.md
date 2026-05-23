# SplitLah — Telegram Expense Splitting Bot

Split group expenses fairly, track debts, and settle up — all inside Telegram. Works in group chats and private chats.

---

## Features

- **Guided first-time setup** — `/start` walks you through timezone, currency, and your first trip; prompts to add members immediately after, or skip trip creation and come back later
- **Guided expense entry** — step-by-step inline keyboard flow: description → amount → currency → who paid → split mode → participants → confirm
- **Four split modes** — equal, ratio (e.g. 2:1:1), percentage (must total 100), exact amounts
- **Multi-currency** — live FX conversion via [ExchangeRate-API](https://exchangerate-api.com) (160+ currencies) with fallback to [frankfurter.app](https://frankfurter.app) (~32 currencies); rates cached for 1 hour
- **Debt simplification** — greedy algorithm minimises the number of transfers needed to settle all debts
- **Grouped settle-up view** — `/simplify` groups debts by debtor with optional per-expense breakdown and nudge buttons
- **Settlement recording** — log payments between members; balances update immediately
- **Paginated history** — browse all past expenses; tap any row to edit or delete
- **Multiple trips** — create separate trips (holiday, housemates, dinner club); switch between them instantly
- **Virtual members** — add people who aren't on Telegram
- **Per-user timezones** — timestamps shown in each user's local time; defaults to SGT (UTC+8)
- **Works anywhere** — group chats auto-register members; private chats work with virtual members

---

## Commands

| Command | Description |
|---|---|
| `/start` | First-time setup: timezone → currency → first trip |
| `/newtrip` | Create a new trip |
| `/trips` | List trips and switch active trip; tap ✏️ to rename, add/remove members, clear history, or delete |
| `/add` | Record a new expense (guided flow) |
| `/balances` | Net balance for every member (🟢 owed · 🔴 owes · ✅ settled) |
| `/simplify` | Minimum transfers to settle all debts, grouped by debtor |
| `/settle` | Record a payment between members |
| `/history` | Browse past expenses with pagination; tap to edit description or delete |
| `/currency` | Change the base currency for the active trip |
| `/settimezone` | Set your personal display timezone |
| `/help` | Show all commands |
| `/cancel` | Abort the current operation |

---

## Local Setup

**Requirements:** Python 3.11+

```bash
# 1. Clone and create a virtual environment
git clone https://github.com/jeffwbz/SplitLah.git
cd SplitLah
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here        # from @BotFather
DATABASE_URL=sqlite+aiosqlite:///splitlah.db  # SQLite default, no setup needed
DEFAULT_CURRENCY=SGD                           # optional, default SGD
EXCHANGE_RATE_API_KEY=your_key_here           # optional — free at exchangerate-api.com
```

> **Getting a bot token:** Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts.

> **ExchangeRate-API key:** Without it the bot falls back to frankfurter.app which covers ~32 currencies. With a free key you get 160+ currencies and higher rate limits.

```bash
# 4. Run
python -m bot.main
```

SQLite database (`splitlah.db`) is created automatically on first run. No migrations needed.

---

## Deploy on Railway

1. **Push to GitHub** (already done if you're reading this).

2. **Create a Railway project** → New Project → Deploy from GitHub repo → select this repo.

3. **Add a PostgreSQL database** — in your Railway project click **+ New** → **Database** → **PostgreSQL**. Railway sets `DATABASE_URL` automatically.

4. **Add environment variables** in Railway → your service → Variables:
   - `TELEGRAM_BOT_TOKEN` — your token from @BotFather
   - `EXCHANGE_RATE_API_KEY` — optional but recommended
   - `DEFAULT_CURRENCY` — optional, defaults to SGD

5. **Deploy** — Railway reads `railway.toml` and runs `python -m bot.main` as a worker. No web server needed.

The `DATABASE_URL` from Railway uses the `postgres://` scheme; `config.py` rewrites it to `postgresql+asyncpg://` automatically.

---

## Project Structure

```
bot/
├── main.py              Entry point; handler registration and polling
├── config.py            Environment variables, DATABASE_URL normalisation
├── database.py          SQLAlchemy 2.0 async schema and all query helpers
├── currency.py          FX lookup (ExchangeRate-API + frankfurter fallback, 1 h cache)
├── debt.py              Debt simplification algorithm
├── splits.py            Share calculation for all four split modes
├── formatters.py        Message formatting (balances, simplify, expense summary, history)
└── handlers/
    ├── common.py         /help, /cancel, member registration middleware, shared utilities
    ├── onboarding.py     /start — first-time setup (timezone → currency → trip name)
    ├── trip.py           /newtrip, /trips, trip editing
    ├── expense.py        /add — 9-state guided conversation
    ├── expense_actions.py Expense detail, edit description, delete (from /history)
    ├── balance.py        /balances, /simplify, /history, /currency
    ├── settle.py         /settle — standalone callbacks (no ConversationHandler)
    └── settimezone.py    /settimezone

tests/
└── test_formatters.py
```

**Database:** SQLite (`aiosqlite`) locally, PostgreSQL (`asyncpg`) in production — same SQLAlchemy schema, zero code changes required.

---

## How It Works

### Debt simplification

Each member's net balance = amounts paid − expense shares + settlements received − settlements sent.

Positive balance → others owe them. Negative → they owe others. The greedy algorithm repeatedly pairs the largest creditor with the largest debtor, producing the minimum number of payment transactions in O(n log n).

### Callback routing

python-telegram-bot's `ConversationHandler` fallbacks (`silent_answer`) can intercept unmatched callbacks from any active conversation. To prevent this, all non-conversation callbacks — `/simplify` buttons, history pagination, settle flow, trip switching — are registered in **handler group `-1`** and raise `ApplicationHandlerStop` after handling, so they always run before any conversation fallback can swallow them.

### FX conversion

`currency.py` queries ExchangeRate-API first (free tier: 1,500 requests/month). If the key is absent or the request fails, it falls back to frankfurter.app. All rates are cached in-memory for 1 hour per currency pair.
