"""
Paper (simulated) trading position engine.

IMPORTANT — SIMULATION ONLY:
This module never places, modifies, or cancels a real broker order. It exists
purely to track hypothetical CE/PE positions and their P&L against whatever
option-chain feed is currently powering the pipeline (mock today, Dhan in the
future — the engine only depends on `OptionChainSnapshot`, so swapping the
live data source underneath it requires no changes here).

Flow:
    open_position()  -> creates an OPEN PaperPosition at the current premium
    update()         -> called on every tick; marks positions to market and
                         auto-exits any that have crossed the configured
                         Take Profit / Stop Loss thresholds
    close_position()  -> manual simulated exit
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.models import Contract, OptionChainSnapshot
from src.risk import (  # re-exported: existing callers import these from here
    EXIT_MANUAL,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    ExitRule,
    evaluate_exit,
    position_pnl,
)
from src.timeutil import to_ist

VALID_OPTION_TYPES = ("CE", "PE")
VALID_SIDES = ("BUY", "SELL")


@dataclass
class PaperTradeConfig:
    """
    Configurable risk parameters. Temporary defaults: TP=10%, SL=5%.

    `take_profit_amount`/`stop_loss_amount` are an ADDITIONAL, independent
    exit trigger expressed in absolute ₹ P&L rather than %  — e.g. "exit once
    this position has made/lost ₹500" regardless of premium. Both None by
    default (disabled): existing percent-based behavior is unchanged unless
    a caller explicitly sets one. When both a percent and a ₹ target are
    configured, a position auto-exits on whichever is hit first (see
    PaperTradingEngine.update()) — never hard-coded, always read from here.
    """

    take_profit_percent: float = 10.0
    stop_loss_percent: float = 5.0
    take_profit_amount: Optional[float] = None
    stop_loss_amount: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "take_profit_percent": self.take_profit_percent,
            "stop_loss_percent": self.stop_loss_percent,
            "take_profit_amount": self.take_profit_amount,
            "stop_loss_amount": self.stop_loss_amount,
        }


@dataclass
class PaperPosition:
    """A single simulated CE/PE option position (BUY = long, SELL = short/write). Never a real order."""

    id: str
    option_type: str  # "CE" or "PE"
    strike: float
    quantity: int
    entry_price: float
    entry_time: datetime
    take_profit_percent: float
    stop_loss_percent: float
    side: str = "BUY"  # "BUY" (long) or "SELL" (short/write)
    # ₹ (absolute P&L) exit targets — an additional, independent trigger
    # alongside the percent ones above. None = that ₹ trigger is disabled for
    # this position. Captured at open time from PaperTradeConfig, same as the
    # percent fields, so a later config change never retroactively alters an
    # already-open position's own thresholds.
    take_profit_amount: Optional[float] = None
    stop_loss_amount: Optional[float] = None
    current_price: float = 0.0
    status: str = "OPEN"  # "OPEN" | "EXITED"
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None  # TAKE_PROFIT | STOP_LOSS | MANUAL
    pnl: float = 0.0
    pnl_percent: float = 0.0
    # Provider-independent contract identity, captured from the option
    # chain at open time (None when opened without a chain, e.g. in tests).
    underlying: Optional[str] = None
    expiry: Optional[str] = None
    exchange: Optional[str] = None
    instrument_id: Optional[str] = None

    @property
    def contract(self) -> Contract:
        return Contract(
            underlying=self.underlying or "",
            expiry=self.expiry or "",
            strike=self.strike,
            option_type=self.option_type,
            exchange=self.exchange,
            instrument_id=self.instrument_id,
        )

    @property
    def exit_rule(self) -> ExitRule:
        """This position's own TP/SL thresholds, captured at open time."""
        return ExitRule(
            take_profit_percent=self.take_profit_percent,
            stop_loss_percent=self.stop_loss_percent,
            take_profit_amount=self.take_profit_amount,
            stop_loss_amount=self.stop_loss_amount,
        )

    def mark_to_market(self, price: float) -> None:
        self.current_price = price
        # Side-aware P&L lives in src/risk.py (shared with any future risk engine).
        self.pnl, self.pnl_percent = position_pnl(self.side, self.entry_price, price, self.quantity)

    def close(self, price: float, timestamp: datetime, reason: str) -> None:
        self.mark_to_market(price)
        self.exit_price = price
        self.exit_time = timestamp
        self.exit_reason = reason
        self.status = "EXITED"

    def to_dict(self) -> dict:
        # Absolute premium levels implied by the configured TP/SL percentages,
        # and how far current P&L% still has to move to reach each one (0 or
        # below means the threshold has been reached/crossed). Plain
        # arithmetic on the already-confirmed TP/SL inputs — not a new
        # business formula. BUY profits as price rises, so its TP sits above
        # entry and SL below; SELL (short/write) is the mirror image.
        if self.side == "BUY":
            take_profit_price = self.entry_price * (1 + self.take_profit_percent / 100.0)
            stop_loss_price = self.entry_price * (1 - self.stop_loss_percent / 100.0)
        else:
            take_profit_price = self.entry_price * (1 - self.take_profit_percent / 100.0)
            stop_loss_price = self.entry_price * (1 + self.stop_loss_percent / 100.0)
        distance_to_tp_percent = self.take_profit_percent - self.pnl_percent
        distance_to_sl_percent = self.pnl_percent + self.stop_loss_percent
        is_open = self.status == "OPEN"

        # Position value: current notional exposure (mark-to-market while
        # open; frozen at the exit price once closed, since current_price is
        # updated to the exit price by close() -> mark_to_market()).
        position_value = self.quantity * self.current_price
        buy_value = self.entry_price * self.quantity if self.side == "BUY" else 0.0
        sell_value = self.entry_price * self.quantity if self.side == "SELL" else 0.0

        # ₹-distance to each configured amount target (None if that target
        # isn't set for this position, or once it's closed) — mirrors the
        # existing distance_to_tp_percent/distance_to_sl_percent pattern.
        distance_to_tp_amount = (
            round(self.take_profit_amount - self.pnl, 2)
            if is_open and self.take_profit_amount is not None
            else None
        )
        distance_to_sl_amount = (
            round(self.pnl + self.stop_loss_amount, 2)
            if is_open and self.stop_loss_amount is not None
            else None
        )

        return {
            "id": self.id,
            "option_type": self.option_type,
            "side": self.side,
            "strike": self.strike,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "exchange": self.exchange,
            "instrument_id": self.instrument_id,
            "quantity": self.quantity,
            "entry_price": round(self.entry_price, 2),
            "current_price": round(self.current_price, 2),
            "position_value": round(position_value, 2),
            "buy_value": round(buy_value, 2),
            "sell_value": round(sell_value, 2),
            "entry_time": to_ist(self.entry_time).isoformat() if self.entry_time else None,
            "exit_time": to_ist(self.exit_time).isoformat() if self.exit_time else None,
            "exit_price": round(self.exit_price, 2) if self.exit_price is not None else None,
            "exit_reason": self.exit_reason,
            "status": self.status,
            # `pnl`/`pnl_percent` are this position's live (open) or final
            # (closed) P&L either way; `unrealized_pnl`/`realized_pnl` mirror
            # the same number into the field that names which one applies —
            # a portfolio-wide sum of these is exposed in
            # PaperTradingEngine.to_state_dict()'s "pnl_summary".
            "pnl": round(self.pnl, 2),
            "pnl_percent": round(self.pnl_percent, 2),
            "unrealized_pnl": round(self.pnl, 2) if is_open else None,
            "realized_pnl": round(self.pnl, 2) if not is_open else None,
            "take_profit_percent": self.take_profit_percent,
            "stop_loss_percent": self.stop_loss_percent,
            "take_profit_amount": self.take_profit_amount,
            "stop_loss_amount": self.stop_loss_amount,
            "take_profit_price": round(take_profit_price, 2),
            "stop_loss_price": round(stop_loss_price, 2),
            "distance_to_tp_percent": round(distance_to_tp_percent, 2) if is_open else None,
            "distance_to_sl_percent": round(distance_to_sl_percent, 2) if is_open else None,
            "distance_to_tp_amount": distance_to_tp_amount,
            "distance_to_sl_amount": distance_to_sl_amount,
            "simulated": True,
        }


