"""Durable channel publication state.

Revision ID: 0007_channel_posts
Revises: 0006_schedule_publishers
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_channel_posts"
down_revision = "0006_schedule_publishers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("channel_states",
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("channel_posts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("source_chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schedule_date", sa.Date(), nullable=False),
        sa.Column("message_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", "schedule_date"))


def downgrade():
    op.drop_table("channel_posts")
    op.drop_table("channel_states")
