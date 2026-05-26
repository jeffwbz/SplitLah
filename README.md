# SplitLah

> Split group expenses fairly, settle up fast — all inside Telegram.

A Telegram bot for tracking shared expenses across multiple trips. Designed for travel groups, housemates, and anyone splitting costs regularly. Works in group chats and private chats.

---

## Features

- **Guided expense entry** — step-by-step inline keyboard flow: description → amount → currency → who paid → split mode → participants → confirm; ← Back at every step
- **Four split modes** — equal, ratio (e.g. 2:1:1), percentage (must total 100), exact amounts
- **Multi-currency** — live FX conversion to the trip's base currency; exchange rates cached for 1 hour
- **Searchable currency picker** — type a country name ("Laos") or currency name ("Kip") to find any ISO code (LAK); works for both the trip base currency and per-expense currency
- **Per-trip expense numbering** — each trip's expenses are numbered from #1, independent of other trips; numbers are computed dynamically so deletion never creates gaps
- **Debt simplification** — greedy algorithm minimises the number of transfers needed to settle all debts in a trip
- **Itemised nudges** — 👋 button sends a public reminder listing the debtor's specific outstanding expenses and amounts
- **Settlement recording** — log payments between members; balances update immediately
- **Multi-trip support** — separate trips for different occasions; switch the active trip with `/trips`
- **Timezone-aware timestamps** — defaults to SGT (UTC+8); each user can set their own timezone with `/settimezone`
- **Virtual members** — add people who aren't on Telegram
- **Works anywhere** — group chats and private chats; group members are auto-registered when they send messages

---

## Commands

| Command | Description |
|---|---|
| `/start` | First-time setup: timezone → base currency → first trip |
| `/newtrip` | Create a new named trip |
| `/trips` | List all trips; tap ✏️ to rename, add/remove members, clear history, or delete |
| `/add` | Record a new expense (guided flow) |
| `/balances` | Net balance for every member in the active trip |
| `/simplify` | Minimum transfers to settle all debts, with optional per-expense breakdown |
| `/settle` | Record a payment between two members |
| `/history` | Browse past expenses with pagination; tap any row to edit or delete |
| `/currency` | Change the base currency for the active trip |
| `/settimezone` | Set your personal display timezone |
| `/help` | Show all commands |
| `/cancel` | Abort the current operation |

**Inline actions** (triggered via buttons, not typed commands):

| Action | Where |
|---|---|
| 👋 **Nudge** | `/simplify` — sends a public reminder with the debtor's itemised expense breakdown |
| ✏️ **Edit description** | `/history` — tap any expense row to edit its description |
| 🗑 **Delete** | `/history` — tap any expense row to permanently delete it |

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| Telegram | [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v22 (async) |
| Database | SQLite (`aiosqlite`) locally · PostgreSQL (`asyncpg`) on Railway |
| ORM | SQLAlchemy 2.0 async |
| FX rates | [ExchangeRate-API](https://exchangerate-api.com) (optional, 160+ currencies) · [frankfurter.app](https://frankfurter.app) (fallback, ~32 currencies) |
| Deployment | Railway — auto-deploys from GitHub on push to `main` |

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

# 3. Configure environment
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here        # from @BotFather
DATABASE_URL=sqlite+aiosqlite:///splitlah.db  # SQLite default, no setup needed
DEFAULT_CURRENCY=SGD                           # optional
EXCHANGE_RATE_API_KEY=your_key_here           # optional — free at exchangerate-api.com
```

> **Bot token:** Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts.

> **ExchangeRate-API:** Without a key the bot falls back to frankfurter.app (~32 currencies). With a free key you get 160+ currencies and higher rate limits.

```bash
# 4. Run
python -m bot.main
```

The SQLite database (`splitlah.db`) is created automatically on first run. No migrations needed.

---

## Deploy on Railway

1. **Push to GitHub** and ensure the repo is accessible to Railway.

2. **Create a Railway project** → New Project → Deploy from GitHub repo → select this repo.

3. **Add a PostgreSQL database** — in your Railway project click **+ New** → **Database** → **PostgreSQL**. Railway sets `DATABASE_URL` automatically.

4. **Set environment variables** under your service → Variables:
   - `TELEGRAM_BOT_TOKEN` — your token from @BotFather
   - `EXCHANGE_RATE_API_KEY` — optional but recommended
   - `DEFAULT_CURRENCY` — optional, defaults to `SGD`

5. **Deploy** — Railway reads `railway.toml` and runs `python -m bot.main` as a worker process. No web server needed.

The `postgres://` URL from Railway is rewritten to `postgresql+asyncpg://` by `config.py` automatically.

---

## Project Structure

```
bot/
├── main.py               Entry point; registers all handlers, sets bot profile on startup
├── config.py             Environment variables, DATABASE_URL normalisation
├── database.py           SQLAlchemy 2.0 async schema and all query helpers
├── currency.py           FX lookup (ExchangeRate-API + frankfurter fallback, 1 h cache)
├── debt.py               Debt simplification algorithm
├── splits.py             Share calculation for all four split modes
├── formatters.py         Message formatting helpers
└── handlers/
    ├── common.py          Shared utilities, /help, /cancel, member registration middleware
    ├── onboarding.py      /start — first-time guided setup
    ├── trip.py            /newtrip, /trips, trip editing and member management
    ├── expense.py         /add — 10-state guided conversation
    ├── expense_actions.py Expense detail view, edit description, delete (from /history)
    ├── balance.py         /balances, /simplify, /history, /currency, nudge
    ├── settle.py          /settle — stateless callbacks, no ConversationHandler needed
    └── settimezone.py     /settimezone

tests/
└── test_formatters.py
```

**Database:** SQLite locally, PostgreSQL in production — same SQLAlchemy schema, no code changes required between environments.

---

## Notes

- **Timestamps** default to SGT (UTC+8). Each user can override with `/settimezone`; the setting affects all expense timestamps shown to that user.
- **Expense numbers** are per-trip, start at #1, and are computed dynamically from the current set of expenses — deletion never creates numbering gaps.
- **Balances** are recalculated on every `/balances` call from raw expense and settlement data; no stored running totals.
- **FX rates** are captured at expense creation time and stored with the expense; they are not recalculated when viewing balances.
- **Callback routing** — non-conversation callbacks (history pagination, simplify buttons, settle flow, trip switching) are registered in handler group `-1` and raise `ApplicationHandlerStop` after handling, preventing ConversationHandler fallbacks from silently swallowing them.
