"""market history: candles, ATM snapshots and legacy paper trades in the shared database

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_candles",
        sa.Column("symbol", sa.String(length=48), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("open_time", sa.String(length=40), nullable=False),
        sa.Column("close_time", sa.String(length=40), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("tick_count", sa.Integer(), nullable=False),
        sa.Column("trading_date", sa.String(length=10), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "interval_minutes", "open_time"),
    )
    with op.batch_alter_table("market_candles", schema=None) as batch_op:
        batch_op.create_index("ix_market_candles_date", ["symbol", "interval_minutes", "trading_date"], unique=False)
        batch_op.create_index("ix_market_candles_trading_date", ["trading_date"], unique=False)

    op.create_table(
        "market_chain_snapshots",
        sa.Column("id", sa.Integer().with_variant(sa.BigInteger(), "postgresql"), autoincrement=True, nullable=False),
        sa.Column("underlying", sa.String(length=32), nullable=False),
        sa.Column("underlying_ltp", sa.Float(), nullable=True),
        sa.Column("expiry", sa.String(length=16), nullable=True),
        sa.Column("atm_strike", sa.Float(), nullable=True),
        sa.Column("atm_call_ltp", sa.Float(), nullable=True),
        sa.Column("atm_put_ltp", sa.Float(), nullable=True),
        sa.Column("straddle_premium", sa.Float(), nullable=True),
        sa.Column("timestamp", sa.String(length=40), nullable=False),
        sa.Column("trading_date", sa.String(length=10), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("market_chain_snapshots", schema=None) as batch_op:
        batch_op.create_index("ix_market_chain_lookup", ["underlying", "trading_date", "timestamp"], unique=False)

    op.create_table(
        "legacy_paper_trades",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("entry_time", sa.String(length=40), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_time", sa.String(length=40), nullable=True),
        sa.Column("exit_reason", sa.String(length=16), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("pnl_percent", sa.Float(), nullable=True),
        sa.Column("take_profit_percent", sa.Float(), nullable=True),
        sa.Column("stop_loss_percent", sa.Float(), nullable=True),
        sa.Column("take_profit_amount", sa.Float(), nullable=True),
        sa.Column("stop_loss_amount", sa.Float(), nullable=True),
        sa.Column("trading_date", sa.String(length=10), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("legacy_paper_trades", schema=None) as batch_op:
        batch_op.create_index("ix_legacy_paper_trades_date", ["trading_date"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("legacy_paper_trades", schema=None) as batch_op:
        batch_op.drop_index("ix_legacy_paper_trades_date")
    op.drop_table("legacy_paper_trades")
    with op.batch_alter_table("market_chain_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_market_chain_lookup")
    op.drop_table("market_chain_snapshots")
    with op.batch_alter_table("market_candles", schema=None) as batch_op:
        batch_op.drop_index("ix_market_candles_trading_date")
        batch_op.drop_index("ix_market_candles_date")
    op.drop_table("market_candles")
