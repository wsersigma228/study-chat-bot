"""Track owner review messages without notifying the entire old backlog.

Revision ID: 0008_homework_review_notices
Revises: 0007_channel_posts
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_homework_review_notices"
down_revision = "0007_channel_posts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "homework_review_notices",
        sa.Column("homework_id", sa.BigInteger(), sa.ForeignKey("homework.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("notification_message_id", sa.BigInteger()),
        sa.Column("reply_message_id", sa.BigInteger()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Keep the existing archive quiet; recent unclear items still get a review request.
    op.execute("""
        INSERT INTO homework_review_notices (homework_id, source_updated_at, status, updated_at)
        SELECT id, updated_at, 'skipped', now() FROM homework
        WHERE status = 'needs_review' AND updated_at < now() - interval '24 hours'
    """)


def downgrade():
    op.drop_table("homework_review_notices")
