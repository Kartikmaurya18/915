"""
Single source of truth for Indian-market (Asia/Kolkata / IST) time handling.

Convention used throughout this codebase: a *naive* datetime anywhere in the
pipeline (e.g. parsed from an exchange's "DD-Mon-YYYY HH:MM:SS" string) is
always treated as already being IST wall-clock time — never UTC, never
server-local. `to_ist()` enforces that convention: naive datetimes get the
IST zone attached (not converted), while timezone-aware datetimes are
correctly converted into IST regardless of their original zone.

Use `now_ist()` instead of `datetime.now()` anywhere a "current time" is
needed for market data, so behavior is identical whether the server process
itself happens to be running in UTC (e.g. a cloud host), IST, or anything
else — see the deployment note this fixes in CLIENT_CLARIFICATIONS.md /
the timezone requirement.
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def get_zone(tz: str | tzinfo | None = None) -> tzinfo:
    """Resolve a timezone string/object to a tzinfo, defaulting to IST."""
    if tz is None:
        return IST
    if isinstance(tz, str):
        try:
            return ZoneInfo(tz)
        except Exception:
            return IST
    return tz


def now_ist(tz: str | tzinfo | None = None) -> datetime:
    """Current time, timezone-aware, in IST (or the given zone)."""
    return datetime.now(get_zone(tz))


def to_ist(value: datetime, tz: str | tzinfo | None = None) -> datetime:
    """
    Normalize any datetime to timezone-aware IST.

    Naive input is assumed to already represent IST wall-clock time (the
    convention this whole module enforces) and simply gets the zone
    attached. Aware input is converted.
    """
    zone = get_zone(tz)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def trading_date(value: datetime, tz: str | tzinfo | None = None) -> date:
    """The Asia/Kolkata calendar date a given datetime falls on."""
    return to_ist(value, tz).date()


def trading_date_str(value: datetime, tz: str | tzinfo | None = None) -> str:
    """The Asia/Kolkata calendar date, as an ISO "YYYY-MM-DD" string."""
    return trading_date(value, tz).isoformat()


def today_ist(tz: str | tzinfo | None = None) -> date:
    """Today's date in Asia/Kolkata."""
    return now_ist(tz).date()


def is_trading_day(d: date) -> bool:
    """Mon-Fri and not a known exchange holiday — delegates to
    src/market_calendar.py so there is exactly one trading-day rule."""
    from src.market_calendar import is_trading_day as _calendar_is_trading_day

    return _calendar_is_trading_day(d)
