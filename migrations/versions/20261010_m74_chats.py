"""M74 chats (ADR-0017): chat kind, pin, activity, title source; per-user chat settings.

The existing primary feed of every user becomes their first chat («Основной чат»); its
activity timestamps are backfilled from the entries. Legacy conversations keep
``kind = NULL`` and are not listed as chats.

Revision ID: 20261010_m74_chats
Revises: 20261009_m50_oauth
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261010_m74_chats"
down_revision: str | None = "20261009_m50_oauth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
MAIN_CHAT_TITLE = "Основной чат"
OLD_FEED_TITLE = "Mia"


def upgrade() -> None:
    op.add_column("conversations", sa.Column("kind", sa.String(16), nullable=True))
    op.add_column("conversations", sa.Column("title_source", sa.String(8), nullable=True))
    op.add_column("conversations", sa.Column("pinned_at", TS, nullable=True))
    op.add_column("conversations", sa.Column("last_message_at", TS, nullable=True))
    op.add_column("conversations", sa.Column("last_assistant_at", TS, nullable=True))
    op.create_check_constraint(
        "ck_conversations_kind",
        "conversations",
        "kind IS NULL OR kind IN ('chat', 'telegram', 'background')",
    )
    op.create_check_constraint(
        "ck_conversations_title_source",
        "conversations",
        "title_source IS NULL OR title_source IN ('auto', 'model', 'user')",
    )
    op.create_index(
        "ix_conversations_user_kind_activity",
        "conversations",
        ["user_id", "kind", "last_message_at"],
    )
    op.create_index(
        "uq_conversations_telegram_chat",
        "conversations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'telegram'"),
    )
    op.create_index(
        "uq_conversations_background",
        "conversations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'background'"),
    )
    op.execute(
        """
        UPDATE conversations AS c
        SET kind = 'chat',
            last_message_at = (
                SELECT max(e.created_at) FROM entries AS e
                WHERE e.conversation_id = c.id AND e.role IN ('user', 'assistant')
            ),
            last_assistant_at = (
                SELECT max(e.created_at) FROM entries AS e
                WHERE e.conversation_id = c.id AND e.role = 'assistant'
            )
        WHERE c.is_primary_feed
        """
    )
    op.execute(
        sa.text(
            "UPDATE conversations SET title = :main, title_source = 'auto' "
            "WHERE is_primary_feed AND (title IS NULL OR title = :old)"
        ).bindparams(main=MAIN_CHAT_TITLE, old=OLD_FEED_TITLE)
    )
    op.create_table(
        "chat_settings",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "retention_days IS NULL OR retention_days IN (30, 60, 180)",
            name="ck_chat_settings_retention_days",
        ),
    )


def downgrade() -> None:
    op.drop_table("chat_settings")
    op.execute(
        sa.text(
            "UPDATE conversations SET title = :old WHERE is_primary_feed AND title = :main"
        ).bindparams(main=MAIN_CHAT_TITLE, old=OLD_FEED_TITLE)
    )
    op.drop_index("uq_conversations_background", table_name="conversations")
    op.drop_index("uq_conversations_telegram_chat", table_name="conversations")
    op.drop_index("ix_conversations_user_kind_activity", table_name="conversations")
    op.drop_constraint("ck_conversations_title_source", "conversations", type_="check")
    op.drop_constraint("ck_conversations_kind", "conversations", type_="check")
    op.drop_column("conversations", "last_assistant_at")
    op.drop_column("conversations", "last_message_at")
    op.drop_column("conversations", "pinned_at")
    op.drop_column("conversations", "title_source")
    op.drop_column("conversations", "kind")
