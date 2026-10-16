"""Per-user paper trading, stateless: the database is the only source of truth.

Nothing about a user's book is cached in this process, so any number of gateway
replicas can serve any user, and a restart loses nothing. Prices come from a
`MarketView` (the hub's latest topic state). Take-profit / stop-loss exits are NOT
decided here - a single exit worker (src/paper/exit_worker.py), running next to the
engine, scans open positions on every tick.

Isolation rules enforced here:
  * every query filters on `user_id`, taken from the authenticated principal -
    never from request input;
  * a position id from another user is indistinguishable from a missing one;
  * closing is a conditional UPDATE (`... WHERE status = 'OPEN'`), so a manual close
    racing an automatic exit has exactly one winner.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from src.auth.service import audit
from src.db.models import IdempotencyKey, PaperPositionRow, PaperSettings
from src.db.session import session_scope
from src.execution import OrderRequest, PaperExecutionClient
from src.paper.market_view import NoQuote
from src.paper_trading import EXIT_MANUAL, PaperPosition, PaperTradeConfig, PaperTradingEngine
from src.timeutil import trading_date_str

MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN_POSITIONS", "100"))
MAX_QUANTITY = int(os.getenv("PAPER_MAX_QUANTITY", "100000"))


class PaperError(ValueError):
    """A request the user can fix (bad input, limit reached, no such contract)."""


# -- row <-> position -----------------------------------------------------------------


def row_to_position(row: PaperPositionRow) -> PaperPosition:
    return PaperPosition(
        id=row.id,
        option_type=row.option_type,
        strike=row.strike,
        quantity=row.quantity,
        entry_price=row.entry_price,
        entry_time=row.entry_time,
        take_profit_percent=row.take_profit_percent,
        stop_loss_percent=row.stop_loss_percent,
        side=row.side,
        take_profit_amount=row.take_profit_amount,
        stop_loss_amount=row.stop_loss_amount,
        current_price=row.current_price,
        status=row.status,
        exit_price=row.exit_price,
        exit_time=row.exit_time,
        exit_reason=row.exit_reason,
        pnl=row.pnl,
        pnl_percent=row.pnl_percent,
        underlying=row.underlying,
        expiry=row.expiry,
        exchange=row.exchange,
        instrument_id=row.instrument_id,
    )


def position_values(pos: PaperPosition) -> dict:
    """Column values for `pos`, usable for INSERT and for a conditional UPDATE."""
    return {
        "option_type": pos.option_type,
        "side": pos.side,
        "strike": pos.strike,
        "quantity": pos.quantity,
        "entry_price": pos.entry_price,
        "current_price": pos.current_price,
        "entry_time": pos.entry_time,
        "take_profit_percent": pos.take_profit_percent,
        "stop_loss_percent": pos.stop_loss_percent,
        "take_profit_amount": pos.take_profit_amount,
        "stop_loss_amount": pos.stop_loss_amount,
        "status": pos.status,
        "exit_price": pos.exit_price,
        "exit_time": pos.exit_time,
        "exit_reason": pos.exit_reason,
        "pnl": pos.pnl,
        "pnl_percent": pos.pnl_percent,
        "underlying": pos.underlying,
        "expiry": pos.expiry,
        "exchange": pos.exchange,
        "instrument_id": pos.instrument_id,
        "trading_date": trading_date_str(pos.entry_time),
    }


def position_to_row(user_id: str, pos: PaperPosition) -> PaperPositionRow:
    return PaperPositionRow(user_id=user_id, id=pos.id, **position_values(pos))


def close_if_open(session, user_id: str, pos: PaperPosition) -> bool:
    """Persist a just-closed position only if the database still has it OPEN.
    Returns False if someone else (a manual close, the exit worker) got there first."""
    values = position_values(pos)
    result = session.execute(
        update(PaperPositionRow)
        .where(PaperPositionRow.user_id == user_id, PaperPositionRow.id == pos.id, PaperPositionRow.status == "OPEN")
        .values(**values)
    )
    return result.rowcount == 1


def row_to_persisted_trade(row: PaperPositionRow) -> dict:
    """Shape a closed row like the legacy SQLite `paper_trades` row, so the
    existing historical-session serializers work on it unchanged."""
    return {
        "id": row.id,
        "option_type": row.option_type,
        "side": row.side,
        "strike": row.strike,
        "quantity": row.quantity,
        "entry_price": row.entry_price,
        "entry_time": row.entry_time.isoformat(),
        "exit_price": row.exit_price,
        "exit_time": row.exit_time.isoformat() if row.exit_time else None,
        "exit_reason": row.exit_reason,
        "pnl": row.pnl,
        "pnl_percent": row.pnl_percent,
        "take_profit_percent": row.take_profit_percent,
        "stop_loss_percent": row.stop_loss_percent,
        "take_profit_amount": row.take_profit_amount,
        "stop_loss_amount": row.stop_loss_amount,
        "trading_date": row.trading_date,
    }


def default_config(cfg: dict | None = None) -> PaperTradeConfig:
    paper = (cfg or {}).get("paper_trading") or {}
    return PaperTradeConfig(
        take_profit_percent=float(paper.get("take_profit_percent", 10.0)),
        stop_loss_percent=float(paper.get("stop_loss_percent", 5.0)),
        take_profit_amount=paper.get("take_profit_amount"),
        stop_loss_amount=paper.get("stop_loss_amount"),
    )


# -- the service ----------------------------------------------------------------------


class PaperService:
    def __init__(self, view, defaults: PaperTradeConfig | None = None):
        self._view = view
        self._defaults = defaults or default_config()

    def _config_for(self, session, user_id: str) -> PaperTradeConfig:
        row = session.get(PaperSettings, user_id)
        if row is None:
            return PaperTradeConfig(**self._defaults.as_dict())
        return PaperTradeConfig(
            take_profit_percent=row.take_profit_percent,
            stop_loss_percent=row.stop_loss_percent,
            take_profit_amount=row.take_profit_amount,
            stop_loss_amount=row.stop_loss_amount,
        )

    # -- reads ---------------------------------------------------------------

    def state(self, user_id: str) -> dict:
        with session_scope() as session:
            config = self._config_for(session, user_id)
            rows = session.scalars(
                select(PaperPositionRow).where(PaperPositionRow.user_id == user_id).order_by(PaperPositionRow.entry_time.asc())
            ).all()
            open_positions = [row_to_position(r) for r in rows if r.status == "OPEN"]
            closed = sorted((r for r in rows if r.status != "OPEN"), key=lambda r: r.exit_time or r.entry_time)
            history = [row_to_position(r) for r in closed]
        for pos in open_positions:  # mark to the latest market price for display (exits are the worker's job)
            price = self._view.leg_price(pos.option_type, pos.strike)
            if price is not None:
                pos.mark_to_market(price)
        book = PaperTradingEngine(config)
        book.load_state(open_positions, history)
        return book.to_state_dict()

    def trades_for_date(self, user_id: str, trading_date: str) -> list[dict]:
        with session_scope() as session:
            rows = session.scalars(
                select(PaperPositionRow)
                .where(
                    PaperPositionRow.user_id == user_id,
                    PaperPositionRow.trading_date == trading_date,
                    PaperPositionRow.status != "OPEN",
                )
                .order_by(PaperPositionRow.entry_time.asc())
            ).all()
            return [row_to_persisted_trade(r) for r in rows]

    # -- writes --------------------------------------------------------------

    def set_config(
        self,
        user_id: str,
        take_profit_percent: Optional[float],
        stop_loss_percent: Optional[float],
        take_profit_amount: Optional[float],
        stop_loss_amount: Optional[float],
    ) -> dict:
        with session_scope() as session:
            scratch = PaperTradingEngine(self._config_for(session, user_id))
            try:
                config = scratch.set_config(take_profit_percent, stop_loss_percent, take_profit_amount, stop_loss_amount)
            except ValueError as exc:  # validated BEFORE anything is written
                raise PaperError(str(exc)) from exc
            row = session.get(PaperSettings, user_id)
            if row is None:
                row = PaperSettings(user_id=user_id)
                session.add(row)
            row.take_profit_percent = config.take_profit_percent
            row.stop_loss_percent = config.stop_loss_percent
            row.take_profit_amount = config.take_profit_amount
            row.stop_loss_amount = config.stop_loss_amount
            row.updated_at = datetime.now(timezone.utc)
            audit(session, "paper_config_changed", user_id, None, config.as_dict())
            return config.as_dict()

    def open_position(
        self,
        user_id: str,
        option_type: str,
        strike: Optional[float],
        quantity: int,
        side: str,
        expiry: Optional[str] = None,
        underlying: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        if not (0 < int(quantity) <= MAX_QUANTITY):
            raise PaperError(f"quantity must be between 1 and {MAX_QUANTITY}.")
        if idempotency_key:
            stored = self._stored_response(user_id, idempotency_key)
            if stored is not None:
                return stored
        with session_scope() as session:
            open_count = session.scalar(
                select(func.count()).select_from(PaperPositionRow).where(
                    PaperPositionRow.user_id == user_id, PaperPositionRow.status == "OPEN"
                )
            )
            config = self._config_for(session, user_id)
        if open_count >= MAX_OPEN_POSITIONS:
            raise PaperError(f"Open position limit reached ({MAX_OPEN_POSITIONS}). Close one first.")
        try:
            contract, ltp, timestamp = self._view.quote(option_type, strike, expiry, underlying)
            result = PaperExecutionClient(PaperTradingEngine(config)).place_order(
                OrderRequest(
                    contract=contract,
                    side=(side or "BUY").upper(),
                    quantity=int(quantity),
                    reference_price=ltp,
                    timestamp=timestamp,
                )
            )
        except (NoQuote, ValueError) as exc:
            raise PaperError(str(exc)) from exc
        position: PaperPosition = result.position
        response = position.to_dict()
        try:
            with session_scope() as session:
                session.add(position_to_row(user_id, position))
                if idempotency_key:
                    session.add(IdempotencyKey(user_id=user_id, key=idempotency_key, response=response))
                audit(
                    session,
                    "paper_open",
                    user_id,
                    None,
                    {"id": position.id, "type": position.option_type, "strike": position.strike, "qty": position.quantity},
                )
        except IntegrityError:
            # A concurrent request with the same idempotency key won the race.
            stored = self._stored_response(user_id, idempotency_key) if idempotency_key else None
            if stored is not None:
                return stored
            raise
        return response

    def close_position(self, user_id: str, position_id: str) -> Optional[dict]:
        with session_scope() as session:
            row = session.get(PaperPositionRow, (user_id, position_id))
            if row is None or row.status != "OPEN":
                return None  # not found, already closed, or someone else's: indistinguishable
            position = row_to_position(row)
            price = self._view.leg_price(position.option_type, position.strike)
            position.close(price if price is not None else position.current_price, self._view.latest_time(), EXIT_MANUAL)
            if not close_if_open(session, user_id, position):
                return None  # the exit worker closed it between our read and our write
            audit(session, "paper_close", user_id, None, {"id": position.id, "pnl": round(position.pnl, 2)})
            return position.to_dict()

    def _stored_response(self, user_id: str, key: str) -> Optional[dict]:
        with session_scope() as session:
            row = session.get(IdempotencyKey, (user_id, key))
            return dict(row.response) if row is not None else None
