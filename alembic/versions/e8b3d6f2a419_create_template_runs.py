"""create template_runs

Revision ID: e8b3d6f2a419
Revises: d5f2b7a1c308
Create Date: 2026-10-19 13:00:00.000000

Новая таблица запусков шаблонов (фоновые задачи). Старый код её не использует —
применять можно до выкладки нового кода.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "e8b3d6f2a419"
down_revision = "d5f2b7a1c308"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("user_role", sa.String(32), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "progress", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "input_files", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("searches_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_template_runs_user_status", "template_runs", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_template_runs_user_status", table_name="template_runs")
    op.drop_table("template_runs")
