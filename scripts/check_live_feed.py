"""Read-only check of the real market-data feed. Run this first, during market hours, before trusting live data.

    python scripts/check_live_feed.py --seconds 60
    python scripts/check_live_feed.py --seconds 30 --underlying BANKNIFTY

It only READS: it never places an order, never writes to the database, never starts the app. It reports

  1. the date, the session (open / closed / holiday) and the next open
  2. credentials present (never printed) and when the access token expires
  3. that the provider connects and which error code Dhan returned if it does not
  4. the option chain: spot, expiry, how many strikes, and which fields Dhan actually sends
  5. Dhan's own IV and Greeks compared with this project's Black-Scholes calculator (the real test of the maths)
  6. the index tick stream: rate, longest gap
  7. how long a chain refresh takes

Exit code 0 only if nothing failed. Needs `pip install dhanhq` and DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (in .env).
Real market data is licensed: this is for your own testing. See docs/LIVE_DATA.md before showing it to anyone else.
"""

from __future__ import annotations

import argparse
import copy
import os
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.market_calendar import holiday_name, is_trading_day  # noqa: E402
from src.models import OptionChainSnapshot  # noqa: E402
from src.option_chain.greeks import CalculatedGreeksEngine  # noqa: E402
from src.tick_validation import get_market_session_status  # noqa: E402
from src.timeutil import now_ist  # noqa: E402

FIELDS = {
    "last price": lambda s: s.call_ltp > 0 or s.put_ltp > 0,
    "bid/ask": lambda s: s.call_bid is not None or s.put_bid is not None,
    "open interest": lambda s: s.call_oi is not None or s.put_oi is not None,
    "previous-day OI": lambda s: s.call_oi_prev is not None or s.put_oi_prev is not None,
    "volume": lambda s: s.call_volume is not None or s.put_volume is not None,
    "implied volatility": lambda s: s.call_iv is not None or s.put_iv is not None,
    "delta": lambda s: s.call_delta is not None or s.put_delta is not None,
    "gamma": lambda s: s.call_gamma is not None or s.put_gamma is not None,
    "theta": lambda s: s.call_theta is not None or s.put_theta is not None,
    "vega": lambda s: s.call_vega is not None or s.put_vega is not None,
}


def field_coverage(chain: OptionChainSnapshot) -> dict[str, tuple[int, int]]:
    """{field: (strikes that have it, total strikes)}."""
    total = len(chain.strikes)
    return {name: (sum(1 for leg in chain.strikes if has(leg)), total) for name, has in FIELDS.items()}


def compare_greeks(chain: OptionChainSnapshot, rate: float = 0.065, dividend: float = 0.012) -> dict:
    """Compare the provider's IV/delta/gamma/theta/vega with this project's calculator, strike by strike.

    The provider's values are blanked on a copy, recomputed from prices alone, and the two sets compared on the
    strikes where both exist. Large, systematic gaps mean a unit or convention difference worth knowing about."""
    mine = copy.deepcopy(chain)
    for leg in mine.strikes:
        for side in ("call", "put"):
            for f in ("iv", "delta", "gamma", "theta", "vega"):
                setattr(leg, f"{side}_{f}", None)
    CalculatedGreeksEngine(rate, dividend).enrich(mine)
    result: dict[str, dict] = {}
    for f in ("iv", "delta", "gamma", "theta", "vega"):
        diffs, ratios = [], []
        for theirs, ours in zip(chain.strikes, mine.strikes):
            for side in ("call", "put"):
                a, b = getattr(theirs, f"{side}_{f}"), getattr(ours, f"{side}_{f}")
                if a is None or b is None:
                    continue
                diffs.append(abs(a - b))
                if abs(b) > 1e-9:
                    ratios.append(a / b)
        if diffs:
            result[f] = {
                "pairs": len(diffs),
                "mean_abs_diff": round(statistics.mean(diffs), 6),
                "max_abs_diff": round(max(diffs), 6),
                "median_ratio_provider_over_mine": round(statistics.median(ratios), 4) if ratios else None,
            }
    return result


