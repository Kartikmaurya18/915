"""
Execution interface: the boundary between "the app wants a position" and
"something fills it".

    Paper trading buttons / future strategy engine
                    │  OrderRequest (provider-independent Contract)
                    ▼
            ExecutionClient.place_order()
                    │
          ┌─────────┴──────────┐
    PaperExecutionClient   (future) broker execution client
      [implemented]          [NOT implemented — no real orders]

Only PaperExecutionClient exists. A broker client would implement this same
interface (mapping Contract.instrument_id to the broker's security id) and
be registered in src/execution/__init__.py::build_execution_client — the
callers above would not change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.models import Contract


@dataclass(frozen=True)
class OrderRequest:
    contract: Contract
    side: str  # "BUY" | "SELL"
    quantity: int
    # Latest market price of the contract when the request was made. Paper
    # execution fills at this price; a broker client would use it only as a
    # reference (e.g. for a limit or a slippage check).
    reference_price: float
    timestamp: datetime
    order_type: str = "MARKET"
    limit_price: Optional[float] = None


@dataclass
class OrderResult:
    accepted: bool
    order_id: Optional[str]
    fill_price: Optional[float]
    simulated: bool
    message: Optional[str] = None
    # The position the fill created/closed — PaperPosition for paper.
    position: object | None = field(default=None, repr=False)


class ExecutionClient(ABC):
    #: "paper" for simulation; a broker client would report its own name.
    mode: str = "abstract"
    simulated: bool = True

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult:
        """Open a position for `request`."""

    @abstractmethod
    def close_position(self, position_id: str, price: float, timestamp: datetime, reason: str) -> OrderResult:
        """Exit an open position."""
