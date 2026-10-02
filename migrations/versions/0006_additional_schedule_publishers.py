"""Keep owner-confirmed additional schedule publishers without losing the first one.

Revision ID: 0006_schedule_publishers
Revises: 0005_relative_homework_due
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0006_schedule_publishers"
down_revision = "0005_relative_homework_due"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_chats", sa.Column(
        "additional_schedule_publisher_user_ids", JSONB(), nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ))


def downgrade():
    op.drop_column("source_chats", "additional_schedule_publisher_user_ids")
