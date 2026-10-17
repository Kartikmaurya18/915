# Real market data: how to turn it on, test it, and turn it off

Everything runs on **simulated (mock) prices by default**. Real data is opt-in, off unless you set one variable, and this page is
the checklist for doing that safely. Trading stays a **simulation** either way: no real order is ever placed.

## Before you start: licensing (this is not a technical step)

A data subscription lets *you* see prices. It does **not** by itself let you show them to other people.

| Use | Usually needs |
|---|---|
| You, testing on your own machine | Your own Dhan Data API subscription |
| Other people see the prices (a website, even free) | Redistribution / display terms from the data vendor **and** the exchange (NSE). Check Dhan's terms and NSE's market-data policy first. |

Until you have confirmed that, keep real data to your own machine and your own login. The app logs a warning every time live data is enabled.
Nothing here is legal advice; the licensing question is yours to settle with the vendor.

## One-time setup

1. **Dhan:** in your DhanHQ account, subscribe to **Data APIs**. Without it every data call fails with error `806` ("Data APIs not subscribed").
2. **Install the SDK (pinned):** `pip install -r requirements-live.txt`. It is `dhanhq==2.0.2` **on purpose**: the current release (2.3.0) changed the
   client constructor and removed the market-feed class this code uses, so "just upgrade" breaks it.
3. **Credentials:** put `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in `.env` (it is git-ignored). Never commit them. They are only read server-side.
4. **Token lifetime:** Dhan access tokens normally last about 24 hours. Generate a fresh one the morning you test. The app reads the token's expiry,
   warns on screen in its last hour, and refuses to start with one that has already expired.

## Market hours (India Standard Time)

| | |
|---|---|
| Pre-open | 09:00 - 09:15 (no continuous trading) |
| Continuous session | **09:15 - 15:30**, Monday to Friday |
| Closed | Saturdays, Sundays and NSE holidays (`data/nse_holidays.json`) |

Dates around now, from that file: **Mon 19 Oct 2026 is a normal trading day; Tue 20 Oct 2026 is a holiday (Dussehra)**; then 10 Nov (Diwali-Balipratipada) and
24 Nov. Check NSE's own calendar as well; the file is a static copy.

Outside the session a real feed is frozen or silent. The app says so on screen ("The market has closed for the day. Prices shown are from the last session")
instead of calling it a broken feed.

## Test it, in this order

**1. Check the feed without running the app** (read-only; never places an order, never writes to the database):

```bash
python scripts/check_live_feed.py --seconds 60
```

It reports the session, your credentials (never printed) and token expiry, the connection and any Dhan error code explained in words, the option chain
(strikes, expiry, which fields Dhan actually sends), a comparison of **Dhan's own IV and Greeks against this project's calculator**, the tick rate
and the longest gap. Exit code 0 means every check passed. Run it **between 09:15 and 15:30 on a trading day**; outside that, "market is open" fails by design.

Read the Greeks comparison: a median provider/mine ratio near 1.0 means the units and conventions agree. A ratio near 100 or 0.01 on IV means a unit
mismatch to fix before trusting the numbers.

**2. Run the app against the real feed (your machine only):**

```bash
export LIVE_DATA_ENABLED=true MARKET_DATA_PROVIDER=dhan
python -m uvicorn src.api.server:app --port 8000     # or the ingestor, as in docs/DEPLOYMENT.md
```

**3. Watch these, at these times:**

| When | What to check |
|---|---|
| 09:00 | Banner says the market has not opened; nothing is mislabelled as live |
| 09:15 | Feed turns live, ticks flow, the option chain fills, IV and Greeks appear, ATM is sensible |
| Mid-session | Chain refreshes every few seconds (Dhan allows one option-chain call per 3 s); `chain_age_seconds` stays low; compare a few LTPs with another screen |
| Unplug the network for 30 s | "Reconnecting" banner appears, then clears; no silent stale prices |
| 15:30 | Banner changes to "market has closed"; no "feed stale" alarm |
| After | Candles and chain snapshots were stored; History page shows the day |

**What counts as a fail:** prices that disagree with another trusted screen by more than a tick or two; a banner that never clears; ticks arriving in
bursts after a gap (backlog); IV unit mismatch; an error you cannot explain from the message on screen.

## Turning it off (rollback)

Unset `LIVE_DATA_ENABLED` (or set it to `false`) and restart. The deployment is back on mock data. Nothing else needs to change.

## What the code does to protect you

| Risk | Behaviour |
|---|---|
| Real data switched on by accident | Off unless `LIVE_DATA_ENABLED` is set; logs a licensing warning when on |
| A failed provider quietly falling back to mock prices | Never: a provider that cannot start is shown as `ERROR` with the reason, with no data |
| Expired / invalid token mid-session | Dhan codes 807/808/809 are recognised, shown as a message, and the status turns `ERROR` even while the index still ticks |
| Old option chain shown as current | A cached chain is reused only for a few seconds after a failed refresh, never after an auth error; the chain's age is shown separately from the index tick |
| The engine falling behind the feed | The slow chain call used to be paid once per tick while ticks queued up (a lag that only grows); the provider now keeps only the newest tick |
| A closed market reported as a broken feed | Session is judged by the clock for real feeds; closed is "closed", not "stale" |
| Wrong SDK version | The error names the pinned version to install |

## Known gaps (be aware, not blockers for a first test)

- The index price comes over Dhan's WebSocket, but the **option chain is polled over REST** (about every 3-5 seconds), so the chain is not tick-by-tick.
  Per-contract streaming would need subscriptions for each option; that is not built.
- The tick timestamp is when this server received it, not the exchange's trade time, so **feed-to-backend delay cannot be measured** yet.
- Only the nearest expiry is served.
- The NSE provider remains disabled; Dhan is the only real source.
- Dhan's response field names were checked against their documentation; the exact JSON of a live response has not been seen here, so the first
  market-hours run of `check_live_feed.py` is also the first real validation of the field mapping.
