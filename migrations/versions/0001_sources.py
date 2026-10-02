"""Store Telegram Desktop source snapshots.

Revision ID: 0001_sources
Revises:
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_sources"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_chats",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("desktop_export_chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("name", sa.Text()),
        sa.Column("chat_type", sa.String(40)),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("source_chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_type", sa.String(30), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
        sa.Column("sender_ref", sa.Text()),
        sa.Column("sender_telegram_user_id", sa.BigInteger()),
        sa.Column("sender_name", sa.Text()),
        sa.Column("reply_to_telegram_id", sa.BigInteger()),
        sa.Column("forwarded_from", sa.Text()),
        sa.Column("forwarded_from_id", sa.Text()),
        sa.Column("grouped_id", sa.BigInteger()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("chat_id", "telegram_message_id"),
    )
    op.create_table(
        "message_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("source_edited_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", "revision_no"),
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("message_revision_id", sa.BigInteger(), sa.ForeignKey("message_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("mime_type", sa.Text()),
        sa.Column("export_path", sa.Text()),
        sa.Column("declared_size", sa.BigInteger()),
        sa.Column("actual_size", sa.BigInteger()),
        sa.Column("source_status", sa.String(30), nullable=False),
        sa.Column("local_status", sa.String(30), nullable=False),
        sa.UniqueConstraint("message_revision_id", "ordinal"),
    )


def downgrade():
    op.drop_table("attachments")
    op.drop_table("message_revisions")
    op.drop_table("messages")
    op.drop_table("source_chats")
