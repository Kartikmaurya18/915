from __future__ import annotations

import os
import threading
from datetime import date, datetime
from typing import Callable

from src.candles import CandleAggregator
from src.config import load_config
from src.data_quality import compute_staleness, default_stale_threshold_seconds
from src.data_sources import (
    PROVIDERS,
    REPLAY_SEQUENCES,
    ProviderStatus,
    build_provider,
    provider_key,
)
from src.data_sources.replay import DEFAULT_SEQUENCE
from src.execution import OrderRequest, build_execution_client
from src.logging import get_logger
from src.models import ATMResult, Candle, OptionChainSnapshot, PriceTick
from src.option_chain import (
    StrikeStraddleCandleEngine,
    find_atm_strike,
    find_atm_strike_delta,
)
from src.option_chain.greeks import build_greeks_engine
from src.paper_trading import PaperPosition, PaperTradeConfig, PaperTradingEngine
from src.risk import EXIT_MANUAL
from src.persistence import PersistenceStore, ensure_seeded
from src.price_calculator import get_calculator
from src.rrg import RRGCalculator, RRGConfig
from src.serializers import (
    _empty_paper_stats,
    _empty_pnl_summary,
    serialize_persisted_candle,
    summarize_persisted_paper_trades,
)
from src.tick_validation import get_market_session_status, validate_tick
from src.market_calendar import is_trading_day as is_nse_trading_day
from src.timeutil import now_ist, to_ist, today_ist, trading_date_str


def build_data_source(cfg: dict):
    """Backward-compatible alias — provider selection lives in
    src/data_sources/registry.py."""
    return build_provider(cfg)


UpdateCallback = Callable[[dict], None]


