"""M41 Telegram bot: link tokens and the linked chat of a user.

Revision ID: 20261009_m41_telegram
Revises: 20261009_m17_memory
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m41_telegram"
down_revision: str | None = "20261009_m17_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "telegram_link_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_telegram_link_tokens_hash"),
    )
    op.create_index("ix_telegram_link_tokens_user", "telegram_link_tokens", ["user_id"])
    op.create_table(
        "telegram_links",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_telegram_links_user"),
        sa.UniqueConstraint("chat_id", name="uq_telegram_links_chat"),
    )


def downgrade() -> None:
    op.drop_table("telegram_links")
    op.drop_index("ix_telegram_link_tokens_user", table_name="telegram_link_tokens")
    op.drop_table("telegram_link_tokens")
