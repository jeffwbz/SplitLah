import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

_raw = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///splitlah.db")
if _raw.startswith("postgres://"):
    DATABASE_URL = "postgresql+asyncpg" + _raw[len("postgres"):]
elif _raw.startswith("postgresql://") and "+asyncpg" not in _raw:
    DATABASE_URL = "postgresql+asyncpg" + _raw[len("postgresql"):]
else:
    DATABASE_URL = _raw

DEFAULT_CURRENCY: str = os.getenv("DEFAULT_CURRENCY", "SGD")

EXCHANGE_RATE_API_KEY: str = os.getenv("EXCHANGE_RATE_API_KEY", "")

SUPPORTED_CURRENCIES: list[str] = [
    "SGD", "MYR", "USD", "EUR", "GBP",
    "IDR", "THB", "JPY", "CNY", "AUD",
]