class PaperTradingEngine:
    """
    Thread-safe simulation-only position book. Each position is a CE/PE at a
    given strike, opened either BUY (long — profits as premium rises) or
    SELL (short/write — profits as premium falls); the P&L math mirrors
    accordingly (see PaperPosition.mark_to_market). No real order routing is
    implemented — this never touches a broker.
    """

    def __init__(self, config: PaperTradeConfig | None = None):
        self._config = config or PaperTradeConfig()
        self._lock = threading.Lock()
        self._positions: dict[str, PaperPosition] = {}
        self._history: list[PaperPosition] = []

    # -- configuration -----------------------------------------------------

    def get_config(self) -> PaperTradeConfig:
        with self._lock:
            return PaperTradeConfig(**self._config.as_dict())

    def set_config(
        self,
        take_profit_percent: float | None = None,
        stop_loss_percent: float | None = None,
        take_profit_amount: float | None = None,
        stop_loss_amount: float | None = None,
    ) -> PaperTradeConfig:
        with self._lock:
            if take_profit_percent is not None:
                if take_profit_percent <= 0:
                    raise ValueError("take_profit_percent must be greater than zero.")
                self._config.take_profit_percent = float(take_profit_percent)
            if stop_loss_percent is not None:
                if stop_loss_percent <= 0:
                    raise ValueError("stop_loss_percent must be greater than zero.")
                self._config.stop_loss_percent = float(stop_loss_percent)
            if take_profit_amount is not None:
                if take_profit_amount <= 0:
                    raise ValueError("take_profit_amount must be greater than zero.")
                self._config.take_profit_amount = float(take_profit_amount)
            if stop_loss_amount is not None:
                if stop_loss_amount <= 0:
                    raise ValueError("stop_loss_amount must be greater than zero.")
                self._config.stop_loss_amount = float(stop_loss_amount)
            return PaperTradeConfig(**self._config.as_dict())

    # -- position lifecycle --------------------------------------------------

    def open_position(
        self,
        option_type: str,
        strike: float,
        quantity: int,
        price: float,
        timestamp: datetime,
        side: str = "BUY",
        contract: Contract | None = None,
    ) -> PaperPosition:
        option_type = (option_type or "").upper()
        side = (side or "BUY").upper()
        if option_type not in VALID_OPTION_TYPES:
            raise ValueError("option_type must be 'CE' or 'PE'.")
        if side not in VALID_SIDES:
            raise ValueError("side must be 'BUY' or 'SELL'.")
        if quantity <= 0:
            raise ValueError("quantity must be greater than zero.")
        if price <= 0:
            raise ValueError("entry price must be greater than zero.")

        with self._lock:
            position = PaperPosition(
                id=uuid.uuid4().hex[:10],
                option_type=option_type,
                side=side,
                strike=float(strike),
                quantity=int(quantity),
                entry_price=float(price),
                current_price=float(price),
                entry_time=timestamp,
                take_profit_percent=self._config.take_profit_percent,
                stop_loss_percent=self._config.stop_loss_percent,
                take_profit_amount=self._config.take_profit_amount,
                stop_loss_amount=self._config.stop_loss_amount,
                underlying=contract.underlying if contract else None,
                expiry=contract.expiry if contract else None,
                exchange=contract.exchange if contract else None,
                instrument_id=contract.instrument_id if contract else None,
            )
            self._positions[position.id] = position
            return position

    def close_position(
        self,
        position_id: str,
        price: float,
        timestamp: datetime,
        reason: str = EXIT_MANUAL,
    ) -> PaperPosition | None:
        with self._lock:
            position = self._positions.get(position_id)
            if position is None or position.status != "OPEN":
                return None
            position.close(price, timestamp, reason)
            del self._positions[position_id]
            self._history.append(position)
            return position

    def update(self, chain: OptionChainSnapshot | None, timestamp: datetime) -> list[PaperPosition]:
        """
        Mark every open position to market from the latest option chain tick,
        then auto-exit anything that has crossed TP/SL. Called once per tick
        from the main pipeline (ticks -> option chain -> ATM -> candles -> UI),
        so paper trading rides the same feed the rest of the system uses.
        """
        if chain is None or not self._positions:
            return []

        price_by_strike = {leg.strike: leg for leg in chain.strikes}
        exited: list[PaperPosition] = []

        with self._lock:
            for position_id in list(self._positions.keys()):
                position = self._positions[position_id]
                leg = price_by_strike.get(position.strike)
                if leg is None:
                    continue  # strike rolled out of chain window; hold last known price
                current = leg.call_ltp if position.option_type == "CE" else leg.put_ltp
                position.mark_to_market(current)

                # Percent and ₹ triggers, both read from what THIS position
                # captured at open time (never the engine's current live
                # config, so a later config change never retroactively moves
                # an already-open position's thresholds). Rule evaluation
                # lives in src/risk.py::evaluate_exit.
                reason = evaluate_exit(position.pnl, position.pnl_percent, position.exit_rule)
                if reason is not None:
                    position.close(current, timestamp, reason)
                    del self._positions[position_id]
                    self._history.append(position)
                    exited.append(position)

        return exited

    # -- state restore (a book is rebuilt from database rows to render a user's state) ----

    def load_state(self, open_positions: list[PaperPosition], history: list[PaperPosition]) -> None:
        """Replace the in-memory book with persisted state (oldest-first history)."""
        with self._lock:
            self._positions = {p.id: p for p in open_positions}
            self._history = list(history)

    # -- read access ---------------------------------------------------------

    def get_open_positions(self) -> list[PaperPosition]:
        with self._lock:
            return list(self._positions.values())

    def get_history(self) -> list[PaperPosition]:
        with self._lock:
            return list(self._history)

    def _stats(self) -> dict:
        """
        Aggregate performance stats over closed trades: win rate, total P&L,
        average win/loss, and max drawdown. Standard aggregate arithmetic over
        already-realized P&L — not a business formula requiring confirmation.
        Max drawdown walks the closed trades in the order they exited (the
        book's actual equity curve) and tracks the largest peak-to-trough dip
        in cumulative P&L.
        """
        closed = self._history
        total_trades = len(closed)
        if total_trades == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_percent": 0.0,
                "total_pnl": 0.0,
                "average_win": 0.0,
                "average_loss": 0.0,
                "max_drawdown": 0.0,
            }

        wins = [p.pnl for p in closed if p.pnl > 0]
        losses = [p.pnl for p in closed if p.pnl < 0]
        total_pnl = sum(p.pnl for p in closed)

        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for p in closed:
            cumulative += p.pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)

        return {
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_percent": round((len(wins) / total_trades) * 100.0, 2),
            "total_pnl": round(total_pnl, 2),
            "average_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "average_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "max_drawdown": round(max_drawdown, 2),
        }

    def _pnl_summary(self) -> dict:
        """
        Portfolio-wide P&L and turnover, combining open (unrealized) and
        closed (realized) positions — the numbers the top-level Paper Trading
        summary displays, distinct from the per-trade `pnl` on each position
        and from `_stats()`'s closed-trades-only win/loss aggregates.

        Turnover = Buy Value + Sell Value (capital deployed, by side) — never
        conflated with profit. Buy/Sell Value are each position's entry
        value (entry_price × quantity), summed across every position ever
        opened (open + closed) in this session.

        `total_pnl_percent` = total_pnl / total_turnover — a reasonable
        portfolio-level return metric, not a client-confirmed formula (same
        placeholder-and-isolated status as the CE+PE combined_value formula
        elsewhere in this codebase); swap here if the client specifies
        something else.
        """
        open_positions = list(self._positions.values())
        closed_positions = self._history

        unrealized_pnl = sum(p.pnl for p in open_positions)
        realized_pnl = sum(p.pnl for p in closed_positions)
        total_pnl = unrealized_pnl + realized_pnl

        all_positions = open_positions + closed_positions
        buy_turnover = sum(p.entry_price * p.quantity for p in all_positions if p.side == "BUY")
        sell_turnover = sum(p.entry_price * p.quantity for p in all_positions if p.side == "SELL")
        total_turnover = buy_turnover + sell_turnover

        total_position_value = sum(p.quantity * p.current_price for p in open_positions)

        return {
            "unrealized_pnl": round(unrealized_pnl, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_percent": round((total_pnl / total_turnover) * 100.0, 2) if total_turnover else 0.0,
            "buy_turnover": round(buy_turnover, 2),
            "sell_turnover": round(sell_turnover, 2),
            "total_turnover": round(total_turnover, 2),
            "total_position_value": round(total_position_value, 2),
            "open_position_count": len(open_positions),
        }

    def to_state_dict(self) -> dict:
        with self._lock:
            open_positions = [p.to_dict() for p in self._positions.values()]
            history = [p.to_dict() for p in reversed(self._history[-50:])]
            return {
                "simulated": True,
                "config": self._config.as_dict(),
                "open_positions": open_positions,
                "history": history,
                "stats": self._stats(),
                "pnl_summary": self._pnl_summary(),
            }
