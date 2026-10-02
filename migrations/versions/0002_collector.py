"""Bind one verified Telegram peer and track live collection.

Revision ID: 0002_collector
Revises: 0001_sources
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_collector"
down_revision = "0001_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_chats", sa.Column("telegram_peer_id", sa.BigInteger()))
    op.add_column("source_chats", sa.Column("collector_account_user_id", sa.BigInteger()))
    op.add_column("source_chats", sa.Column("schedule_publisher_user_id", sa.BigInteger()))
    op.add_column("source_chats", sa.Column("confirmed_at", sa.DateTime(timezone=True)))
    op.add_column("source_chats", sa.Column("last_history_sync_id", sa.BigInteger()))
    op.add_column("source_chats", sa.Column("last_history_sync_at", sa.DateTime(timezone=True)))
    op.add_column("source_chats", sa.Column("last_live_event_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint("uq_source_chats_telegram_peer_id", "source_chats", ["telegram_peer_id"])
    op.add_column("messages", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("attachments", sa.Column("telegram_media_id", sa.BigInteger()))
    op.add_column("attachments", sa.Column("storage_key", sa.Text()))
    op.add_column("attachments", sa.Column("sha256", sa.String(64)))
    op.add_column("attachments", sa.Column("error_code", sa.String(50)))


def downgrade():
    op.drop_column("attachments", "error_code")
    op.drop_column("attachments", "sha256")
    op.drop_column("attachments", "storage_key")
    op.drop_column("attachments", "telegram_media_id")
    op.drop_column("messages", "deleted_at")
    op.drop_constraint("uq_source_chats_telegram_peer_id", "source_chats", type_="unique")
    op.drop_column("source_chats", "last_live_event_at")
    op.drop_column("source_chats", "last_history_sync_at")
    op.drop_column("source_chats", "last_history_sync_id")
    op.drop_column("source_chats", "confirmed_at")
    op.drop_column("source_chats", "schedule_publisher_user_id")
    op.drop_column("source_chats", "collector_account_user_id")
    op.drop_column("source_chats", "telegram_peer_id")
