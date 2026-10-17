"""One-off imports from the original local SQLite file (data/market_data.db) into the shared database.

    python -m src.db.import_legacy history       --sqlite data/market_data.db
    python -m src.db.import_legacy paper-trades  --sqlite data/market_data.db --email you@example.com

Both are safe to re-run: history skips trading dates that already exist in the destination, and a
paper trade that is already in the user's book is left alone. The source file is only read.
DATABASE_URL selects the destination, exactly as for the app.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from src.db.history import _insert
from src.db.models import LegacyPaperTrade, MarketCandle, MarketChainSnapshot, PaperPositionRow, User
from src.db.session import session_scope

CHUNK = 2000


def _open_source(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No SQLite file at {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)  # read-only: the source is never modified
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _chunks(cursor):
    while True:
        rows = cursor.fetchmany(CHUNK)
        if not rows:
            return
        yield rows


def import_history(sqlite_path: str | Path) -> dict[str, int]:
    """Copy candles and option-chain snapshots. Returns how many rows were added per table."""
    added = {"candles": 0, "chain_snapshots": 0, "legacy_paper_trades": 0}
    src = _open_source(sqlite_path)
    try:
        if _has_table(src, "candles"):
            cursor = src.execute(
                "SELECT symbol, interval_minutes, open_time, close_time, open, high, low, close, tick_count, trading_date "
                "FROM candles WHERE trading_date != ''"
            )
            for rows in _chunks(cursor):
                with session_scope() as session:
                    stmt = _insert(session, MarketCandle).values([dict(r) for r in rows]).on_conflict_do_nothing()
                    # RETURNING, not rowcount: PostgreSQL's driver reports -1 for a multi-row insert.
                    added["candles"] += len(session.execute(stmt.returning(MarketCandle.symbol)).all())

        if _has_table(src, "option_chain_snapshots"):
            with session_scope() as session:
                have = set(session.scalars(select(MarketChainSnapshot.trading_date).distinct()))
            cursor = src.execute(
                "SELECT underlying, underlying_ltp, expiry, atm_strike, atm_call_ltp, atm_put_ltp, straddle_premium, timestamp, trading_date "
                "FROM option_chain_snapshots WHERE trading_date != ''"
            )
            for rows in _chunks(cursor):
                fresh = [dict(r) for r in rows if r["trading_date"] not in have]  # skip days that are already there
                if fresh:
                    with session_scope() as session:
                        session.execute(MarketChainSnapshot.__table__.insert(), fresh)
                    added["chain_snapshots"] += len(fresh)

        if _has_table(src, "paper_trades"):
            cols = [c.name for c in LegacyPaperTrade.__table__.columns]
            cursor = src.execute(f"SELECT {', '.join(cols)} FROM paper_trades")
            for rows in _chunks(cursor):
                with session_scope() as session:
                    stmt = _insert(session, LegacyPaperTrade).values([dict(r) for r in rows]).on_conflict_do_nothing()
                    added["legacy_paper_trades"] += len(session.execute(stmt.returning(LegacyPaperTrade.id)).all())
    finally:
        src.close()
    return added


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without a timezone: {value!r}")
    return parsed


def import_paper_trades(sqlite_path: str | Path, email: str) -> dict[str, int]:
    """Copy the old single-book closed trades into one user's account."""
    src = _open_source(sqlite_path)
    result = {"imported": 0, "already_present": 0, "skipped": 0}
    try:
        if not _has_table(src, "paper_trades"):
            return result
        with session_scope() as session:
            user = session.scalar(select(User).where(User.email == email.strip().lower()))
            if user is None:
                raise LookupError(f"No user with email {email!r}. Register first, then import.")
            user_id = user.id
        for row in src.execute("SELECT * FROM paper_trades ORDER BY entry_time"):
            try:
                entry, exit_time = _aware(row["entry_time"]), _aware(row["exit_time"])
                if entry is None or row["exit_price"] is None:
                    raise ValueError("incomplete trade")
                values = dict(
                    option_type=row["option_type"], side=row["side"], strike=row["strike"], quantity=row["quantity"],
                    entry_price=row["entry_price"], current_price=row["exit_price"], entry_time=entry,
                    take_profit_percent=row["take_profit_percent"] or 0.0, stop_loss_percent=row["stop_loss_percent"] or 0.0,
                    take_profit_amount=row["take_profit_amount"], stop_loss_amount=row["stop_loss_amount"],
                    status="EXITED", exit_price=row["exit_price"], exit_time=exit_time, exit_reason=row["exit_reason"],
                    pnl=row["pnl"] or 0.0, pnl_percent=row["pnl_percent"] or 0.0, trading_date=row["trading_date"],
                )
            except (ValueError, TypeError, KeyError):
                result["skipped"] += 1
                continue
            with session_scope() as session:
                if session.get(PaperPositionRow, (user_id, row["id"])) is not None:
                    result["already_present"] += 1
                    continue
                session.add(PaperPositionRow(user_id=user_id, id=row["id"], **values))
                result["imported"] += 1
    finally:
        src.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.db.import_legacy", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("history", "paper-trades"):
        p = sub.add_parser(name)
        p.add_argument("--sqlite", default="data/market_data.db", help="the original SQLite file")
        if name == "paper-trades":
            p.add_argument("--email", required=True, help="the account that receives the trades")
    args = parser.parse_args(argv)
    try:
        result = import_history(args.sqlite) if args.command == "history" else import_paper_trades(args.sqlite, args.email)
    except (FileNotFoundError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