class MarketEngine:
    """Runs the market data loop and exposes thread-safe snapshots."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.underlying = self.cfg.get("underlying", "NIFTY")
        self.intervals = self.cfg.get("candle_intervals", [1, 5, 15])
        self.price_calc_method = self.cfg.get("price_calculator", "mock")
        self.log_level = self.cfg.get("log_level", "INFO")
        self.poll_interval = float(self.cfg.get("poll_interval_seconds", 5))
        self.stale_threshold_seconds = float(
            self.cfg.get(
                "stale_threshold_seconds", default_stale_threshold_seconds(self.poll_interval)
            )
        )
        # ATM selection: "delta" (Greeks-based, default) or "price" (nearest to spot).
        # Falls back to price-based automatically if a chain carries no Greeks.
        self.atm_method = str(self.cfg.get("atm_method", "delta")).lower()
        self.delta_threshold = float(self.cfg.get("delta_threshold", 0.5))
        self.session_start = self.cfg.get("session_start", "09:15")
        self.session_end = self.cfg.get("session_end", "15:30")
        # Which price feeds the candle aggregator/chart: "underlying" (spot,
        # default) or "combined_value" (ATM Call LTP + Put LTP). Pure display
        # choice — the CE+PE formula itself lives in the price calculator.
        self.candle_price_basis = str(self.cfg.get("candle_price_basis", "underlying")).lower()

        # Provider selection is centralized in the registry. fail_soft: a
        # provider that can't start (e.g. Dhan without credentials) yields an
        # ERROR-status placeholder so the app/UI still load — never MOCK data.
        self._source = build_provider(self.cfg, fail_soft=True)
        self._greeks = build_greeks_engine(self.cfg.get("greeks_source", "provider"))
        self._aggregator = CandleAggregator(
            symbol=self.underlying,
            intervals_minutes=self.intervals,
            timezone=self.cfg.get("timezone", "Asia/Kolkata"),
            session_start=self.session_start,
        )
        # Per-strike CE/PE/Straddle OHLC candles — any strike in the option
        # chain, not just ATM. Backs the ATM Straddle Chart's candlestick
        # view; a strike's series is fixed to that strike by construction
        # (see StrikeStraddleCandleEngine), so switching strikes in the UI
        # is just switching which strike's already-tracked series is read.
        self._strike_candle_engine = StrikeStraddleCandleEngine(
            intervals_minutes=self.intervals,
            timezone=self.cfg.get("timezone", "Asia/Kolkata"),
            session_start=self.session_start,
        )
        self._price_calculator = get_calculator(self.price_calc_method)
        self._logger = get_logger(self.log_level)

        self._logger.log_system_event(
            "provider_selected",
            {
                "provider": self._source.provider_name,
                "underlying": self.underlying,
                "provider_class": self._source.__class__.__name__,
                "status": self._source.status().value,
            }
        )

        # Initialize RRG calculator if enabled
        self._rrg_calculator = None
        if self.cfg.get("enable_rrg", False):
            rrg_config = RRGConfig(
                benchmark_symbol=self.underlying,
                timeframe=self.cfg.get("rrg_timeframe", "1min"),
                rolling_window=self.cfg.get("rrg_rolling_window", 14),
                momentum_period=self.cfg.get("rrg_momentum_period", 1),
                option_strikes=self.cfg.get("rrg_strikes"),
                expiry=self.cfg.get("rrg_expiry"),
            )
            self._rrg_calculator = RRGCalculator(rrg_config)

        # Paper trading (simulation-only) position engine. Configurable TP/SL,
        # defaults to TP=10%, SL=5% until overridden via config or the API.
        # take_profit_amount/stop_loss_amount (₹) are an additional, optional
        # trigger — None/unset (the default) leaves behavior exactly as
        # before; never hard-coded (see src/paper_trading.py::PaperTradeConfig).
        paper_cfg = self.cfg.get("paper_trading", {}) or {}
        tp_amount = paper_cfg.get("take_profit_amount")
        sl_amount = paper_cfg.get("stop_loss_amount")
        self._paper_trading = PaperTradingEngine(
            PaperTradeConfig(
                take_profit_percent=float(paper_cfg.get("take_profit_percent", 10.0)),
                stop_loss_percent=float(paper_cfg.get("stop_loss_percent", 5.0)),
                take_profit_amount=float(tp_amount) if tp_amount is not None else None,
                stop_loss_amount=float(sl_amount) if sl_amount is not None else None,
            )
        )
        # Every open/close goes through the execution interface. Only paper
        # execution exists; see src/execution/.
        self._execution = build_execution_client(self.cfg.get("execution_mode", "paper"), self._paper_trading)

        # Persistence (SQLite) for completed candles and option chain/ATM snapshots.
        # Purely additive: candle/ATM logic has no dependency on this being enabled.
        persistence_cfg = self.cfg.get("persistence", {}) or {}
        self._persistence: PersistenceStore | None = None
        self._chain_persist_interval = float(
            persistence_cfg.get("option_chain_snapshot_interval_seconds", 5)
        )
        self._last_chain_persist_at: datetime | None = None
        if persistence_cfg.get("enabled", False):
            db_path = persistence_cfg.get("db_path", "data/market_data.db")
            # Restore a committed seed of historical MOCK data if db_path
            # doesn't exist yet (e.g. a fresh Railway container with no
            # persistent disk) — no-op if db_path already has data (always
            # true on localhost) or no seed is configured. See
            # ensure_seeded()'s docstring for the full why.
            try:
                ensure_seeded(db_path, persistence_cfg.get("seed_db_path"))
            except Exception as e:
                self._logger.log_error(e, {"context": "persistence_seed"})
            try:
                self._persistence = PersistenceStore(db_path)
            except Exception as e:
                self._logger.log_error(e, {"context": "persistence_init"})
                try:
                    self._persistence = PersistenceStore("/tmp/market_data.db")
                except Exception:
                    self._persistence = None

        # RLock, not Lock: _build_snapshot() calls get_settings(), which
        # re-acquires this same lock from the thread that already holds it
        # (both the tick worker's per-tick block and get_snapshot()).
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_error: Exception | None = None
        self._subscribers: list[UpdateCallback] = []

        self._latest_tick: PriceTick | None = None
        self._latest_atm: ATMResult | None = None
        self._latest_chain: OptionChainSnapshot | None = None
        self._latest_calculated_prices: dict | None = None
        self._latest_rrg_snapshot = None
        self._latest_market_session: str | None = None
        self._tick_count = 0

        # Reliability: timestamp validation (duplicate/out-of-order rejection).
        self._last_accepted_timestamp: datetime | None = None
        self._duplicate_tick_count = 0
        self._rejected_tick_count = 0

    @property
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def subscribe(self, callback: UpdateCallback) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: UpdateCallback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._worker_error = None
        self._worker = threading.Thread(target=self._tick_worker, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=2)
            self._worker = None

    def close(self) -> None:
        """Release resources (persistence DB handle). Safe to call after stop()."""
        self.stop()
        if self._persistence is not None:
            self._persistence.close()
            self._persistence = None

    def get_persisted_candles(self, interval_minutes: int, limit: int = 200) -> list[dict]:
        """Completed candles read back from the persistence store (empty if disabled)."""
        if self._persistence is None:
            return []
        return self._persistence.get_recent_candles(self.underlying, interval_minutes, limit)

    def get_persisted_option_chain_snapshots(self, limit: int = 200) -> list[dict]:
        """Recent option chain/ATM snapshots read back from the persistence store."""
        if self._persistence is None:
            return []
        return self._persistence.get_recent_option_chain_snapshots(self.underlying, limit)

    def tick_once(self) -> dict | None:
        """Process one mock tick on-demand (used in serverless environments)."""
        try:
            chain = self._source.fetch_option_chain() if hasattr(self._source, "fetch_option_chain") else None
            spot = float(chain["records"]["underlyingValue"]) if chain else self._source.get_underlying_ltp()
            from src.timeutil import now_ist
            tick = PriceTick(
                symbol=self.underlying,
                price=spot,
                timestamp=now_ist(self.cfg.get("timezone", "Asia/Kolkata")),
                provider=self._source.provider_name,
                exchange=getattr(self._source, "spec", None).exchange if hasattr(self._source, "spec") else "NSE",
            )
            return self._process_tick_data(tick)
        except Exception as e:
            self._logger.log_error(e, {"context": "tick_once"})
            return None

    def get_snapshot(self) -> dict:
        if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            self.tick_once()
        with self._lock:
            return self._build_snapshot(completed=[])

    # -- Historical Date/Session selector -------------------------------
    # Everything below is read-only and additive: it only reads back what
    # the live pipeline above already persists, grouped by Asia/Kolkata
    # trading date (see src/timeutil.py). None of it touches live state.

    def get_persisted_strike_candles_for_date(self, strike: float, trading_date: str) -> dict | None:
        """CE/PE/Straddle OHLC candles for one strike on one past trading
        date — the historical counterpart of get_strike_straddle_candles(),
        used when switching strikes within an already-selected historical
        session without recomputing the whole session bundle."""
        if self._persistence is None:
            return None
        by_interval = self._persistence.get_strike_candles_for_date(strike, self.intervals, trading_date)
        if not any(rows for legs in by_interval.values() for rows in legs.values()):
            return None
        return {
            "strike": strike,
            "by_interval": {
                interval: {leg: [serialize_persisted_candle(r) for r in rows] for leg, rows in legs.items()}
                for interval, legs in by_interval.items()
            },
        }

    def get_available_trading_dates(self) -> dict:
        """Every *valid NSE trading day* with persisted data (newest
        first), plus today's Asia/Kolkata date — backs the Date/Session
        selector's Previous/Next/date-picker navigation and lets the
        frontend tell 'today, still live' apart from 'a past session,
        read-only'.

        Persisted dates are filtered through market_calendar.is_trading_day
        so a weekend or a known NSE holiday is never offered as a
        selectable historical session, even if a dev/test run happened to
        record MOCK ticks on one — selectability is the intersection of
        "has data" and "is a real trading day", not either alone."""
        raw_dates = self._persistence.list_trading_dates(self.underlying) if self._persistence else []
        dates = [d for d in raw_dates if is_nse_trading_day(date.fromisoformat(d))]
        return {"dates": dates, "today": today_ist().isoformat()}

    def get_historical_session(self, trading_date: str, strike: float | None = None) -> dict:
        """A read-only bundle for one past Asia/Kolkata trading date: candles
        (every configured interval), option chain snapshots through the day,
        one strike's CE/PE/Straddle candles (ATM-at-close if `strike` is
        omitted), and that day's paper trading history/P&L. Empty/zeroed
        sub-sections (not an error) if persistence is off or nothing was
        recorded for this date — see `has_data`."""
        # A weekend/NSE-holiday date is never a selectable historical
        # session (see get_available_trading_dates) — reported as "no
        # data" even if some MOCK data happens to be persisted under it
        # (e.g. a dev/test run), so a hand-typed ?date= can't surface a
        # date the Date/Session selector itself would never offer. `today`
        # is exempt: live MOCK mode runs regardless of the calendar.
        is_valid_trading_date = trading_date == today_ist().isoformat() or is_nse_trading_day(
            date.fromisoformat(trading_date)
        )

        if self._persistence is None or not is_valid_trading_date:
            return {
                "trading_date": trading_date,
                "has_data": False,
                "underlying": {"symbol": self.underlying},
                "candles": {},
                "option_chain_snapshots": [],
                "available_strikes": [],
                "straddle_candles": None,
                "paper_trading": {"history": [], "stats": _empty_paper_stats(), "pnl_summary": _empty_pnl_summary()},
            }

        candles = {
            str(interval): [
                serialize_persisted_candle(row)
                for row in self._persistence.get_candles_for_date(self.underlying, interval, trading_date)
            ]
            for interval in self.intervals
        }
        chain_snapshots = self._persistence.get_option_chain_snapshots_for_date(self.underlying, trading_date)
        # Defense in depth: normalize each row's timestamp to an IST-offset
        # ISO string here too, not just at write time — a DB written before
        # this normalization existed could still hold offset-less strings.
        for row in chain_snapshots:
            if row.get("timestamp"):
                row["timestamp"] = to_ist(datetime.fromisoformat(row["timestamp"])).isoformat()
        available_strikes = self._persistence.list_strikes_for_date(trading_date)

        last_snapshot = chain_snapshots[-1] if chain_snapshots else None
        focus_strike = strike
        if focus_strike is None and last_snapshot is not None:
            focus_strike = last_snapshot.get("atm_strike")
        if focus_strike is None and available_strikes:
            focus_strike = available_strikes[len(available_strikes) // 2]

        straddle_candles = None
        if focus_strike is not None:
            by_interval = self._persistence.get_strike_candles_for_date(
                float(focus_strike), self.intervals, trading_date
            )
            straddle_candles = {
                "strike": float(focus_strike),
                "by_interval": {
                    interval: {leg: [serialize_persisted_candle(r) for r in rows] for leg, rows in legs.items()}
                    for interval, legs in by_interval.items()
                },
            }

        trades = self._persistence.get_paper_trades_for_date(trading_date)
        paper_trading = summarize_persisted_paper_trades(trades)

        has_data = bool(candles.get(str(self.intervals[0])) or chain_snapshots or trades)
        underlying_prices = [row["underlying_ltp"] for row in chain_snapshots if row.get("underlying_ltp") is not None]

        return {
            "trading_date": trading_date,
            "has_data": has_data,
            "underlying": {
                "symbol": self.underlying,
                "open": underlying_prices[0] if underlying_prices else None,
                "high": max(underlying_prices) if underlying_prices else None,
                "low": min(underlying_prices) if underlying_prices else None,
                "close": underlying_prices[-1] if underlying_prices else None,
                "last_updated": last_snapshot["timestamp"] if last_snapshot else None,
            },
            "candles": candles,
            "option_chain_snapshots": chain_snapshots,
            "available_strikes": available_strikes,
            "straddle_candles": straddle_candles,
            "paper_trading": paper_trading,
        }

    # -- Paper trading (simulation only, never a real order) ----------------

    def _current_leg_price(self, option_type: str, strike: float):
        """Look up the current CE/PE premium for `strike` from the latest chain."""
        chain = self._latest_chain
        if chain is None:
            return None
        leg = chain.get_leg(strike)
        return leg.ltp(option_type) if leg is not None else None

    def open_paper_position(
        self,
        option_type: str,
        strike: float | None = None,
        quantity: int = 50,
        side: str = "BUY",
        expiry: str | None = None,
        underlying: str | None = None,
    ) -> PaperPosition:
        """Open a SIMULATED position on a contract from the current Option
        Chain (the source of truth for contract selection). `expiry`/
        `underlying`, when given, must match the chain — so a selection made
        against one chain can't silently fill against a different contract."""
        option_type = (option_type or "").upper()
        if option_type not in ("CE", "PE"):
            raise ValueError("option_type must be 'CE' or 'PE'.")
        with self._lock:
            atm = self._latest_atm
            tick = self._latest_tick
            chain = self._latest_chain
            if chain is None:
                raise ValueError("Option chain not available yet; cannot open a paper position.")
            if underlying is not None and underlying.upper() != chain.underlying.upper():
                raise ValueError(f"Underlying {underlying} is not the current chain's ({chain.underlying}).")
            if expiry is not None and expiry != chain.expiry:
                raise ValueError(f"Expiry {expiry} is not the current chain's ({chain.expiry}).")
            if strike is None:
                # Defensive fallback only — the UI always sends the strike
                # explicitly from the selected Option Chain contract.
                if atm is None:
                    raise ValueError("ATM strike not available yet; cannot open a paper position.")
                strike = atm.strike
            quote = chain.quote(strike, option_type)
            if quote is None:
                raise ValueError(f"Strike {strike} not found in the current option chain.")
            timestamp = tick.timestamp if tick else now_ist()
        result = self._execution.place_order(
            OrderRequest(
                contract=quote.contract,
                side=(side or "BUY").upper(),
                quantity=quantity,
                reference_price=quote.ltp,
                timestamp=timestamp,
            )
        )
        if not result.accepted:
            raise ValueError(result.message or "Order rejected.")
        return result.position

    def close_paper_position(self, position_id: str) -> PaperPosition | None:
        with self._lock:
            tick = self._latest_tick
            timestamp = tick.timestamp if tick else now_ist()
            open_positions = {p.id: p for p in self._paper_trading.get_open_positions()}
            position = open_positions.get(position_id)
            if position is None:
                return None
            price = self._current_leg_price(position.option_type, position.strike)
            if price is None:
                price = position.current_price
        result = self._execution.close_position(position_id, price, timestamp, EXIT_MANUAL)
        closed = result.position if result.accepted else None
        if closed is not None:
            self._persist_paper_trade(closed)
        return closed

    def _persist_paper_trade(self, position: PaperPosition) -> None:
        """Persist a just-closed simulated position for the historical Date/
        Session selector's Paper Trading tab. No-op if persistence is off."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_paper_trade(position)
        except Exception as e:
            self._logger.log_error(e, {"context": "persistence_write_paper_trade"})

    def set_paper_config(
        self,
        take_profit_percent: float | None = None,
        stop_loss_percent: float | None = None,
        take_profit_amount: float | None = None,
        stop_loss_amount: float | None = None,
    ) -> PaperTradeConfig:
        return self._paper_trading.set_config(
            take_profit_percent, stop_loss_percent, take_profit_amount, stop_loss_amount
        )

    def get_paper_state(self) -> dict:
        return self._paper_trading.to_state_dict()

    # -- Settings / logs / runtime reconfiguration ---------------------------

    def get_recent_logs(self, limit: int = 200, level: str | None = None) -> list[dict]:
        """Recent structured log entries (in-memory ring buffer), newest first."""
        return self._logger.get_recent(limit, level)

    def get_settings(self) -> dict:
        """Current runtime-adjustable settings for the Settings tab."""
        with self._lock:
            source = self._source
            chain = self._latest_chain
            data_source = provider_key(self.cfg)
            expiry = chain.expiry if chain is not None else getattr(source, "expiry", None)
            replay_sequence = getattr(source, "sequence_name", DEFAULT_SEQUENCE)
            replay_speed = getattr(source, "speed", 1.0)
            atm_method = self.atm_method
            delta_threshold = self.delta_threshold
            candle_price_basis = self.candle_price_basis
        paper_cfg = self._paper_trading.get_config()
        return {
            "data_source": data_source,
            "market_data_provider": data_source,
            "available_providers": sorted(PROVIDERS),
            "greeks_source": self._greeks.name,
            "execution_mode": self._execution.mode,
            "expiry": expiry,
            "atm_method": atm_method,
            "delta_threshold": delta_threshold,
            "take_profit_percent": paper_cfg.take_profit_percent,
            "stop_loss_percent": paper_cfg.stop_loss_percent,
            "candle_price_basis": candle_price_basis,
            "replay": {
                "available_sequences": sorted(REPLAY_SEQUENCES.keys()),
                "sequence": replay_sequence,
                "speed": replay_speed,
            },
        }

    def set_atm_config(self, atm_method: str | None, delta_threshold: float | None) -> dict:
        """Update the ATM selection method ("delta" or "price") and/or the
        Delta-closest-to threshold. Either argument may be omitted (None) to
        leave it unchanged."""
        if atm_method is not None:
            atm_method = str(atm_method).lower()
            if atm_method not in ("delta", "price"):
                raise ValueError(f"Unknown atm_method: {atm_method!r}. Use 'delta' or 'price'.")
        if delta_threshold is not None:
            if not (0 < delta_threshold < 1):
                raise ValueError("delta_threshold must be between 0 and 1 (exclusive).")
        with self._lock:
            if atm_method is not None:
                self.atm_method = atm_method
            if delta_threshold is not None:
                self.delta_threshold = float(delta_threshold)
        return self.get_settings()

    def set_candle_price_basis(self, candle_price_basis: str) -> dict:
        """Switch which price feeds the candle aggregator/chart: "underlying"
        (spot) or "combined_value" (ATM Call LTP + Put LTP). Pure display
        choice — never touches the CE+PE formula itself."""
        basis = str(candle_price_basis).lower()
        if basis not in ("underlying", "combined_value"):
            raise ValueError(
                f"Unknown candle_price_basis: {candle_price_basis!r}. "
                "Use 'underlying' or 'combined_value'."
            )
        with self._lock:
            self.candle_price_basis = basis
        return self.get_settings()

    def reconfigure_source(
        self, data_source: str, sequence: str | None = None, speed: float | None = None
    ) -> dict:
        """Switch the live feed (e.g. into deterministic MOCK Replay/Test
        Mode) without losing candle/paper-trading state — only the worker
        thread and `self._source` are replaced; the aggregator, paper
        trading engine, and persisted history all live on `self` and are
        untouched."""
        source_name = str(data_source).lower()
        if source_name not in PROVIDERS:
            raise ValueError(
                f"Unknown data_source: {data_source!r}. Use one of {sorted(PROVIDERS)}."
            )
        new_cfg = dict(self.cfg)
        new_cfg["data_source"] = new_cfg["market_data_provider"] = source_name
        if sequence is not None:
            new_cfg["replay_sequence"] = sequence
        if speed is not None:
            new_cfg["replay_speed"] = speed
        # Not fail_soft: a provider that can't start is reported to the
        # caller (HTTP 400) and the current feed keeps running.
        new_source = build_provider(new_cfg)

        was_running = self.is_running
        if was_running:
            self.stop()
        old_source = self._source
        with self._lock:
            self._source = new_source
            self.cfg = new_cfg
        disconnect = getattr(old_source, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception as exc:
                self._logger.log_error(exc, {"context": "data_source_disconnect"})
        self._logger.log_system_event(
            "data_source_reconfigured",
            {"data_source": source_name, "provider_class": new_source.__class__.__name__},
        )
        if was_running:
            self.start()
        return self.get_settings()

    def _process_tick_data(self, tick: PriceTick) -> dict:
        self._last_accepted_timestamp = tick.timestamp
        self._latest_market_session = get_market_session_status(
            tick.timestamp,
            session_start=self.session_start,
            session_end=self.session_end,
            timezone=self.cfg.get("timezone", "Asia/Kolkata"),
        )

        # Log incoming price tick
        self._logger.log_price_tick(tick, {"source": self._source.provider_name})

        try:
            # Normalized chain straight from the provider, then Greeks
            # (provider-supplied today; see option_chain/greeks.py).
            chain = self._greeks.enrich(self._source.get_option_chain())

            # ATM selection: Delta-based (Greeks, default) with automatic
            # fallback to price-based if the chain carries no Greeks;
            # "price" mode uses price-based directly. Either way, the
            # price-based strike is attached for on-screen comparison.
            price_atm = find_atm_strike(chain)
            if self.atm_method == "delta":
                atm = find_atm_strike_delta(chain, delta_threshold=self.delta_threshold)
                if atm is None:
                    atm = price_atm
            else:
                atm = price_atm
            if atm is not None and atm.price_based_strike is None:
                atm.price_based_strike = price_atm.strike if price_atm else None

            # Log ATM update
            if atm:
                self._logger.log_atm_update(
                    {
                        "strike": atm.strike,
                        "call_ltp": atm.call_ltp,
                        "put_ltp": atm.put_ltp,
                        "straddle_premium": atm.straddle_premium,
                    }
                )
        except Exception as e:
            self._logger.log_error(e, {"context": "option_chain_fetch"})
            atm = self._latest_atm
            chain = self._latest_chain

        with self._lock:
            self._latest_tick = tick
            self._latest_atm = atm
            self._latest_chain = chain
            self._latest_calculated_prices = self._price_calculator.calculate(atm) if atm else None

            # Log calculated prices
            if self._latest_calculated_prices:
                self._logger.log_calculated_prices(self._latest_calculated_prices)

            # Update RRG calculator if enabled
            if self._rrg_calculator and chain:
                try:
                    self._rrg_calculator.update(tick, chain)
                    self._latest_rrg_snapshot = self._rrg_calculator.calculate_rrg()
                except Exception as e:
                    self._logger.log_error(e, {"context": "rrg_calculation"})

            # Mark paper (simulated) positions to market and auto-exit
            # on TP/SL. Never touches a real order — see paper_trading.py.
            try:
                paper_exits = self._paper_trading.update(chain, tick.timestamp)
                for pos in paper_exits:
                    self._logger.log_system_event(
                        "paper_position_exit",
                        {
                            "id": pos.id,
                            "option_type": pos.option_type,
                            "strike": pos.strike,
                            "exit_reason": pos.exit_reason,
                            "pnl": pos.pnl,
                            "pnl_percent": pos.pnl_percent,
                            "simulated": True,
                        },
                    )
                    self._persist_paper_trade(pos)
            except Exception as e:
                self._logger.log_error(e, {"context": "paper_trading_update"})

            self._tick_count += 1
            candle_tick = tick
            if self.candle_price_basis == "combined_value" and self._latest_calculated_prices:
                combined_value = self._latest_calculated_prices.get("combined_value")
                if combined_value is not None:
                    candle_tick = PriceTick(
                        symbol=tick.symbol,
                        price=combined_value,
                        timestamp=tick.timestamp,
                        volume=tick.volume,
                    )
            completed = self._aggregator.process_tick(candle_tick)
            completed_strike_candles = self._strike_candle_engine.process(chain, tick.timestamp)

            # Log completed candles
            for candle in completed:
                self._logger.log_candle_completed(candle)

            # Persist (optional — see `persistence.enabled` in config).
            # Every write here is additionally grouped by its
            # Asia/Kolkata trading date (see src/timeutil.py), which
            # is what backs the historical Date/Session selector.
            if self._persistence is not None:
                try:
                    for candle in completed:
                        self._persistence.save_candle(candle)
                    for candle in completed_strike_candles:
                        self._persistence.save_candle(candle)
                    if chain is not None and (
                        self._last_chain_persist_at is None
                        or (tick.timestamp - self._last_chain_persist_at).total_seconds()
                        >= self._chain_persist_interval
                    ):
                        self._persistence.save_option_chain_snapshot(chain, atm)
                        self._last_chain_persist_at = tick.timestamp
                except Exception as e:
                    self._logger.log_error(e, {"context": "persistence_write"})

            snapshot = self._build_snapshot(completed=completed)
            return snapshot

    def _tick_worker(self) -> None:
        try:
            self._logger.log_system_event("engine_started", {"underlying": self.underlying})

            for tick in self._source.stream_ticks():
                if self._stop_event.is_set():
                    break

                # Reliability gate: reject a missing/invalid timestamp, an
                # exact-duplicate re-delivery, or an out-of-order tick before
                # it can reach the candle aggregator (see src/tick_validation.py).
                validation = validate_tick(tick, self._last_accepted_timestamp)
                if not validation.is_valid:
                    with self._lock:
                        if validation.is_duplicate:
                            self._duplicate_tick_count += 1
                        else:
                            self._rejected_tick_count += 1
                    self._logger.log_system_event(
                        "tick_rejected",
                        {"reason": validation.reason, "timestamp": str(tick.timestamp if tick else None)},
                    )
                    continue

                snapshot = self._process_tick_data(tick)
                with self._lock:
                    subscribers = list(self._subscribers)

                for callback in subscribers:
                    try:
                        callback(snapshot)
                    except Exception as e:
                        self._logger.log_error(e, {"context": "subscriber_callback"})
        except Exception as exc:
            self._logger.log_error(exc, {"context": "tick_worker"})
            self._worker_error = exc
            self._stop_event.set()

    def get_strike_straddle_candles(self, strike: float) -> dict | None:
        """CE/PE/Straddle OHLC candles for one strike (any strike currently
        or previously present in the option chain), for the ATM Straddle
        Chart's candlestick view. None if that strike hasn't been seen yet."""
        from src.serializers import serialize_strike_candles

        with self._lock:
            builders = self._strike_candle_engine.get_strike_builders(strike)
            if builders is None:
                return None
            return serialize_strike_candles(strike, builders)

    def _build_snapshot(self, completed: list[Candle]) -> dict:
        from src.serializers import serialize_snapshot

        # Status comes from the provider itself — no provider-specific
        # branching here. A crashed tick worker overrides it with ERROR.
        source = self._source
        try:
            status = source.status()
            status_detail = source.status_detail()
        except Exception as exc:  # a status probe must never break the snapshot
            status, status_detail = ProviderStatus.ERROR, f"status unavailable: {exc.__class__.__name__}"
        if self._worker_error is not None:
            status = ProviderStatus.ERROR
            status_detail = f"Feed stopped: {self._worker_error.__class__.__name__}: {self._worker_error}"
        status_label = status.value
        # Simulated providers keep their config key ("mock"/"replay") as the
        # label, exactly as before; market-sourced ones show their name.
        effective_source = provider_key(self.cfg) if source.is_simulated else source.provider_name
        provider_info = {
            "name": source.provider_name,
            "key": provider_key(self.cfg),
            "status": status_label,
            "status_detail": status_detail,
            "is_simulated": source.is_simulated,
            "underlying": self.underlying,
            "expiries": self._latest_chain.expiries if self._latest_chain else [],
            "greeks_source": self._greeks.name,
            "execution_mode": self._execution.mode,
        }

        last_tick_age_seconds, is_stale = compute_staleness(
            self._latest_tick.timestamp if self._latest_tick else None,
            threshold_seconds=self.stale_threshold_seconds,
        )

        return serialize_snapshot(
            duplicate_tick_count=self._duplicate_tick_count,
            rejected_tick_count=self._rejected_tick_count,
            market_session=self._latest_market_session,
            underlying=self.underlying,
            tick=self._latest_tick,
            atm=self._latest_atm,
            chain=self._latest_chain,
            calculated_prices=self._latest_calculated_prices,
            rrg_snapshot=self._latest_rrg_snapshot,
            active_candles=self._aggregator.snapshot_active(),
            completed_candles=self._aggregator.completed_candles,
            newly_completed=completed,
            tick_count=self._tick_count,
            intervals=self.intervals,
            data_source=effective_source,
            status=status_label,
            paper_trading=self._paper_trading.to_state_dict(),
            last_tick_age_seconds=last_tick_age_seconds,
            is_stale=is_stale,
            stale_threshold_seconds=self.stale_threshold_seconds,
            persistence_enabled=self._persistence is not None,
            settings=self.get_settings(),
            provider=provider_info,
        )
