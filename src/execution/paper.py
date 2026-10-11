"""
Paper (simulated) execution: fills every order instantly at its reference
price inside PaperTradingEngine. Never contacts a broker.
"""

from __future__ import annotations

from datetime import datetime

from src.execution.base import ExecutionClient, OrderRequest, OrderResult
from src.paper_trading import PaperTradingEngine


class PaperExecutionClient(ExecutionClient):
    mode = "paper"
    simulated = True

    def __init__(self, book: PaperTradingEngine):
        self.book = book

    def place_order(self, request: OrderRequest) -> OrderResult:
        position = self.book.open_position(
            request.contract.option_type,
            request.contract.strike,
            request.quantity,
            request.reference_price,
            request.timestamp,
            side=request.side,
            contract=request.contract,
        )
        return OrderResult(
            accepted=True,
            order_id=position.id,
            fill_price=position.entry_price,
            simulated=True,
            position=position,
        )

    def close_position(self, position_id: str, price: float, timestamp: datetime, reason: str) -> OrderResult:
        position = self.book.close_position(position_id, price, timestamp, reason=reason)
        if position is None:
            return OrderResult(False, position_id, None, True, "Position not found or already closed.")
        return OrderResult(True, position_id, position.exit_price, True, position=position)
