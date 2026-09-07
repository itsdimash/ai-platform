"""add table_data to chat_messages

Revision ID: a3f81c9e2b47
Revises: e639378d1f38
Create Date: 2026-09-07 00:00:00.000000

"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "a3f81c9e2b47"
down_revision = "e639378d1f38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("table_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "table_data")
