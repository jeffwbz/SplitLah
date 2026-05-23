import logging
import time

import httpx

from bot.config import EXCHANGE_RATE_API_KEY

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, float]] = {}  # key -> (rate, timestamp)
_CACHE_TTL = 3600

_currencies_cache: tuple[dict[str, str], float] | None = None

# Common informal / alternative codes that differ from ISO 4217
_ALIASES: dict[str, str] = {
    "NTD": "TWD",
    "RMB": "CNY",
    "YUAN": "CNY",
    "RENMINBI": "CNY",
    "BAHT": "THB",
    "RINGGIT": "MYR",
    "RUPIAH": "IDR",
    "WON": "KRW",
    "DONG": "VND",
    "PESO": "PHP",
    "RUPEE": "INR",
    "DIRHAM": "AED",
    "RIYAL": "SAR",
}


async def get_fx_rate(from_currency: str, to_currency: str) -> float:
    if from_currency == to_currency:
        return 1.0

    key = f"{from_currency}-{to_currency}"
    cached = _cache.get(key)
    if cached:
        rate, ts = cached
        if time.time() - ts < _CACHE_TTL:
            logger.debug("FX %s→%s: %.4f (cached)", from_currency, to_currency, rate)
            return rate

    try:
        if EXCHANGE_RATE_API_KEY:
            rate = await _get_fx_rate_exchangerate(from_currency, to_currency)
        else:
            rate = await _get_fx_rate_frankfurter(from_currency, to_currency)
        _cache[key] = (rate, time.time())
        logger.info("FX %s→%s: %.4f", from_currency, to_currency, rate)
        return rate
    except Exception as exc:
        logger.warning("FX lookup failed (%s→%s): %s", from_currency, to_currency, exc)
        if key in _cache:
            stale_rate = _cache[key][0]
            logger.warning("FX %s→%s: using stale cached rate %.4f", from_currency, to_currency, stale_rate)
            return stale_rate
        raise


async def _get_fx_rate_exchangerate(from_currency: str, to_currency: str) -> float:
    url = (
        f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}"
        f"/pair/{from_currency}/{to_currency}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            raise ValueError(f"ExchangeRate-API error: {data.get('error-type', 'unknown')}")
        return float(data["conversion_rate"])


async def _get_fx_rate_frankfurter(from_currency: str, to_currency: str) -> float:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(
            "https://api.frankfurter.app/latest",
            params={"from": from_currency, "to": to_currency},
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data["rates"][to_currency])


async def convert(amount: float, from_currency: str, to_currency: str) -> tuple[float, float]:
    """Returns (converted_amount, fx_rate)."""
    rate = await get_fx_rate(from_currency, to_currency)
    return round(amount * rate, 2), rate


async def get_all_currencies() -> dict[str, str]:
    """Return {code: name} for all supported currencies."""
    global _currencies_cache
    if _currencies_cache:
        data, ts = _currencies_cache
        if time.time() - ts < _CACHE_TTL:
            logger.debug("get_all_currencies: cache hit (%d currencies)", len(data))
            return data
    try:
        if EXCHANGE_RATE_API_KEY:
            data = await _get_all_currencies_exchangerate()
        else:
            data = await _get_all_currencies_frankfurter()
        _currencies_cache = (data, time.time())
        logger.info("get_all_currencies: fetched %d currencies", len(data))
        return data
    except Exception as exc:
        logger.warning("Currency list fetch failed: %s", exc)
        if _currencies_cache:
            logger.warning("get_all_currencies: returning stale cache")
            return _currencies_cache[0]
        return {}


async def _get_all_currencies_exchangerate() -> dict[str, str]:
    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/codes"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            raise ValueError(f"ExchangeRate-API error: {data.get('error-type', 'unknown')}")
        return {code: name for code, name in data["supported_codes"]}


async def _get_all_currencies_frankfurter() -> dict[str, str]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get("https://api.frankfurter.app/currencies")
        resp.raise_for_status()
        return resp.json()


async def search_currencies(query: str) -> list[tuple[str, str]]:
    """Search currencies by code or name. Returns [(code, name), ...] up to 8 results."""
    q = query.strip().upper()
    resolved = _ALIASES.get(q)
    if resolved:
        q = resolved

    all_cur = await get_all_currencies()
    if not all_cur:
        return []

    q_lower = query.strip().lower()
    exact: list[tuple[str, str]] = []
    partial: list[tuple[str, str]] = []
    for code, name in all_cur.items():
        if code == q:
            exact.append((code, name))
        elif code.startswith(q) or q_lower in name.lower():
            partial.append((code, name))

    return (exact + partial)[:8]


def resolve_alias(code: str) -> str:
    """Resolve informal currency codes to ISO 4217 (e.g. NTD → TWD)."""
    return _ALIASES.get(code.upper(), code.upper())


async def is_currency_supported(currency: str) -> bool:
    try:
        all_cur = await get_all_currencies()
        if all_cur:
            return currency.upper() in all_cur
    except Exception:
        pass
    return True  # assume valid if lookup fails
