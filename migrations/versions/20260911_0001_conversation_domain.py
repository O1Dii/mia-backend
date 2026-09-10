"""Create canonical user and conversation persistence.

Revision ID: 20260911_0001
Revises: None
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260911_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

turn_source = postgresql.ENUM(
    "android_text", "android_voice", name="turn_source", create_type=False
)
turn_status = postgresql.ENUM(
    "pending",
    "active",
    "completed",
    "interrupted",
    "failed",
    name="turn_status",
    create_type=False,
)
entry_role = postgresql.ENUM(
    "user", "assistant", "tool", "system", name="entry_role", create_type=False
)
entry_kind = postgresql.ENUM(
    "text",
    "transcript",
    "tool_call",
    "tool_result",
    "artifact_ref",
    name="entry_kind",
    create_type=False,
)
entry_modality = postgresql.ENUM("text", "audio", name="entry_modality", create_type=False)
entry_delivery_status = postgresql.ENUM(
    "pending",
    "streaming",
    "delivered",
    "interrupted",
    "failed",
    name="entry_delivery_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (
        turn_source,
        turn_status,
        entry_role,
        entry_kind,
        entry_modality,
        entry_delivery_status,
    ):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("google_sub", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=True),
        sa.Column("avatar_url", sa.String(2048), nullable=True),
        sa.Column("locale", sa.String(35), server_default="en", nullable=False),
        sa.Column("timezone", sa.String(64), server_default="UTC", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_sub", name="uq_users_google_sub"),
    )
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("next_sequence", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("next_sequence >= 1", name="ck_conversations_next_sequence_positive"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_user_updated", "conversations", ["user_id", "updated_at", "id"]
    )
    op.create_table(
        "turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", turn_source, nullable=False),
        sa.Column("status", turn_status, server_default="pending", nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "latency_metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "conversation_id", name="uq_turns_id_conversation"),
        sa.UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_turns_conversation_idempotency"
        ),
    )
    op.create_index("ix_turns_conversation_started", "turns", ["conversation_id", "started_at"])
    op.create_table(
        "entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", entry_role, nullable=False),
        sa.Column("kind", entry_kind, nullable=False),
        sa.Column("modality", entry_modality, nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column(
            "delivery_status", entry_delivery_status, server_default="pending", nullable=False
        ),
        sa.Column(
            "provider_metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["turns.id", "turns.conversation_id"],
            name="fk_entries_turn_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_entries_conversation_sequence"),
    )
    op.create_index("ix_entries_conversation_sequence", "entries", ["conversation_id", "sequence"])
    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("resource_kind", sa.String(32), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "endpoint", "idempotency_key", name="uq_idempotency_user_endpoint_key"
        ),
    )
    op.create_index("ix_idempotency_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("entries")
    op.drop_table("turns")
    op.drop_table("conversations")
    op.drop_table("users")
    bind = op.get_bind()
    for enum_type in (
        entry_delivery_status,
        entry_modality,
        entry_kind,
        entry_role,
        turn_status,
        turn_source,
    ):
        enum_type.drop(bind, checkfirst=True)
