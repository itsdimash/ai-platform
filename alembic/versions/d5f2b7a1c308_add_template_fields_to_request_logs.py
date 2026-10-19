"""add template_id, run_id and searches_used to ai_request_logs

Revision ID: d5f2b7a1c308
Revises: c4e8a1f3b920
Create Date: 2026-10-19 12:00:00.000000

ai_request_logs:
  template_id    — шаблон, породивший запрос (NULL для обычного чата)
  run_id         — id запуска шаблона (строка с uuid)
  searches_used  — число выполненных веб-поисков

Все колонки nullable и аддитивные — старый код миграцию не замечает,
применять можно до выкладки нового кода.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d5f2b7a1c308"
down_revision = "c4e8a1f3b920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_request_logs", sa.Column("template_id", sa.String(64), nullable=True))
    op.add_column("ai_request_logs", sa.Column("run_id", sa.String(36), nullable=True))
    op.add_column("ai_request_logs", sa.Column("searches_used", sa.Integer(), nullable=True))
    op.create_index("ix_ai_request_logs_run_id", "ai_request_logs", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_request_logs_run_id", table_name="ai_request_logs")
    op.drop_column("ai_request_logs", "searches_used")
    op.drop_column("ai_request_logs", "run_id")
    op.drop_column("ai_request_logs", "template_id")
