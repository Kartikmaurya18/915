"""
Lightweight SQLite persistence for completed candles (including per-strike
CE/PE/Straddle candles), option chain / ATM snapshots, and paper trading
history — all keyed additionally by Asia/Kolkata trading date, so previous
trading sessions can be retrieved after a restart/redeploy.

This is a purely additive layer: nothing in the candle aggregator, ATM
resolver, or paper trading engine depends on it. It is safe to disable via
config (`persistence.enabled: false`) without touching any pipeline logic.
Uses Python's stdlib `sqlite3` — no new dependency.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from src.models import ATMResult, Candle, OptionChainSnapshot
from src.paper_trading import PaperPosition
from src.timeutil import to_ist, trading_date_str


def ensure_seeded(db_path: str | Path, seed_db_path: str | Path | None) -> None:
    """
    Bootstrap the runtime SQLite file from a committed seed copy, but ONLY
    when the runtime file doesn't exist yet.

    Why this exists: on a host with an ephemeral container filesystem (e.g.
    Railway without a mounted Volume), `db_path` starts out missing on every
    deploy/restart — nothing written to local disk during a previous
    container's lifetime survives. Localhost never hits this because its
    `data/market_data.db` already exists and keeps accumulating across runs.
    Committing a seed snapshot (`data/seed/market_data.seed.db` by default —
    see .gitignore's carve-out) and copying it into place here means the
    deployed app has the same previously-recorded MOCK trading sessions
    localhost already has, without changing the storage mechanism, schema,
    or any read/write API at all: this only decides which bytes are on disk
    the moment before PersistenceStore first opens `db_path`.

    A no-op whenever `db_path` already exists (never overwrites real
    accumulated data — local or a Railway container that's already been
    running for a while) or no seed is configured/present.
    """
    if not seed_db_path:
        return
    db_path = Path(db_path)
    seed_db_path = Path(seed_db_path)
    if db_path.exists() or not seed_db_path.exists():
        return
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed_db_path, db_path)
    except OSError:
        pass


class PersistenceStore:
    """Thread-safe SQLite-backed store for completed candles, option chain
    snapshots, and paper trade history — each retrievable by Asia/Kolkata
    trading date."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        except (OSError, sqlite3.Error):
            self.db_path = Path("/tmp") / Path(db_path).name
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    open_time TEXT NOT NULL,
                    close_time TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    tick_count INTEGER NOT NULL,
                    trading_date TEXT NOT NULL DEFAULT '',
                    UNIQUE(symbol, interval_minutes, open_time)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candles_lookup "
                "ON candles(symbol, interval_minutes, open_time)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_chain_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    underlying TEXT NOT NULL,
                    underlying_ltp REAL,
                    expiry TEXT,
                    atm_strike REAL,
                    atm_call_ltp REAL,
                    atm_put_ltp REAL,
                    straddle_premium REAL,
                    timestamp TEXT NOT NULL,
                    trading_date TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chain_snapshots_lookup "
                "ON option_chain_snapshots(underlying, timestamp)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id TEXT PRIMARY KEY,
                    option_type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    strike REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_price REAL,
                    exit_time TEXT,
                    exit_reason TEXT,
                    pnl REAL,
                    pnl_percent REAL,
                    take_profit_percent REAL,
                    stop_loss_percent REAL,
                    take_profit_amount REAL,
                    stop_loss_amount REAL,
                    trading_date TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_trading_date "
                "ON paper_trades(trading_date)"
            )
            self._conn.commit()
            self._migrate_add_trading_date_columns()

    def _migrate_add_trading_date_columns(self) -> None:
        """Backfill `trading_date` for rows written before this column
        existed (a plain DB opened from an older version of this app)."""
        for table, time_col in (("candles", "open_time"), ("option_chain_snapshots", "timestamp")):
            cols = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if "trading_date" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN trading_date TEXT NOT NULL DEFAULT ''")
            rows = self._conn.execute(
                f"SELECT id, {time_col} FROM {table} WHERE trading_date = '' OR trading_date IS NULL"
            ).fetchall()
            for row in rows:
                try:
                    ts = datetime.fromisoformat(row[time_col])
                except (TypeError, ValueError):
                    continue
                self._conn.execute(
                    f"UPDATE {table} SET trading_date = ? WHERE id = ?",
                    (trading_date_str(ts), row["id"]),
                )
            if rows:
                self._conn.commit()

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candles_trading_date "
            "ON candles(symbol, interval_minutes, trading_date)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chain_snapshots_trading_date "
            "ON option_chain_snapshots(underlying, trading_date)"
        )
        self._conn.commit()

    # -- writes ---------------------------------------------------------

    def save_candle(self, candle: Candle) -> None:
        """Upsert a completed candle keyed on (symbol, interval, open_time).
        `symbol` may be the underlying (e.g. "NIFTY") or a per-strike leg
        (e.g. "24800.0_ce" / "24800.0_pe" / "24800.0_straddle" — see
        StrikeStraddleCandleEngine), both stored in the same table."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO candles
                    (symbol, interval_minutes, open_time, close_time, open, high, low, close, tick_count, trading_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval_minutes, open_time) DO UPDATE SET
                    close_time=excluded.close_time,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    tick_count=excluded.tick_count,
                    trading_date=excluded.trading_date
                """,
                (
                    candle.symbol,
                    candle.interval_minutes,
                    to_ist(candle.open_time).isoformat(),
                    to_ist(candle.close_time).isoformat(),
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.tick_count,
                    trading_date_str(candle.open_time),
                ),
            )
            self._conn.commit()

    def save_option_chain_snapshot(
        self, chain: OptionChainSnapshot, atm: ATMResult | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO option_chain_snapshots
                    (underlying, underlying_ltp, expiry, atm_strike, atm_call_ltp,
                     atm_put_ltp, straddle_premium, timestamp, trading_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain.underlying,
                    chain.underlying_ltp,
                    chain.expiry,
                    atm.strike if atm else None,
                    atm.call_ltp if atm else None,
                    atm.put_ltp if atm else None,
                    atm.straddle_premium if atm else None,
                    to_ist(chain.timestamp).isoformat(),
                    trading_date_str(chain.timestamp),
                ),
            )
            self._conn.commit()

    def save_paper_trade(self, position: PaperPosition) -> None:
        """Persist a closed (exited) simulated position, keyed by its own id
        so a re-save (shouldn't normally happen) is idempotent, not a
        duplicate. Grouped under the trading date it was OPENED on."""
        if position.entry_time is None:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO paper_trades
                    (id, option_type, side, strike, quantity, entry_price, entry_time,
                     exit_price, exit_time, exit_reason, pnl, pnl_percent,
                     take_profit_percent, stop_loss_percent, take_profit_amount,
                     stop_loss_amount, trading_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.id,
                    position.option_type,
                    position.side,
                    position.strike,
                    position.quantity,
                    position.entry_price,
                    to_ist(position.entry_time).isoformat(),
                    position.exit_price,
                    to_ist(position.exit_time).isoformat() if position.exit_time else None,
                    position.exit_reason,
                    position.pnl,
                    position.pnl_percent,
                    position.take_profit_percent,
                    position.stop_loss_percent,
                    position.take_profit_amount,
                    position.stop_loss_amount,
                    trading_date_str(position.entry_time),
                ),
            )
            self._conn.commit()

    # -- reads (recent / live tail) --------------------------------------

    def get_recent_candles(
        self, symbol: str, interval_minutes: int, limit: int = 100
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT symbol, interval_minutes, open_time, close_time, open, high, low, close, tick_count
                FROM candles
                WHERE symbol = ? AND interval_minutes = ?
                ORDER BY open_time DESC
                LIMIT ?
                """,
                (symbol, interval_minutes, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_recent_option_chain_snapshots(self, underlying: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT underlying, underlying_ltp, expiry, atm_strike, atm_call_ltp,
                       atm_put_ltp, straddle_premium, timestamp
                FROM option_chain_snapshots
                WHERE underlying = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (underlying, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # -- reads (by Asia/Kolkata trading date, for the historical view) ----

    def list_trading_dates(self, underlying: str | None = None) -> list[str]:
        """Every distinct trading date with any persisted data (candles,
        option chain snapshots, or paper trades), newest first."""
        with self._lock:
            dates: set[str] = set()
            for row in self._conn.execute(
                "SELECT DISTINCT trading_date FROM candles WHERE trading_date != ''"
            ):
                dates.add(row["trading_date"])
            chain_sql = "SELECT DISTINCT trading_date FROM option_chain_snapshots WHERE trading_date != ''"
            params: tuple = ()
            if underlying is not None:
                chain_sql += " AND underlying = ?"
                params = (underlying,)
            for row in self._conn.execute(chain_sql, params):
                dates.add(row["trading_date"])
            for row in self._conn.execute(
                "SELECT DISTINCT trading_date FROM paper_trades WHERE trading_date != ''"
            ):
                dates.add(row["trading_date"])
        return sorted(dates, reverse=True)

    def get_candles_for_date(
        self, symbol: str, interval_minutes: int, trading_date: str, limit: int = 2000
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT symbol, interval_minutes, open_time, close_time, open, high, low, close, tick_count
                FROM candles
                WHERE symbol = ? AND interval_minutes = ? AND trading_date = ?
                ORDER BY open_time ASC
                LIMIT ?
                """,
                (symbol, interval_minutes, trading_date, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_option_chain_snapshots_for_date(
        self, underlying: str, trading_date: str, limit: int = 5000
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT underlying, underlying_ltp, expiry, atm_strike, atm_call_ltp,
                       atm_put_ltp, straddle_premium, timestamp
                FROM option_chain_snapshots
                WHERE underlying = ? AND trading_date = ?
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (underlying, trading_date, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_strikes_for_date(self, trading_date: str) -> list[float]:
        """Distinct strikes with persisted per-strike (CE/PE/Straddle)
        candle data on this trading date — powers the historical Straddle
        Chart's strike picker. Strike-leg symbols are "<strike>_ce" /
        "_pe" / "_straddle" (see StrikeStraddleCandleEngine)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT symbol FROM candles
                WHERE trading_date = ?
                  AND (symbol LIKE '%_ce' OR symbol LIKE '%_pe' OR symbol LIKE '%_straddle')
                """,
                (trading_date,),
            ).fetchall()
        strikes: set[float] = set()
        for row in rows:
            symbol = row["symbol"]
            base = symbol.rsplit("_", 1)[0]
            try:
                strikes.add(float(base))
            except ValueError:
                continue
        return sorted(strikes)

    def get_strike_candles_for_date(
        self, strike: float, intervals: list[int], trading_date: str
    ) -> dict[str, dict[str, list[dict]]]:
        """CE/PE/Straddle OHLC candles for one strike on one trading date, at
        every requested interval — the historical counterpart of
        serialize_strike_candles(), read straight from the DB instead of the
        in-memory (live-only) StrikeStraddleCandleEngine."""
        by_interval: dict[str, dict[str, list[dict]]] = {}
        for interval in intervals:
            by_interval[str(interval)] = {}
            for leg in ("ce", "pe", "straddle"):
                symbol = f"{strike}_{leg}"
                by_interval[str(interval)][leg] = self.get_candles_for_date(
                    symbol, interval, trading_date
                )
        return by_interval

    def get_paper_trades_for_date(self, trading_date: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM paper_trades
                WHERE trading_date = ?
                ORDER BY entry_time ASC
                """,
                (trading_date,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
