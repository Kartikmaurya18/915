"""initial schema: users, auth, paper trading, audit

Revision ID: 0001
Revises: 
Create Date: 2026-10-16 00:28:13.079747
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=True),
    sa.Column('event', sa.String(length=48), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index('ix_audit_log_event_created', ['event', 'created_at'], unique=False)
        batch_op.create_index('ix_audit_log_user_created', ['user_id', 'created_at'], unique=False)

    op.create_table('users',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('idempotency_keys',
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('response', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'key')
    )
    op.create_table('paper_positions',
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('option_type', sa.String(length=2), nullable=False),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('strike', sa.Float(), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('entry_price', sa.Float(), nullable=False),
    sa.Column('current_price', sa.Float(), nullable=False),
    sa.Column('entry_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('take_profit_percent', sa.Float(), nullable=False),
    sa.Column('stop_loss_percent', sa.Float(), nullable=False),
    sa.Column('take_profit_amount', sa.Float(), nullable=True),
    sa.Column('stop_loss_amount', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('exit_price', sa.Float(), nullable=True),
    sa.Column('exit_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('exit_reason', sa.String(length=16), nullable=True),
    sa.Column('pnl', sa.Float(), nullable=False),
    sa.Column('pnl_percent', sa.Float(), nullable=False),
    sa.Column('underlying', sa.String(length=32), nullable=True),
    sa.Column('expiry', sa.String(length=16), nullable=True),
    sa.Column('exchange', sa.String(length=16), nullable=True),
    sa.Column('instrument_id', sa.String(length=64), nullable=True),
    sa.Column('trading_date', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    with op.batch_alter_table('paper_positions', schema=None) as batch_op:
        batch_op.create_index('ix_paper_positions_user_date', ['user_id', 'trading_date'], unique=False)
        batch_op.create_index('ix_paper_positions_user_status', ['user_id', 'status'], unique=False)

    op.create_table('paper_settings',
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('take_profit_percent', sa.Float(), nullable=False),
    sa.Column('stop_loss_percent', sa.Float(), nullable=False),
    sa.Column('take_profit_amount', sa.Float(), nullable=True),
    sa.Column('stop_loss_amount', sa.Float(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('refresh_tokens',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('family_id', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.create_index('ix_refresh_tokens_family_id', ['family_id'], unique=False)
        batch_op.create_index('ix_refresh_tokens_user_id', ['user_id'], unique=False)




def downgrade() -> None:

    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.drop_index('ix_refresh_tokens_user_id')
        batch_op.drop_index('ix_refresh_tokens_family_id')

    op.drop_table('refresh_tokens')
    op.drop_table('paper_settings')
    with op.batch_alter_table('paper_positions', schema=None) as batch_op:
        batch_op.drop_index('ix_paper_positions_user_status')
        batch_op.drop_index('ix_paper_positions_user_date')

    op.drop_table('paper_positions')
    op.drop_table('idempotency_keys')
    op.drop_table('users')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_log_user_created')
        batch_op.drop_index('ix_audit_log_event_created')

    op.drop_table('audit_log')

