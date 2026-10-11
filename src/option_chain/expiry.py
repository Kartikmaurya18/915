from __future__ import annotations

from datetime import date, datetime, timedelta

from src.market_calendar import is_trading_day, previous_trading_day
from src.timeutil import today_ist

# NIFTY's historical weekly-expiry weekday (Thursday). MOCK/Replay data has no
# real exchange calendar to consult, so this is just a reasonable, documented
# default for the auto-computed fallback below — not a claim about NSE's
# actual (and occasionally-revised) expiry-day rules.
DEFAULT_WEEKLY_EXPIRY_WEEKDAY = 3  # Monday=0 ... Thursday=3 ... Sunday=6


def resolve_mock_expiry(
    configured_expiry: str | None,
    timezone: str = "Asia/Kolkata",
    weekday: int = DEFAULT_WEEKLY_EXPIRY_WEEKDAY,
    today: date | None = None,
) -> str:
    """
    Resolve the expiry date string MOCK/Replay data sources should report.

    - If `configured_expiry` is a non-empty string (from `expiry` in
      config.yaml or the `EXPIRY` env var), it is returned verbatim — an
      explicit, operator-chosen pin.
    - Otherwise, computes the next occurrence of `weekday` on or after
      "today" (in `timezone`, or `today` if given for testing), so the mock
      chain always reports a live-looking, non-stale expiry instead of a
      hard-coded date that silently drifts into the past.

    Returned in the same "%d-%b-%Y" format used elsewhere in the mock/
    NSE-style chain payload (e.g. "28-Aug-2026"), so it's a drop-in for
    `records.expiryDates[0]`.
    """
    if configured_expiry:
        return str(configured_expiry)

    if today is None:
        today = today_ist(timezone)
    days_ahead = (weekday - today.weekday()) % 7
    expiry_date = today + timedelta(days=days_ahead)
    # Exchanges move an expiry that lands on a holiday to the previous
    # trading day; mirror that so MOCK never reports a holiday expiry.
    if not is_trading_day(expiry_date):
        expiry_date = previous_trading_day(expiry_date)
    return expiry_date.strftime("%d-%b-%Y")
