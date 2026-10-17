"""Housekeeping for the shared database. Run from a scheduled job (cron, a platform scheduler).

    python -m src.db.maintenance purge-history --keep-days 90
    python -m src.db.maintenance purge-expired                 # old refresh tokens and idempotency keys
    python -m src.db.maintenance stats

Market history grows by roughly one ATM snapshot per 5 seconds and a few candles per minute per
tracked strike, so a trading year is large. `purge-history` deletes whole past trading days older than
the retention window; it never touches today. Every command prints what it did and is safe to repeat.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from src.db.history import SqlPersistenceStore
from src.db.models import AuditLog, IdempotencyKey, MarketCandle, MarketChainSnapshot, PaperPositionRow, RefreshToken, User
from src.db.session import session_scope
from src.timeutil import today_ist

IDEMPOTENCY_RETENTION_DAYS = 2  # a client has no reason to retry a request after this long
REFRESH_TOKEN_GRACE_DAYS = 7  # keep expired tokens briefly so replay detection can still recognise them


def purge_history(keep_days: int) -> int:
    """Delete candles and ATM snapshots from days before today minus `keep_days`. Returns rows removed."""
    if keep_days < 1:
        raise ValueError("keep_days must be at least 1")
    cutoff = (today_ist() - timedelta(days=keep_days)).isoformat()
    return SqlPersistenceStore().purge_before(cutoff)


def purge_expired(now: datetime | None = None) -> dict[str, int]:
    """Delete idempotency keys and refresh tokens that can no longer matter."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        keys = session.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < now - timedelta(days=IDEMPOTENCY_RETENTION_DAYS))).rowcount or 0
        tokens = session.execute(delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=REFRESH_TOKEN_GRACE_DAYS))).rowcount or 0
    return {"idempotency_keys": keys, "refresh_tokens": tokens}


def stats() -> dict[str, int | str | None]:
    with session_scope() as session:
        count = lambda model: int(session.scalar(select(func.count()).select_from(model)) or 0)  # noqa: E731
        return {
            "users": count(User),
            "paper_positions": count(PaperPositionRow),
            "open_positions": int(session.scalar(select(func.count()).select_from(PaperPositionRow).where(PaperPositionRow.status == "OPEN")) or 0),
            "market_candles": count(MarketCandle),
            "market_chain_snapshots": count(MarketChainSnapshot),
            "refresh_tokens": count(RefreshToken),
            "audit_log": count(AuditLog),
            "history_from": session.scalar(select(func.min(MarketCandle.trading_date))),
            "history_to": session.scalar(select(func.max(MarketCandle.trading_date))),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.db.maintenance", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    purge = sub.add_parser("purge-history")
    purge.add_argument("--keep-days", type=int, required=True)
    sub.add_parser("purge-expired")
    sub.add_parser("stats")
    args = parser.parse_args(argv)
    if args.command == "purge-history":
        try:
            print({"removed_rows": purge_history(args.keep_days)})
        except ValueError as exc:
            print(str(exc))
            return 2
    elif args.command == "purge-expired":
        print(purge_expired())
    else:
        print(stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