def next_open_text() -> str:
    now = now_ist()
    day = now.date()
    start_h, start_m = 9, 15
    if is_trading_day(day) and (now.hour, now.minute) < (start_h, start_m):
        return f"today {day:%a %d %b} at 09:15 IST"
    day += timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return f"{day:%a %d %b %Y} at 09:15 IST"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=float, default=45, help="how long to watch the index tick stream")
    parser.add_argument("--underlying", default="NIFTY")
    args = parser.parse_args()

    failures: list[str] = []

    def check(ok: bool, message: str) -> None:
        print(("  ok    " if ok else "  FAIL  ") + message)
        if not ok:
            failures.append(message)

    now = now_ist()
    session = get_market_session_status(now)
    print(f"1. Date and session\n   now {now:%a %d %b %Y %H:%M:%S} IST; session {session}")
    if holiday_name(now.date()):
        print(f"   NSE holiday today: {holiday_name(now.date())}")
    if session != "OPEN":
        print(f"   The market is not open. Next open: {next_open_text()}. Live prices will be frozen or absent.")
    check(session == "OPEN", "market is open (run this again 09:15-15:30 IST on a trading day for a meaningful test)")

    os.environ["LIVE_DATA_ENABLED"] = "true"  # this script is an explicit, operator-run opt-in
    from src.data_sources.dhan import DHAN_UNDERLYING_IDS, token_expiry
    from src.data_sources.registry import build_provider

    print("\n2. Credentials")
    from src.data_sources.dhan import _ensure_env_loaded

    _ensure_env_loaded()
    check(bool(os.getenv("DHAN_CLIENT_ID")), "DHAN_CLIENT_ID is set")
    check(bool(os.getenv("DHAN_ACCESS_TOKEN")), "DHAN_ACCESS_TOKEN is set")
    expiry = token_expiry(os.getenv("DHAN_ACCESS_TOKEN"))
    if expiry is None:
        print("   token expiry: unknown (not a readable JWT). Dhan tokens normally last 24 hours; check yours.")
    else:
        left = expiry - now_ist()
        check(left.total_seconds() > 3600, f"access token valid until {expiry:%a %d %b %H:%M} IST ({left.total_seconds() / 3600:.1f} h left)")

    print("\n3. Connection")
    check(args.underlying.upper() in DHAN_UNDERLYING_IDS, f"{args.underlying.upper()} has a Dhan security id ({sorted(DHAN_UNDERLYING_IDS)})")
    try:
        provider = build_provider({"market_data_provider": "dhan", "underlying": args.underlying, "poll_interval_seconds": 5})
    except Exception as exc:  # the message is credential-free by construction
        check(False, f"could not start the Dhan provider: {exc}")
        return 1
    check(provider.status().value != "ERROR", f"provider status {provider.status().value}" + (f" ({provider.status_detail()})" if provider.status_detail() else ""))
    expiries = provider.list_expiries()
    check(bool(expiries), f"expiries available: {', '.join(expiries[:4]) or 'none'}")

    print("\n4. Option chain")
    started = time.perf_counter()
    try:
        chain = provider.get_option_chain()
    except Exception as exc:
        check(False, f"option chain fetch failed: {exc}")
        provider.disconnect()
        return 1
    fetch_ms = (time.perf_counter() - started) * 1000
    print(f"   {chain.underlying} spot {chain.underlying_ltp}, expiry {chain.expiry}, {len(chain.strikes)} strikes, fetched in {fetch_ms:.0f} ms")
    check(len(chain.strikes) > 10 and chain.underlying_ltp > 0, "chain has strikes and a spot price")
    for name, (have, total) in field_coverage(chain).items():
        print(f"   {name:<20} {have}/{total} strikes")

    print("\n5. Provider Greeks vs this project's calculator")
    diffs = compare_greeks(chain)
    if not diffs:
        print("   the provider sent no Greeks/IV to compare (the calculator will fill them)")
    for f, d in diffs.items():
        print(f"   {f:<6} pairs {d['pairs']:>3}  mean |diff| {d['mean_abs_diff']:<10} max |diff| {d['max_abs_diff']:<10} provider/mine median {d['median_ratio_provider_over_mine']}")
    if "iv" in diffs:
        check(diffs["iv"]["mean_abs_diff"] < 3.0, "IV agrees within 3 volatility points on average (a gap near 100x or 0.01x would mean a unit mismatch)")

    print(f"\n6. Index tick stream ({args.seconds:.0f} s)")
    stamps: list[float] = []
    deadline = time.time() + args.seconds
    for tick in provider.stream_ticks():
        stamps.append(time.time())
        if time.time() >= deadline:
            break
    provider.disconnect()
    if len(stamps) >= 2:
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        print(f"   {len(stamps)} ticks, {len(stamps) / args.seconds:.2f}/s, median gap {statistics.median(gaps):.2f} s, longest gap {max(gaps):.2f} s")
        check(max(gaps) < 15, "no gap longer than 15 s")
    else:
        check(False, f"received {len(stamps)} ticks in {args.seconds:.0f} s (expected a steady stream while the market is open)")

    print("\nsummary")
    print("  " + ("all checks passed" if not failures else f"{len(failures)} check(s) failed:\n   - " + "\n   - ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
