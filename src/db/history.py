"""Market history in the shared database (PostgreSQL in production, SQLite in development).

Drop-in replacement for `src.persistence.PersistenceStore`: same method names, same return shapes.
The difference that matters in production is *where the data lives*. The SQLite store is a file on
the machine running the engine, so it is lost when that container is replaced and invisible to a
standby ingestor. This one lives in the same database as everything else, so a failover, a redeploy
or a second ingestor all see the same history.

Writes come from the single leading engine; reads come from whichever process answers the request.
A write that fails is the caller's problem to log (the engine already wraps persistence in
try/except): market data must keep flowing even when the database hiccups.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects import postgresql, sqlite

from src.db.models import LegacyPaperTrade, MarketCandle, MarketChainSnapshot
from src.db.session import get_engine, session_scope
from src.models import ATMResult, Candle, OptionChainSnapshot
from src.paper_trading import PaperPosition
from src.timeutil import to_ist, trading_date_str

_CANDLE_COLUMNS = ("symbol", "interval_minutes", "open_time", "close_time", "open", "high", "low", "close", "tick_count")
_CHAIN_COLUMNS = ("underlying", "underlying_ltp", "expiry", "atm_strike", "atm_call_ltp", "atm_put_ltp", "straddle_premium", "timestamp")


def _insert(session, model):
    """The dialect's INSERT (both support ON CONFLICT)."""
    return (postgresql if session.get_bind().dialect.name == "postgresql" else sqlite).insert(model)


def _row(obj, columns: tuple[str, ...]) -> dict:
    return {c: getattr(obj, c) for c in columns}


