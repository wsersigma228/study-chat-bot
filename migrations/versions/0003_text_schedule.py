"""Store the current text schedule projection and unresolved candidates.

Revision ID: 0003_text_schedule
Revises: 0002_collector
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0003_text_schedule"
down_revision = "0002_collector"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "schedule_days",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("source_chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schedule_date", sa.Date(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", "schedule_date"),
    )
    op.create_table(
        "schedule_reviews",
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("schedule_date", sa.Date()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("context_ids", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("schedule_reviews")
    op.drop_table("schedule_days")
