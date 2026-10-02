"""Retain explicit relative homework deadlines without inventing a calendar date.

Revision ID: 0005_relative_homework_due
Revises: 0004_homework
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_relative_homework_due"
down_revision = "0004_homework"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("homework", sa.Column("due_text", sa.Text()))


def downgrade():
    op.drop_column("homework", "due_text")
