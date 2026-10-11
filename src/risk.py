"""
Pure risk calculations, separate from the paper trading book and the UI.

Paper trading uses these today (P&L, TP/SL exit evaluation). A future risk
engine — portfolio loss limits, multi-leg strategy P&L, pre-trade checks —
builds on the same functions instead of re-deriving the math. Nothing here
blocks or places orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_MANUAL = "MANUAL"


def position_pnl(side: str, entry_price: float, price: float, quantity: int) -> tuple[float, float]:
    """(pnl, pnl_percent) for one leg. BUY profits as price rises, SELL
    (short/write) as it falls. pnl_percent is signed toward the position's
    own profit direction, so TP/SL comparisons are side-agnostic."""
    direction = 1 if side == "BUY" else -1
    pnl = (price - entry_price) * quantity * direction
    pnl_percent = ((price - entry_price) / entry_price) * 100.0 * direction if entry_price else 0.0
    return pnl, pnl_percent


@dataclass(frozen=True)
class ExitRule:
    """TP/SL thresholds. Percent triggers are always active; ₹ (absolute
    P&L) triggers are optional (None = disabled). Whichever hits first wins."""

    take_profit_percent: float
    stop_loss_percent: float
    take_profit_amount: Optional[float] = None
    stop_loss_amount: Optional[float] = None


def evaluate_exit(pnl: float, pnl_percent: float, rule: ExitRule) -> Optional[str]:
    """EXIT_TAKE_PROFIT / EXIT_STOP_LOSS if a threshold has been reached,
    else None. TP is checked first when a single move crosses both."""
    take_profit_hit = pnl_percent >= rule.take_profit_percent or (
        rule.take_profit_amount is not None and pnl >= rule.take_profit_amount
    )
    if take_profit_hit:
        return EXIT_TAKE_PROFIT
    stop_loss_hit = pnl_percent <= -rule.stop_loss_percent or (
        rule.stop_loss_amount is not None and pnl <= -rule.stop_loss_amount
    )
    if stop_loss_hit:
        return EXIT_STOP_LOSS
    return None


class PositionLike(Protocol):
    side: str
    quantity: int
    entry_price: float
    current_price: float
    pnl: float
    status: str


def aggregate_pnl(positions: Iterable[PositionLike]) -> dict:
    """Combined P&L across several legs (e.g. every leg of a straddle or
    iron condor, or a whole book) — the building block for multi-leg and
    portfolio-level limits."""
    unrealized = realized = entry_value = 0.0
    for p in positions:
        entry_value += p.entry_price * p.quantity
        if p.status == "OPEN":
            unrealized += p.pnl
        else:
            realized += p.pnl
    return {
        "unrealized_pnl": unrealized,
        "realized_pnl": realized,
        "total_pnl": unrealized + realized,
        "entry_value": entry_value,
    }
