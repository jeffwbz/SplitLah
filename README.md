# SplitLah — Telegram Expense Splitting Bot

Split group expenses fairly, track debts, and settle up — right inside Telegram.

## Features

- **Guided expense entry** via inline keyboards (description → amount → currency → payer → split mode → participants → confirm)
- **Four split modes**: equal, ratio (e.g. 2:1:1), percentage, exact amounts
- **Multi-currency** with live FX conversion via [frankfurter.app](https://frankfurter.app) (rates cached 1 h)
- **Debt simplification** — greedy algorithm minimises the number of settlement transactions
- **Settlement tracking** — record partial or full payments between members
- **Paginated expense history**

## Commands

| Command | Description |
|---------|-------------|
| `/add` | Record a new expense (guided) |
| `/balances` | Net balance for every group member |
| `/simplify` | Minimum payments needed to settle all debts |
| `/settle` | Record a payment between members |
| `/history` | Browse past expenses |
| `/currency` | Change the group's base currency |
| `/cancel` | Abort the current operation |

## Quick Start (local)

```bash
# 1. Create a virtualenv and activate it
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN (get one from @BotFather)

# 4. Run
python -m bot.main
```

SQLite is used by default (`splitlah.db` in the project root). No database setup needed.

## Deploy on Railway

1. Push this repo to GitHub.
2. Create a new Railway project → **Deploy from GitHub repo**.
3. Add a **PostgreSQL** plugin — Railway sets `DATABASE_URL` automatically.
4. Add an environment variable: `TELEGRAM_BOT_TOKEN=<your token>`.
5. Railway reads `railway.toml` and runs the bot as a worker process.

## Architecture

```
bot/
  config.py        env vars, DATABASE_URL normalisation (postgres:// -> postgresql+asyncpg://)
  database.py      SQLAlchemy 2.0 async engine, schema, all query helpers
  currency.py      FX lookup via frankfurter.app (async, 1-hour cache)
  debt.py          Debt-simplification algorithm
  splits.py        Share calculation for all four split modes
  formatters.py    Message formatting helpers
  main.py          Application entry point, handler registration
  handlers/
    common.py      /start, /help, member-registration middleware
    expense.py     /add conversation (9-state machine)
    balance.py     /balances, /simplify, /history, /currency
    settle.py      /settle conversation
tests/
  test_formatters.py
```

**Database**: SQLite (`aiosqlite`) locally, PostgreSQL (`asyncpg`) in production — same SQLAlchemy schema, zero code changes.

## How debt simplification works

Each member's net balance = money they paid − their share of expenses + settlements received − settlements sent.

A positive balance means others owe them; negative means they owe others. The greedy algorithm iteratively matches the largest creditor with the largest debtor, producing the minimum number of payment transactions in O(n log n).
