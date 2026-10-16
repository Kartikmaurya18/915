"""The single place take-profit / stop-loss exits are decided.

Runs next to the engine (the ingestor, or the one process in embedded mode) and is
called once per accepted tick with the option chain. It reads every user's OPEN
positions from the database, marks them to the tick's prices, and closes whatever
crossed a threshold with a conditional UPDATE - so a concurrent manual close from a
gateway cannot be double-counted.

Because it holds no state, a failover simply means the next leader's worker picks up
exactly where the database says things are.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from src.auth.service import audit
from src.db.models import PaperPositionRow
from src.db.session import session_scope
from src.paper.service import close_if_open, row_to_position
from src.risk import evaluate_exit

log = logging.getLogger("market_data.paper")


class PaperExitWorker:
    def __init__(self, on_exits: Callable[[set[str]], None] | None = None) -> None:
        self.exits = 0  # for tests and metrics
        # Called with the ids of users whose positions were closed, AFTER the transaction committed,
        # so a notified client that refetches immediately always sees the closed position.
        self._on_exits = on_exits

    def on_chain(self, chain, timestamp: datetime) -> int:
        """Apply exits for one tick. Returns how many positions were closed."""
        if chain is None:
            return 0
        legs = {leg.strike: leg for leg in chain.strikes}
        closed = 0
        affected: set[str] = set()
        with session_scope() as session:
            rows = session.scalars(select(PaperPositionRow).where(PaperPositionRow.status == "OPEN")).all()
            for row in rows:
                if row.underlying and row.underlying != chain.underlying:
                    continue
                leg = legs.get(row.strike)
                if leg is None:
                    continue  # strike rolled out of the chain window: hold the last known price
                price = leg.call_ltp if row.option_type == "CE" else leg.put_ltp
                position = row_to_position(row)
                position.mark_to_market(price)
                reason = evaluate_exit(position.pnl, position.pnl_percent, position.exit_rule)
                if reason is None:
                    continue
                position.close(price, timestamp, reason)
                if close_if_open(session, row.user_id, position):
                    audit(
                        session,
                        "paper_auto_exit",
                        row.user_id,
                        None,
                        {"id": position.id, "reason": reason, "pnl": round(position.pnl, 2)},
                    )
                    closed += 1
                    affected.add(row.user_id)
        self.exits += closed
        if affected and self._on_exits is not None:
            try:
                self._on_exits(affected)
            except Exception:
                log.exception("paper exit notification failed")  # notices are best effort; polling still catches up
        return closed

    def __call__(self, chain, timestamp: datetime) -> None:  # engine chain-listener signature
        try:
            self.on_chain(chain, timestamp)
        except Exception:
            # A database hiccup must not break the tick pipeline; positions are re-evaluated next tick.
            log.exception("paper exit worker failed")
