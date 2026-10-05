"""add attachments to chat_messages

Revision ID: b7c2d4e9f015
Revises: a3f81c9e2b47
Create Date: 2026-10-05 00:00:00.000000

Вложения сообщений (файлы/изображения): JSONB-массив записей
{"type", "name", "key", "mime", "size"}; хранится только ключ объекта в R2.
Миграция аддитивная — старый код её не замечает, поэтому применять можно до
выкладки нового кода.

Также чинит порядок списка чатов: chat_sessions.updated_at раньше не обновлялся
при добавлении сообщений, поэтому у существующих сессий он равен времени
создания. Бэкфилл ставит его в время последнего сообщения. Downgrade
бэкфилл не откатывает (данные до него не сохраняются).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c2d4e9f015"
down_revision = "a3f81c9e2b47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE chat_sessions AS s
        SET updated_at = m.last_message_at
        FROM (
            SELECT session_id, max(created_at) AS last_message_at
            FROM chat_messages
            GROUP BY session_id
        ) AS m
        WHERE m.session_id = s.id AND m.last_message_at > s.updated_at
        """
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "attachments")
