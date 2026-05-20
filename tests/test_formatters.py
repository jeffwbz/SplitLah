# -*- coding: utf-8 -*-
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from bot.formatters import fmt_money, display_name, time_ago, fmt_datetime, _tz_abbr

def test_fmt_money():
    assert fmt_money(45.5, "SGD") == "S$45.50"
    assert fmt_money(1500.0, "JPY") == "¥1,500"
    assert fmt_money(1234567.89, "IDR") == "Rp1,234,568"
    assert fmt_money(9.99, "USD") == "$9.99"

def test_display_name():
    assert display_name({"id": 1, "username": "alice", "first_name": "Alice", "last_name": None}) == "@alice"
    assert display_name({"id": 2, "username": None, "first_name": "Bob", "last_name": "Smith"}) == "Bob Smith"
    assert display_name({"id": 3, "username": None, "first_name": None, "last_name": None}) == "Member#3"

def test_time_ago():
    now = datetime.now(timezone.utc)
    assert time_ago(now - timedelta(seconds=30)) == "just now"
    assert time_ago(now - timedelta(minutes=5)) == "5m ago"
    assert time_ago(now - timedelta(hours=3)) == "3h ago"
    assert time_ago(now - timedelta(days=1)) == "yesterday"
    assert time_ago(now - timedelta(days=5)) == "5d ago"

def test_tz_abbr_fallback():
    """_tz_abbr must return a named abbreviation, never a numeric offset like +08."""
    dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert _tz_abbr(dt.astimezone(ZoneInfo("Asia/Singapore")))    == "SGT"
    assert _tz_abbr(dt.astimezone(ZoneInfo("Asia/Tokyo")))        == "JST"
    assert _tz_abbr(dt.astimezone(ZoneInfo("Asia/Kuala_Lumpur"))) == "MYT"
    assert _tz_abbr(dt.astimezone(ZoneInfo("Asia/Jakarta")))      == "WIB"
    assert _tz_abbr(dt.astimezone(ZoneInfo("Asia/Kolkata")))      == "IST"
    # America/New_York in January is winter -> EST (tzname returns "EST" natively)
    result = _tz_abbr(dt.astimezone(ZoneInfo("America/New_York")))
    assert result == "EST", f"Expected EST, got {result!r}"


def test_fmt_datetime_timezones():
    """Verify time conversion accuracy and timezone label for SGT, JST, and EST."""
    # Anchor: 2026-01-15 12:00 UTC  (January = no DST anywhere)
    dt_utc = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

    # SGT = UTC+8 -> 20:00 same day
    sgt = fmt_datetime(dt_utc, tz=ZoneInfo("Asia/Singapore"))
    assert "20:00" in sgt,         f"SGT time wrong: {sgt}"
    assert "SGT"   in sgt,         f"SGT label missing: {sgt}"
    assert "15 Jan 2026" in sgt,   f"SGT date wrong: {sgt}"

    # JST = UTC+9 -> 21:00 same day
    jst = fmt_datetime(dt_utc, tz=ZoneInfo("Asia/Tokyo"))
    assert "21:00" in jst,         f"JST time wrong: {jst}"
    assert "JST"   in jst,         f"JST label missing: {jst}"
    assert "15 Jan 2026" in jst,   f"JST date wrong: {jst}"

    # EST = UTC-5 -> 07:00 same day
    est = fmt_datetime(dt_utc, tz=ZoneInfo("America/New_York"))
    assert "07:00" in est,         f"EST time wrong: {est}"
    assert "EST"   in est,         f"EST label missing: {est}"
    assert "15 Jan 2026" in est,   f"EST date wrong: {est}"

    # Default (no tz arg) falls back to SGT
    default = fmt_datetime(dt_utc)
    assert "SGT"   in default,     f"Default label wrong: {default}"
    assert "20:00" in default,     f"Default time wrong: {default}"

    print(f"  SGT -> {sgt}")
    print(f"  JST -> {jst}")
    print(f"  EST -> {est}")


if __name__ == "__main__":
    test_fmt_money()
    test_display_name()
    test_time_ago()
    test_tz_abbr_fallback()
    test_fmt_datetime_timezones()
    print("All formatter tests passed.")
