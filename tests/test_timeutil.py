"""Tests for the Asia/Kolkata (IST) time helpers used everywhere in the
pipeline — see src/timeutil.py."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.timeutil import IST, is_trading_day, now_ist, to_ist, today_ist, trading_date, trading_date_str


def test_now_ist_is_timezone_aware_in_kolkata():
    now = now_ist()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 5.5 * 3600


def test_to_ist_attaches_ist_to_naive_datetime_without_shifting_the_clock():
    naive = datetime(2026, 9, 15, 10, 30, 0)
    aware = to_ist(naive)
    assert aware.tzinfo == IST
    assert (aware.hour, aware.minute) == (10, 30)


def test_to_ist_converts_aware_datetime_from_another_zone():
    utc_dt = datetime(2026, 9, 15, 5, 0, 0, tzinfo=timezone.utc)
    ist_dt = to_ist(utc_dt)
    assert (ist_dt.hour, ist_dt.minute) == (10, 30)  # UTC 05:00 == IST 10:30


def test_trading_date_uses_ist_not_utc_near_midnight_boundary():
    # 23:00 UTC on the 15th is 04:30 IST on the 16th — a UTC-naive
    # trading-date calc would wrongly attribute this to the 15th.
    utc_dt = datetime(2026, 9, 15, 23, 0, 0, tzinfo=timezone.utc)
    assert trading_date_str(utc_dt) == "2026-09-16"


def test_trading_date_str_format():
    dt = datetime(2026, 9, 15, 10, 0, 0, tzinfo=IST)
    assert trading_date(dt).isoformat() == trading_date_str(dt) == "2026-09-15"


def test_today_ist_matches_now_ist_date():
    assert today_ist() == now_ist().date()


def test_is_trading_day_flags_weekends():
    tuesday = datetime(2026, 9, 15).date()
    saturday = datetime(2026, 9, 19).date()
    sunday = datetime(2026, 9, 20).date()
    assert is_trading_day(tuesday) is True
    assert is_trading_day(saturday) is False
    assert is_trading_day(sunday) is False


def test_is_trading_day_agrees_with_market_calendar_on_holidays():
    # 2026-09-14 is a Monday but an NSE holiday (Ganesh Chaturthi) — timeutil
    # delegates to market_calendar so there is only one trading-day rule.
    assert is_trading_day(datetime(2026, 9, 14).date()) is False
