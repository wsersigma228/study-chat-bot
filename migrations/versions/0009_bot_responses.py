"""Persist editable bot responses and group schedule posts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_bot_responses"
down_revision = "0008_homework_review_notices"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("channel_posts", sa.Column("destination_type", sa.String(30),
                                             nullable=False, server_default="channel"))
    op.add_column("channel_posts", sa.Column("keyboard_date", sa.Date()))
    op.create_table("bot_responses",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("source_chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("destination_type", sa.String(30), nullable=False),
        sa.Column("schedule_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("keyboard_date", sa.Date(), nullable=False),
        sa.Column("message_ids", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("bot_responses")
    op.drop_column("channel_posts", "keyboard_date")
    op.drop_column("channel_posts", "destination_type")
