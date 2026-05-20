from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SGT = ZoneInfo("Asia/Singapore")

_TZ_ABBR_FALLBACK: dict[str, str] = {
    "Asia/Singapore":    "SGT",
    "Asia/Kuala_Lumpur": "MYT",
    "Asia/Hong_Kong":    "HKT",
    "Asia/Shanghai":     "CST",
    "Asia/Tokyo":        "JST",
    "Asia/Seoul":        "KST",
    "Asia/Bangkok":      "ICT",
    "Asia/Jakarta":      "WIB",
    "Asia/Kolkata":      "IST",
    "Asia/Dubai":        "GST",
    "UTC":               "UTC",
}


def _tz_abbr(dt: datetime) -> str:
    """Return a human-readable timezone abbreviation for an aware datetime.

    strftime('%Z') on Windows returns numeric offsets like '+08' for IANA zones.
    We fall back to a lookup table when that happens.
    """
    abbr = dt.tzname() or ""
    if abbr and not abbr.startswith(("+", "-")):
        return abbr
    key = getattr(dt.tzinfo, "key", None)
    if key:
        return _TZ_ABBR_FALLBACK.get(key, abbr)
    return abbr


def resolve_tz(iana_name: str | None):
    """Return a tzinfo for the stored IANA name, falling back to SGT."""
    if iana_name:
        try:
            return ZoneInfo(iana_name)
        except (ZoneInfoNotFoundError, KeyError):
            pass
    return SGT

CURRENCY_SYMBOLS: dict[str, str] = {
    "SGD": "S$", "MYR": "RM", "USD": "$", "EUR": "€", "GBP": "£",
    "IDR": "Rp", "THB": "฿", "JPY": "¥", "CNY": "¥", "AUD": "A$",
    "HKD": "HK$", "KRW": "₩", "NZD": "NZ$", "CAD": "C$", "CHF": "Fr",
    "TWD": "NT$", "VND": "₫", "PHP": "₱", "INR": "₹", "SAR": "SR",
}

NO_DECIMAL = {"JPY", "IDR", "KRW", "VND"}


def fmt_money(amount: float, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    if currency in NO_DECIMAL:
        return f"{symbol}{int(round(amount)):,}"
    return f"{symbol}{amount:,.2f}"


def display_name(member: dict) -> str:
    """Works with both trip_members rows and plain user dicts."""
    if "display_name" in member and member["display_name"]:
        return member["display_name"]
    if member.get("username"):
        return f"@{member['username']}"
    parts = [member.get("first_name") or "", member.get("last_name") or ""]
    return " ".join(p for p in parts if p) or f"Member#{member.get('id', '?')}"


def user_display_name(user: dict) -> str:
    """Display name for a raw Telegram user (no display_name field)."""
    if user.get("username"):
        return f"@{user['username']}"
    parts = [user.get("first_name") or "", user.get("last_name") or ""]
    return " ".join(p for p in parts if p) or f"User#{user.get('id', '?')}"


def time_ago(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%d %b %Y")


def fmt_datetime(dt: datetime, tz=None) -> str:
    """Absolute date + time in the given timezone (defaults to SGT)."""
    effective_tz = tz if tz is not None else SGT
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(effective_tz)
    return f"{dt.day} {dt.strftime('%b %Y, %H:%M')} {_tz_abbr(dt)}"


def fmt_split_mode(mode: str) -> str:
    return {
        "equal": "Equal split",
        "ratio": "Ratio split",
        "percentage": "Percentage split",
        "exact": "Exact amounts",
    }.get(mode, mode.title())


def fmt_balances(balances: list[dict], trip_name: str, base_currency: str) -> str:
    lines = [f"*{trip_name} · Balances*", f"_Amounts in {base_currency}_", ""]
    if not balances:
        return "\n".join(lines) + "_No expenses yet._"

    for row in balances:
        name = display_name(row)
        net = row["net"]
        if net > 0.005:
            lines.append(f"🟢 {name} is owed {fmt_money(net, base_currency)}")
        elif net < -0.005:
            lines.append(f"🔴 {name} owes {fmt_money(-net, base_currency)}")
        else:
            lines.append(f"✅ {name} settled")

    lines += ["", "_/simplify to minimise transfers_"]
    return "\n".join(lines)


def fmt_simplified(
    transactions: list[tuple[int, int, float]],
    member_map: dict[int, dict],
    trip_name: str,
    base_currency: str,
    breakdown: dict[int, list[dict]] | None = None,
    expanded_debtors: set[int] | None = None,
) -> str:
    lines = [f"*{trip_name} · Settle up*", ""]
    if not transactions:
        return "\n".join(lines) + "_All settled up! 🎉_"

    # Group transactions by debtor, preserving first-appearance order
    debtor_order: list[int] = []
    debtor_payments: dict[int, list[tuple[int, float]]] = {}
    for from_id, to_id, amount in transactions:
        if from_id not in debtor_payments:
            debtor_order.append(from_id)
            debtor_payments[from_id] = []
        debtor_payments[from_id].append((to_id, amount))

    for debtor_id in debtor_order:
        payments = debtor_payments[debtor_id]
        total = sum(amt for _, amt in payments)
        frm = display_name(member_map[debtor_id])
        lines.append(f"👤 *{frm}* · {fmt_money(total, base_currency)} total")
        for to_id, amount in payments:
            to = display_name(member_map[to_id])
            lines.append(f"  → {to} · {fmt_money(amount, base_currency)}")
        if breakdown is not None and expanded_debtors is not None and debtor_id in expanded_debtors:
            for exp in breakdown.get(debtor_id, []):
                payer = exp.get("payer_name")
                payer_str = f" _(paid by {payer})_" if payer else ""
                lines.append(f"  • {exp['description']} — {fmt_money(exp['share_amount'], base_currency)}{payer_str}")
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()

    lines += ["", "_/settle to record a payment_"]
    return "\n".join(lines)


def fmt_expense_summary(expense: dict, shares: list[dict], base_currency: str) -> str:
    payer = display_name(expense["payer"])
    amount_str = fmt_money(expense["amount"], expense["currency"])

    lines = [f"*{expense['description']}*", ""]

    if expense["currency"] != base_currency:
        converted = fmt_money(expense["amount_base"], base_currency)
        lines.append(f"{amount_str} _(≈ {converted})_")
    else:
        lines.append(amount_str)

    split_mode = expense["split_mode"]
    lines.append(f"💳 Paid by *{payer}* · {fmt_split_mode(split_mode)}")

    if shares:
        lines.append("")
        for share in shares:
            lines.append(f"  {display_name(share)} · {fmt_money(share['share_amount'], base_currency)}")

    return "\n".join(lines)


def fmt_history(
    expenses: list[dict], trip_name: str, base_currency: str, page: int, total_pages: int
) -> str:
    lines = [f"*{trip_name} · History* _({page + 1}/{total_pages})_", ""]
    if not expenses:
        return "\n".join(lines) + "_No expenses yet._"
    for exp in expenses:
        payer = exp.get("payer_name", "?")
        when = fmt_datetime(exp["created_at"])
        amount_str = fmt_money(exp["amount_base"], base_currency)

        lines.append(f"*#{exp['id']} {exp['description']}* — {amount_str}")
        meta = f"{payer} · {when}"
        if exp["currency"] != base_currency:
            meta += f"\n_{fmt_money(exp['amount'], exp['currency'])} (1 {exp['currency']} = {exp['fx_rate']:.4f} {base_currency})_"
        lines.append(meta)
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
