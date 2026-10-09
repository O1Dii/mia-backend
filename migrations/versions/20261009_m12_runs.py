"""M12 run engine: runs, run_events, the primary user feed.

Revision ID: 20261009_m12_runs
Revises: 20261009_m11_models
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m12_runs"
down_revision: str | None = "20261009_m11_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_TURN_SOURCES = ("run_voice", "run_chat", "run_job", "run_app")
LEGACY_TURN_SOURCES = ("android_text", "android_voice")
TERMINAL = "status NOT IN ('completed', 'failed', 'cancelled')"


def upgrade() -> None:
    for value in RUN_TURN_SOURCES:
        op.execute(f"ALTER TYPE turn_source ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column("conversations", sa.Column("summary_until_sequence", sa.BigInteger()))
    op.add_column(
        "conversations",
        sa.Column("is_primary_feed", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index(
        "uq_conversations_primary_feed",
        "conversations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary_feed"),
    )

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.String(64)),
        sa.Column("profile", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("model", sa.String(200)),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("device_id", postgresql.UUID(as_uuid=True)),
        sa.Column("app_client_id", sa.String(512)),
        sa.Column(
            "scopes", postgresql.ARRAY(sa.String(200)), nullable=False, server_default="{}"
        ),
        sa.Column("incognito", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("input_text", sa.Text()),
        sa.Column("response", postgresql.JSONB()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("cancel_reason", sa.String(32)),
        sa.Column("steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage", postgresql.JSONB()),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_approval', 'cancel_requested', "
            "'completed', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("profile IN ('voice', 'chat', 'job', 'app')", name="ck_runs_profile"),
        sa.CheckConstraint("next_sequence >= 1", name="ck_runs_next_sequence_positive"),
    )
    op.create_index("ix_runs_user_created", "runs", ["user_id", "created_at"])
    op.create_index(
        "ix_runs_active_heartbeat",
        "runs",
        ["heartbeat_at"],
        postgresql_where=sa.text(TERMINAL),
    )

    op.create_table(
        "run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_run_events_sequence_positive"),
    )


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("runs")
    op.drop_index("uq_conversations_primary_feed", table_name="conversations")
    op.drop_column("conversations", "is_primary_feed")
    op.drop_column("conversations", "summary_until_sequence")

    # Postgres cannot drop enum values: rebuild the type without the run sources.
    # Turns created by runs (and their entries) are removed with them.
    sources = ", ".join(f"'{value}'" for value in RUN_TURN_SOURCES)
    legacy = ", ".join(f"'{value}'" for value in LEGACY_TURN_SOURCES)
    op.execute(f"DELETE FROM turns WHERE source::text IN ({sources})")
    op.execute("ALTER TYPE turn_source RENAME TO turn_source_m12_old")
    op.execute(f"CREATE TYPE turn_source AS ENUM ({legacy})")
    op.execute(
        "ALTER TABLE turns ALTER COLUMN source TYPE turn_source USING source::text::turn_source"
    )
    op.execute("DROP TYPE turn_source_m12_old")
