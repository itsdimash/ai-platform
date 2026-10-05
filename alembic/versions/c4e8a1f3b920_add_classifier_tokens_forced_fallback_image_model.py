"""add classifier tokens, forced_fallback and image_model

Revision ID: c4e8a1f3b920
Revises: b7c2d4e9f015
Create Date: 2026-10-05 12:00:00.000000

ai_request_logs:
  classifier_tokens_in / classifier_tokens_out  — токены вызова классификатора
  forced_fallback                               — файловый запрос потребовал второй попытки
  image_model                                   — реальный id модели картинок
chat_messages:
  image_model                                   — то же, на уровне сообщения (для истории)

Все колонки аддитивные (NOT NULL с DEFAULT либо NULL) — старый код миграцию не замечает,
применять можно до выкладки нового кода.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c4e8a1f3b920"
down_revision = "b7c2d4e9f015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_request_logs",
        sa.Column(
            "classifier_tokens_in", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "ai_request_logs",
        sa.Column(
            "classifier_tokens_out", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "ai_request_logs",
        sa.Column("forced_fallback", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("ai_request_logs", sa.Column("image_model", sa.Text(), nullable=True))
    op.add_column("chat_messages", sa.Column("image_model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "image_model")
    op.drop_column("ai_request_logs", "image_model")
    op.drop_column("ai_request_logs", "forced_fallback")
    op.drop_column("ai_request_logs", "classifier_tokens_out")
    op.drop_column("ai_request_logs", "classifier_tokens_in")
