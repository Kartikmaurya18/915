"""account verification: phone number, verified-at stamps and one-time-code challenges

Existing accounts are stamped as email-verified (they registered before verification existed), so the
upgrade never locks anyone out. They have no phone number and are not asked for one.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("phone", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_unique_constraint("uq_users_phone", ["phone"])
    op.execute("UPDATE users SET email_verified_at = created_at WHERE email_verified_at IS NULL")

    op.create_table(
        "otp_challenges",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("destination", sa.String(length=254), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("otp_challenges", schema=None) as batch_op:
        batch_op.create_index("ix_otp_user_purpose_channel", ["user_id", "purpose", "channel", "created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("otp_challenges", schema=None) as batch_op:
        batch_op.drop_index("ix_otp_user_purpose_channel")
    op.drop_table("otp_challenges")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("uq_users_phone", type_="unique")
        batch_op.drop_column("phone_verified_at")
        batch_op.drop_column("email_verified_at")
        batch_op.drop_column("phone")
