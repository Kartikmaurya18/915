"""Per-user paper trading, backed by the database.

Every user gets their own `PaperTradingEngine` (the existing, unchanged
simulation logic), hydrated from the database on first use and written through
on every open / close / auto-exit. The database is the source of truth; the
in-memory books exist so each market tick can mark positions to market and fire
TP/SL exits without a query per user per tick.

Isolation rules enforced here:
  * every query filters on `user_id`, taken from the authenticated principal -
    never from request input;
  * a position id from another user is indistinguishable from a missing one.

Known Phase-1 limits (addressed in Phase 2 when the engine moves to a dedicated
ingestor worker): the book cache is per-process, and TP/SL exit writes happen on
the tick thread.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.auth.service import audit
from src.db.models import IdempotencyKey, PaperPositionRow, PaperSettings
from src.db.session import session_scope
from src.execution import OrderRequest, PaperExecutionClient
from src.paper_trading import EXIT_MANUAL, PaperPosition, PaperTradeConfig, PaperTradingEngine
from src.timeutil import trading_date_str

MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN_POSITIONS", "100"))
MAX_QUANTITY = int(os.getenv("PAPER_MAX_QUANTITY", "100000"))
BOOK_CACHE_MAX = int(os.getenv("PAPER_BOOK_CACHE_MAX", "5000"))


class PaperError(ValueError):
    """A request the user can fix (bad input, limit reached, no such contract)."""


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


def _apply_position(row: PaperPositionRow, pos: PaperPosition) -> None:
    row.option_type = pos.option_type
    row.side = pos.side
    row.strike = pos.strike
    row.quantity = pos.quantity
    row.entry_price = pos.entry_price
    row.current_price = pos.current_price
    row.entry_time = pos.entry_time
    row.take_profit_percent = pos.take_profit_percent
    row.stop_loss_percent = pos.stop_loss_percent
    row.take_profit_amount = pos.take_profit_amount
    row.stop_loss_amount = pos.stop_loss_amount
    row.status = pos.status
    row.exit_price = pos.exit_price
    row.exit_time = pos.exit_time
    row.exit_reason = pos.exit_reason
    row.pnl = pos.pnl
    row.pnl_percent = pos.pnl_percent
    row.underlying = pos.underlying
    row.expiry = pos.expiry
    row.exchange = pos.exchange
    row.instrument_id = pos.instrument_id
    row.trading_date = trading_date_str(pos.entry_time)


def position_to_row(user_id: str, pos: PaperPosition) -> PaperPositionRow:
    row = PaperPositionRow(user_id=user_id, id=pos.id)
    _apply_position(row, pos)
    return row


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


class UserPaperService:
    def __init__(self, engine):
        self._engine = engine
        self._books: "OrderedDict[str, PaperTradingEngine]" = OrderedDict()
        self._lock = threading.Lock()
        self._failed_writes: list[tuple[str, PaperPosition]] = []
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Rehydrate every user who still has open positions (so their TP/SL keeps
        working after a restart) and subscribe to the tick pipeline."""
        if self._started:
            return
        with session_scope() as session:
            user_ids = session.scalars(
                select(PaperPositionRow.user_id).where(PaperPositionRow.status == "OPEN").distinct()
            ).all()
        for user_id in user_ids:
            self._book(user_id)
        self._engine.add_chain_listener(self.on_chain)
        self._started = True

    def stop(self) -> None:
        self._engine.remove_chain_listener(self.on_chain)
        self._started = False

    # -- books ---------------------------------------------------------------

    def _default_config(self) -> PaperTradeConfig:
        cfg = (self._engine.cfg.get("paper_trading") or {}) if hasattr(self._engine, "cfg") else {}
        return PaperTradeConfig(
            take_profit_percent=float(cfg.get("take_profit_percent", 10.0)),
            stop_loss_percent=float(cfg.get("stop_loss_percent", 5.0)),
            take_profit_amount=cfg.get("take_profit_amount"),
            stop_loss_amount=cfg.get("stop_loss_amount"),
        )

    def _hydrate(self, user_id: str) -> PaperTradingEngine:
        with session_scope() as session:
            settings_row = session.get(PaperSettings, user_id)
            rows = session.scalars(
                select(PaperPositionRow)
                .where(PaperPositionRow.user_id == user_id)
                .order_by(PaperPositionRow.entry_time.asc())
            ).all()
            if settings_row is not None:
                config = PaperTradeConfig(
                    take_profit_percent=settings_row.take_profit_percent,
                    stop_loss_percent=settings_row.stop_loss_percent,
                    take_profit_amount=settings_row.take_profit_amount,
                    stop_loss_amount=settings_row.stop_loss_amount,
                )
            else:
                config = self._default_config()
            open_positions = [row_to_position(r) for r in rows if r.status == "OPEN"]
            closed = sorted(
                (r for r in rows if r.status != "OPEN"),
                key=lambda r: r.exit_time or r.entry_time,
            )
            history = [row_to_position(r) for r in closed]
        book = PaperTradingEngine(config)
        book.load_state(open_positions, history)
        return book

    def _book(self, user_id: str) -> PaperTradingEngine:
        with self._lock:
            book = self._books.get(user_id)
            if book is not None:
                self._books.move_to_end(user_id)
                return book
        # Hydrate outside the lock: it does I/O. A concurrent hydrate for the
        # same user is harmless - setdefault keeps exactly one book.
        fresh = self._hydrate(user_id)
        with self._lock:
            book = self._books.setdefault(user_id, fresh)
            self._books.move_to_end(user_id)
            self._evict_locked()
            return book

    def _evict_locked(self) -> None:
        """Bound memory: drop least-recently-used books that hold no open positions."""
        if len(self._books) <= BOOK_CACHE_MAX:
            return
        for uid in list(self._books.keys()):
            if len(self._books) <= BOOK_CACHE_MAX:
                break
            if not self._books[uid].get_open_positions():
                del self._books[uid]

    # -- reads ---------------------------------------------------------------

    def state(self, user_id: str) -> dict:
        return self._book(user_id).to_state_dict()

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
        book = self._book(user_id)
        before = book.get_config()
        try:
            config = book.set_config(take_profit_percent, stop_loss_percent, take_profit_amount, stop_loss_amount)
        except ValueError as exc:
            raise PaperError(str(exc)) from exc
        try:
            with session_scope() as session:
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
        except Exception:
            book.replace_config(before)  # keep memory consistent with the database
            raise
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
        book = self._book(user_id)
        if len(book.get_open_positions()) >= MAX_OPEN_POSITIONS:
            raise PaperError(f"Open position limit reached ({MAX_OPEN_POSITIONS}). Close one first.")
        try:
            quote, timestamp = self._engine.resolve_paper_quote(option_type, strike, expiry, underlying)
            result = PaperExecutionClient(book).place_order(
                OrderRequest(
                    contract=quote.contract,
                    side=(side or "BUY").upper(),
                    quantity=int(quantity),
                    reference_price=quote.ltp,
                    timestamp=timestamp,
                )
            )
        except ValueError as exc:
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
            book.discard_open(position.id)
            stored = self._stored_response(user_id, idempotency_key) if idempotency_key else None
            if stored is not None:
                return stored
            raise
        except Exception:
            book.discard_open(position.id)
            raise
        return response

    def close_position(self, user_id: str, position_id: str) -> Optional[dict]:
        book = self._book(user_id)
        position = next((p for p in book.get_open_positions() if p.id == position_id), None)
        if position is None:
            return None  # not found, already closed, or someone else's: indistinguishable
        price = self._engine.current_leg_price(position.option_type, position.strike)
        if price is None:
            price = position.current_price
        closed = book.close_position(position_id, price, self._engine.latest_tick_time(), EXIT_MANUAL)
        if closed is None:
            return None
        self._persist_closed(user_id, closed, "paper_close")
        return closed.to_dict()

    # -- tick pipeline -------------------------------------------------------

    def on_chain(self, chain, timestamp) -> None:
        """Called by the engine on every tick: mark every user's open positions
        to market and persist whatever TP/SL closed."""
        if self._failed_writes:
            retry, self._failed_writes = self._failed_writes, []
            for uid, pos in retry:
                self._persist_closed(uid, pos, "paper_auto_exit")
        with self._lock:
            targets = [(uid, book) for uid, book in self._books.items() if book.get_open_positions()]
        for user_id, book in targets:
            for pos in book.update(chain, timestamp):
                self._persist_closed(user_id, pos, "paper_auto_exit")

    def _persist_closed(self, user_id: str, pos: PaperPosition, event: str) -> None:
        try:
            with session_scope() as session:
                row = session.get(PaperPositionRow, (user_id, pos.id))
                if row is None:
                    row = position_to_row(user_id, pos)
                    session.add(row)
                else:
                    _apply_position(row, pos)
                audit(
                    session,
                    event,
                    user_id,
                    None,
                    {"id": pos.id, "reason": pos.exit_reason, "pnl": round(pos.pnl, 2)},
                )
        except Exception:
            # Memory says closed, the database still says open. Retry on the next
            # tick; if the process dies first, the position simply reopens on
            # restart (the database is the source of truth).
            self._failed_writes.append((user_id, pos))

    def _stored_response(self, user_id: str, key: str) -> Optional[dict]:
        with session_scope() as session:
            row = session.get(IdempotencyKey, (user_id, key))
            return dict(row.response) if row is not None else None
