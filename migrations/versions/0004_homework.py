"""Store current homework with stable source identities.

Revision ID: 0004_homework
Revises: 0003_text_schedule
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0004_homework"
down_revision = "0003_text_schedule"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "homework",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("source_chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("subject_key", sa.String(80)),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Date()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("sources", JSONB(), nullable=False),
        sa.Column("attachments", JSONB(), nullable=False),
        sa.Column("owner_override", JSONB()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", "root_message_id", "part_index"),
    )


def downgrade():
    op.drop_table("homework")
