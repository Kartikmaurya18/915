"""Relational schema for users, auth sessions, and per-user paper trading.

Market data (candles, option-chain snapshots) is global and read-only and is
NOT modelled here; only user-owned state is. Every user-owned table carries
`user_id`, and every query against it must filter on it (see src/paper/service.py).

Money columns are floats, matching the existing in-memory paper engine's math.
That is acceptable for simulated P&L; a real-money system would need NUMERIC.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    """Always store UTC and always return timezone-aware UTC datetimes.

    SQLite discards tzinfo; PostgreSQL's timestamptz keeps it. Normalising on
    both sides makes the two behave identically.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; pass a timezone-aware value")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class RefreshToken(Base):
    """One opaque refresh token. Only its SHA-256 is stored.

    Tokens are rotated on every use; `family_id` groups a rotation chain so
    that replaying an already-used token revokes the whole chain.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    family_id: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )


class PaperSettings(Base):
    """A user's own take-profit / stop-loss defaults for newly opened positions."""

    __tablename__ = "paper_settings"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    take_profit_percent: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_percent: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow)


class PaperPositionRow(Base):
    """A simulated position, open or closed. Composite key keeps ids unique per
    user without needing a global id space."""

    __tablename__ = "paper_positions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    take_profit_percent: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_percent: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="OPEN")
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_time: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pnl_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    underlying: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    expiry: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    exchange: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    instrument_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Asia/Kolkata trading date of the entry, "YYYY-MM-DD" (see src/timeutil.py).
    trading_date: Mapped[str] = mapped_column(String(10), nullable=False)

    __table_args__ = (
        Index("ix_paper_positions_user_status", "user_id", "status"),
        Index("ix_paper_positions_user_date", "user_id", "trading_date"),
    )


class IdempotencyKey(Base):
    """Lets a client safely retry a mutating request: the same (user, key)
    returns the stored response instead of acting twice."""

    __tablename__ = "idempotency_keys"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow)


class AuditLog(Base):
    """Append-only record of security-relevant and trading-relevant events."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_audit_log_user_created", "user_id", "created_at"),
        Index("ix_audit_log_event_created", "event", "created_at"),
    )


# -- market history (global, written by the engine's leader, read by everyone) -----------------
# Times are stored as ISO-8601 text with the +05:30 offset, exactly as the original SQLite store
# kept them, so ordering and every reader behave identically on both backends.


class MarketCandle(Base):
    """A completed candle for the underlying or one strike leg ("24800.0_ce" / "_pe" / "_straddle")."""

    __tablename__ = "market_candles"

    symbol: Mapped[str] = mapped_column(String(48), primary_key=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, primary_key=True)
    open_time: Mapped[str] = mapped_column(String(40), primary_key=True)
    close_time: Mapped[str] = mapped_column(String(40), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    tick_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trading_date: Mapped[str] = mapped_column(String(10), nullable=False)

    __table_args__ = (
        Index("ix_market_candles_date", "symbol", "interval_minutes", "trading_date"),
        Index("ix_market_candles_trading_date", "trading_date"),
    )


class MarketChainSnapshot(Base):
    """A periodic ATM-focused option-chain snapshot."""

    __tablename__ = "market_chain_snapshots"

    id: Mapped[int] = mapped_column(Integer().with_variant(BigInteger, "postgresql"), primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying_ltp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expiry: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    atm_strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atm_call_ltp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atm_put_ltp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    straddle_premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    timestamp: Mapped[str] = mapped_column(String(40), nullable=False)
    trading_date: Mapped[str] = mapped_column(String(10), nullable=False)

    __table_args__ = (Index("ix_market_chain_lookup", "underlying", "trading_date", "timestamp"),)


class LegacyPaperTrade(Base):
    """Closed trades of the single global paper book (AUTH_MODE=disabled). Signed-in users' trades
    live in paper_positions; this exists so local development keeps its history on the same backend."""

    __tablename__ = "legacy_paper_trades"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[str] = mapped_column(String(40), nullable=False)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_time: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trading_date: Mapped[str] = mapped_column(String(10), nullable=False)

    __table_args__ = (Index("ix_legacy_paper_trades_date", "trading_date"),)


__all__ = [
    "LegacyPaperTrade",
    "MarketCandle",
    "MarketChainSnapshot",
    "AuditLog",
    "Base",
    "IdempotencyKey",
    "PaperPositionRow",
    "PaperSettings",
    "RefreshToken",
    "User",
    "UTCDateTime",
]
