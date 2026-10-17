# Options terminal features

What was added to the existing app (nothing was rebuilt; the paper-trading engine, APIs, auth and database behaviour are unchanged), how it works,
the conventions it uses, and an honest status against the original brief. Trading is a **simulation** everywhere; no real order is ever placed.

## What's new

| Feature | Where | How it works |
|---|---|---|
| **IV and Greeks** (Delta, Gamma, Theta, Vega) | `src/option_chain/analytics.py`, `greeks.py` | Black-Scholes-Merton (European, as NSE index options are). A provider's own values always win; the calculator only fills gaps. A value with no honest answer (no time value, expired, crossed quotes) stays empty rather than invented. |
| **Bid/ask, OI change** | chain rows | Bid/ask as sent by the provider. OI change is today's OI minus the previous session's close, shown only when the provider sends both. |
| **Option chain table** | `web/src/components/OptionChainCard.tsx`, `lib/chain.ts` | 11 selectable columns (remembered per device), sortable headers, strike search, window around the money, in-the-money shading (also announced in text), memoised rows so a tick re-renders only the strikes that changed. |
| **Strategy builder** | `/strategy`, `web/src/lib/strategy.ts` | 11 presets (long/short call and put, long/short straddle and strangle, bull call spread, bear put spread, iron condor) built on the live chain, editable leg by leg. Net premium, break-evens, max profit/loss, expiry payoff chart, and a model "today" curve. Placed as ordinary, idempotent paper orders. |
| **Strangle chart** | `src/option_chain/pair_candles.py`, `/api/pair-candles` | Exact OHLC of CE@A + PE@B. The high of a sum is not the sum of the highs, so the summed price is fed tick by tick. Pairs are tracked from the first request (bounded to 32), so a pair has **no history before then** and the page says when tracking began. |
| **Workspace** | `/workspace` | 1, 2 or 4 chart panes (price candles, straddle at a strike, strangle), each chosen independently, plus named watchlists. Layout and lists persist in the browser. |
| **Keyboard shortcuts** | `web/src/lib/shortcuts.ts` | `Ctrl K` or `/` search, `N` new order, `G` then `O T S W H R Q` navigate, `?` lists them. Ignored while typing and while a dialog is open. |
| **Feed-health messages** | `web/src/lib/feedHealth.ts` | A visible banner (not just the header pill) for: stale or dropped connection, provider error with its reason, market closed, option chain no longer refreshing, data token about to expire. |

## Conventions (the same everywhere, so the UI never converts)

| Quantity | Unit |
|---|---|
| IV | percentage points (`14.2` means 14.2 %), as Dhan sends it |
| Delta | per 1.00 move of the underlying (calls 0 to 1, puts -1 to 0) |
| Gamma | change in delta per 1.00 move |
| Theta | per **calendar day** (negative for a long option) |
| Vega | per **1 volatility point** |
| Time to expiry | calendar years to **15:30 IST** on the expiry date |
| Rates | risk-free 6.5 % (`RISK_FREE_RATE`); dividend yield is derived from put-call parity at the strike nearest spot, falling back to 1.2 % (`DIVIDEND_YIELD`) |

IV is read from the **out-of-the-money** side of each strike (put-call parity makes both sides equal, and the OTM option carries the time value);
rounding is to display precision (IV 2 dp, delta 4, gamma 6, theta 2, vega 2) so tiny changes do not bloat every update.

## Three numbers a strategy shows, kept apart on purpose

| | Meaning | Exact? |
|---|---|---|
| **At expiry** (solid line, max profit/loss, break-evens) | Intrinsic value at settlement minus the premium, per leg. Piecewise linear, so every figure is computed exactly from the strikes. | Yes |
| **Today (model)** (dashed line) | Black-Scholes at each leg's current IV, across underlying levels. An estimate. | No: model |
| **Live P&L** (badge) | Each leg's current traded price against the price it was added at. | Yes, but "since added", not an account P&L |

Not modelled: brokerage, taxes, slippage, **margin** (an unlimited-loss strategy is flagged, because paper trading would not stop it for lack of funds)
and **lot sizes** (quantity is in units, as in the existing paper API).

## Status against the original brief

| # | Brief | Status |
|---|---|---|
| 1 | Inspect first; reuse; run tests before and after | **Done.** Baseline 544 backend and 285 web tests passed before any change; every step re-ran them. |
| 2 | Options chain for NIFTY, BANKNIFTY, FINNIFTY | **Partly.** Premiums, strikes, ATM/ITM/OTM, bid/ask, volume, OI, OI change, Delta/Gamma/Theta/Vega, IV, sort/filter/columns, automatic ATM: done. **One underlying per deployment**, and **only the nearest expiry**: not done (see below). |
| 3 | Straddle and strangle analytics | **Done**, with one limit: a strangle has no history before it was first requested. Expiry selection not done. |
| 4 | Trading workspace | **Mostly.** Multi-chart layouts, watchlists, candlesticks, live chain, shortcuts, strategy builder, payoff graphs, positions and P&L (existing page), connection and stale warnings: done. **Chart-based navigation (click a candle to jump)** not built. Chart render latency not measured. |
| 5 | Strategy builder | **Done**, all listed strategies. |
| 6 | Low-latency pipeline | **Mostly existing and verified.** Persistent WebSockets, incremental patches, in-memory state, bounded per-client queues with coalescing, reconnection, feed-health monitoring, persistence separated from live processing. **Sequence-gap detection at the provider feed:** not possible (the REST chain exposes no sequence numbers); per-topic gap detection and resync between server and browser exists. |
| 7 | Preserve the paper engine | **Done.** No API or schema change; idempotent orders; per-user isolation tests still pass; failed legs are retried with the same key. |
| 8 | Provider and permissions | **Partly.** Dhan's field names and error codes verified against its documentation, security ids against its public instrument master, SDK versions compared. **Not researched:** subscription pricing, historical-data permissions. Redistribution rights are **not assumed** (see [LIVE_DATA.md](LIVE_DATA.md)). |
| 9 | Performance and testing | **Measured:** per-stage compute latency, delivery latency and payload at 500 clients, slow clients, reconnects ([CAPACITY.md](CAPACITY.md)). **Not measured:** feed-to-backend delay (needs a real feed with exchange timestamps), chart render time, multi-hour soak. |
| 10 | Cloud and security | **Unchanged and documented** ([DEPLOYMENT.md](DEPLOYMENT.md)); nothing was provisioned. |
| 11 | Work incrementally | **Done**, one commit per step. |

### The two larger gaps, and what they would take

* **Several underlyings at once.** Gateways already subscribe to every topic channel, and the Dhan table already maps NIFTY, BANKNIFTY and FINNIFTY, so the
  data path is close. What is missing: a leader lease and control channel **per underlying** (today there is one), the paper exit worker filtered by
  underlying (otherwise one ingestor would mark another's positions), underlying-aware API routes, and a UI selector. It touches paper-trading
  correctness, so it should be its own verified phase against the real Docker stack rather than a quick change.
* **Choosing an expiry.** The provider is asked for one expiry. Serving more means one chain request per expiry (Dhan allows one every 3 seconds), a
  topic per expiry, and a selector. Worth doing together with the above.

## Reproduce the measurements

```bash
python scripts/bench_pipeline.py --ticks 2000 --greeks calculated     # per-stage compute latency, payload sizes
python scripts/loadtest_ws.py --clients 500 --duration 30 --batch     # delivery latency, as in CAPACITY.md
python scripts/check_live_feed.py --seconds 60                         # real feed, market hours only
```