class SqlPersistenceStore:
    def __init__(self) -> None:
        get_engine()  # fail here, at construction, if the database is unreachable or misconfigured
        self.db_path = None  # parity with PersistenceStore (there is no file)

    # -- writes ---------------------------------------------------------

    def save_candle(self, candle: Candle) -> None:
        values = {
            "symbol": candle.symbol,
            "interval_minutes": candle.interval_minutes,
            "open_time": to_ist(candle.open_time).isoformat(),
            "close_time": to_ist(candle.close_time).isoformat(),
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "tick_count": candle.tick_count,
            "trading_date": trading_date_str(candle.open_time),
        }
        with session_scope() as session:
            stmt = _insert(session, MarketCandle).values(**values)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["symbol", "interval_minutes", "open_time"],
                    set_={k: v for k, v in values.items() if k not in ("symbol", "interval_minutes", "open_time")},
                )
            )

    def save_option_chain_snapshot(self, chain: OptionChainSnapshot, atm: ATMResult | None) -> None:
        with session_scope() as session:
            session.add(
                MarketChainSnapshot(
                    underlying=chain.underlying,
                    underlying_ltp=chain.underlying_ltp,
                    expiry=chain.expiry,
                    atm_strike=atm.strike if atm else None,
                    atm_call_ltp=atm.call_ltp if atm else None,
                    atm_put_ltp=atm.put_ltp if atm else None,
                    straddle_premium=atm.straddle_premium if atm else None,
                    timestamp=to_ist(chain.timestamp).isoformat(),
                    trading_date=trading_date_str(chain.timestamp),
                )
            )

    def save_paper_trade(self, position: PaperPosition) -> None:
        if position.entry_time is None:
            return
        values = {
            "id": position.id,
            "option_type": position.option_type,
            "side": position.side,
            "strike": position.strike,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "entry_time": to_ist(position.entry_time).isoformat(),
            "exit_price": position.exit_price,
            "exit_time": to_ist(position.exit_time).isoformat() if position.exit_time else None,
            "exit_reason": position.exit_reason,
            "pnl": position.pnl,
            "pnl_percent": position.pnl_percent,
            "take_profit_percent": position.take_profit_percent,
            "stop_loss_percent": position.stop_loss_percent,
            "take_profit_amount": position.take_profit_amount,
            "stop_loss_amount": position.stop_loss_amount,
            "trading_date": trading_date_str(position.entry_time),
        }
        with session_scope() as session:
            stmt = _insert(session, LegacyPaperTrade).values(**values)
            session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={k: v for k, v in values.items() if k != "id"}))

    # -- reads (recent / live tail) --------------------------------------

    def get_recent_candles(self, symbol: str, interval_minutes: int, limit: int = 100) -> list[dict]:
        with session_scope() as session:
            rows = session.scalars(
                select(MarketCandle)
                .where(MarketCandle.symbol == symbol, MarketCandle.interval_minutes == interval_minutes)
                .order_by(MarketCandle.open_time.desc())
                .limit(limit)
            ).all()
            return [_row(r, _CANDLE_COLUMNS) for r in reversed(rows)]

    def get_recent_option_chain_snapshots(self, underlying: str, limit: int = 100) -> list[dict]:
        with session_scope() as session:
            rows = session.scalars(
                select(MarketChainSnapshot)
                .where(MarketChainSnapshot.underlying == underlying)
                .order_by(MarketChainSnapshot.timestamp.desc())
                .limit(limit)
            ).all()
            return [_row(r, _CHAIN_COLUMNS) for r in reversed(rows)]

    # -- reads (by Asia/Kolkata trading date) -----------------------------

    def list_trading_dates(self, underlying: str | None = None) -> list[str]:
        with session_scope() as session:
            dates: set[str] = set(session.scalars(select(MarketCandle.trading_date).where(MarketCandle.trading_date != "").distinct()))
            chain = select(MarketChainSnapshot.trading_date).where(MarketChainSnapshot.trading_date != "")
            if underlying is not None:
                chain = chain.where(MarketChainSnapshot.underlying == underlying)
            dates.update(session.scalars(chain.distinct()))
            dates.update(session.scalars(select(LegacyPaperTrade.trading_date).where(LegacyPaperTrade.trading_date != "").distinct()))
        return sorted(dates, reverse=True)

    def get_candles_for_date(self, symbol: str, interval_minutes: int, trading_date: str, limit: int = 2000) -> list[dict]:
        with session_scope() as session:
            rows = session.scalars(
                select(MarketCandle)
                .where(MarketCandle.symbol == symbol, MarketCandle.interval_minutes == interval_minutes, MarketCandle.trading_date == trading_date)
                .order_by(MarketCandle.open_time.asc())
                .limit(limit)
            ).all()
            return [_row(r, _CANDLE_COLUMNS) for r in rows]

    def get_option_chain_snapshots_for_date(self, underlying: str, trading_date: str, limit: int = 5000) -> list[dict]:
        with session_scope() as session:
            rows = session.scalars(
                select(MarketChainSnapshot)
                .where(MarketChainSnapshot.underlying == underlying, MarketChainSnapshot.trading_date == trading_date)
                .order_by(MarketChainSnapshot.timestamp.asc())
                .limit(limit)
            ).all()
            return [_row(r, _CHAIN_COLUMNS) for r in rows]

    def list_strikes_for_date(self, trading_date: str) -> list[float]:
        """Distinct strikes with per-strike (CE/PE/Straddle) candles on this date. Leg symbols are
        "<strike>_ce" / "_pe" / "_straddle" (see StrikeStraddleCandleEngine)."""
        with session_scope() as session:
            symbols = session.scalars(
                select(MarketCandle.symbol)
                .where(
                    MarketCandle.trading_date == trading_date,
                    MarketCandle.symbol.like("%\\_ce", escape="\\")
                    | MarketCandle.symbol.like("%\\_pe", escape="\\")
                    | MarketCandle.symbol.like("%\\_straddle", escape="\\"),
                )
                .distinct()
            ).all()
        strikes: set[float] = set()
        for symbol in symbols:
            try:
                strikes.add(float(symbol.rsplit("_", 1)[0]))
            except ValueError:
                continue
        return sorted(strikes)

    def get_strike_candles_for_date(self, strike: float, intervals: list[int], trading_date: str) -> dict[str, dict[str, list[dict]]]:
        by_interval: dict[str, dict[str, list[dict]]] = {}
        for interval in intervals:
            by_interval[str(interval)] = {
                leg: self.get_candles_for_date(f"{strike}_{leg}", interval, trading_date) for leg in ("ce", "pe", "straddle")
            }
        return by_interval

    def get_paper_trades_for_date(self, trading_date: str) -> list[dict]:
        with session_scope() as session:
            rows = session.scalars(
                select(LegacyPaperTrade).where(LegacyPaperTrade.trading_date == trading_date).order_by(LegacyPaperTrade.entry_time.asc())
            ).all()
            return [{c.name: getattr(r, c.name) for c in LegacyPaperTrade.__table__.columns} for r in rows]

    # -- maintenance -------------------------------------------------------

    def count_candles(self) -> int:
        with session_scope() as session:
            return int(session.scalar(select(func.count()).select_from(MarketCandle)) or 0)

    def purge_before(self, trading_date: str) -> int:
        """Delete candles and snapshots from days before `trading_date` (retention). Returns rows removed."""
        with session_scope() as session:
            removed = session.execute(delete(MarketCandle).where(MarketCandle.trading_date < trading_date)).rowcount or 0
            removed += session.execute(delete(MarketChainSnapshot).where(MarketChainSnapshot.trading_date < trading_date)).rowcount or 0
            return removed

    def close(self) -> None:
        """The engine pool is shared with the rest of the process and is not closed here."""
