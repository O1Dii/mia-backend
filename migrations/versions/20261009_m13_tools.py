"""M13 tool router: run-scoped audit columns and approvals.

Revision ID: 20261009_m13_tools
Revises: 20261009_m12_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m13_tools"
down_revision: str | None = "20261009_m12_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_STATUSES = ("uncertain", "denied", "expired", "approval_required")
LEGACY_STATUSES = ("running", "succeeded", "failed", "cancelled")
ROUTER_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("run_id", postgresql.UUID(as_uuid=True)),
    ("operation_id", postgresql.UUID(as_uuid=True)),
    ("tool_call_id", sa.String(128)),
    ("workspace_id", sa.String(64)),
    ("profile", sa.String(16)),
    ("executor", sa.String(16)),
    ("risk", sa.String(24)),
    ("plugin_id", sa.String(128)),
    ("installation_id", sa.String(64)),
    ("approval_id", postgresql.UUID(as_uuid=True)),
    ("result_digest", sa.String(80)),
    ("duration_ms", sa.Integer()),
)


def upgrade() -> None:
    for value in NEW_STATUSES:
        op.execute(f"ALTER TYPE tool_execution_status ADD VALUE IF NOT EXISTS '{value}'")
    op.alter_column("tool_executions", "turn_id", nullable=True)
    op.alter_column("tool_executions", "response_id", nullable=True)
    for name, column_type in ROUTER_COLUMNS:
        op.add_column("tool_executions", sa.Column(name, column_type, nullable=True))
    op.create_unique_constraint(
        "uq_tool_executions_operation_id", "tool_executions", ["operation_id"]
    )
    op.create_index("ix_tool_executions_run", "tool_executions", ["run_id", "created_at"])

    op.create_table(
        "tool_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("tool", sa.String(200), nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=True),
        sa.Column("risk", sa.String(24), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("args_hash", sa.String(71), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(8), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channel", sa.String(16), nullable=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')",
            name="ck_tool_approvals_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'deny')",
            name="ck_tool_approvals_decision",
        ),
    )
    op.create_index(
        "ix_tool_approvals_user_status", "tool_approvals", ["user_id", "status", "created_at"]
    )
    op.create_index("ix_tool_approvals_run", "tool_approvals", ["run_id"])


def downgrade() -> None:
    op.drop_table("tool_approvals")
    # Router rows have no turn: they cannot exist in the legacy schema.
    op.execute("DELETE FROM tool_executions WHERE turn_id IS NULL OR response_id IS NULL")
    op.drop_index("ix_tool_executions_run", table_name="tool_executions")
    op.drop_constraint("uq_tool_executions_operation_id", "tool_executions", type_="unique")
    for name, _ in reversed(ROUTER_COLUMNS):
        op.drop_column("tool_executions", name)
    op.alter_column("tool_executions", "response_id", nullable=False)
    op.alter_column("tool_executions", "turn_id", nullable=False)
    # Postgres cannot drop enum values: rebuild the type without the router statuses.
    new = ", ".join(f"'{value}'" for value in NEW_STATUSES)
    legacy = ", ".join(f"'{value}'" for value in LEGACY_STATUSES)
    op.execute(f"DELETE FROM tool_executions WHERE status::text IN ({new})")
    op.execute("ALTER TYPE tool_execution_status RENAME TO tool_execution_status_m13_old")
    op.execute(f"CREATE TYPE tool_execution_status AS ENUM ({legacy})")
    op.execute(
        "ALTER TABLE tool_executions ALTER COLUMN status TYPE tool_execution_status "
        "USING status::text::tool_execution_status"
    )
    op.execute("DROP TYPE tool_execution_status_m13_old")
