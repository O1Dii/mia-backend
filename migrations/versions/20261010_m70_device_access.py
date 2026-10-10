"""M70 device access (ADR-0016): run origins, persisted device tool declarations, consent
grants, per-device capability settings, the device access log, approvals decided only on the
target device and the WebView web session bound to its device.

Revision ID: 20261010_m70_device_access
Revises: 20261010_m74_chats
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261010_m70_device_access"
down_revision: str | None = "20261010_m74_chats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("tool_approvals", sa.Column("device_consent", JSONB, nullable=True))
    op.add_column("web_sessions", sa.Column("device_id", UUID, nullable=True))
    op.create_foreign_key(
        "web_sessions_device_id_fkey",
        "web_sessions",
        "devices",
        ["device_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_table(
        "device_run_origins",
        sa.Column("run_id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("device_id", UUID, nullable=True),
        sa.Column("web_session_id", UUID, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["web_session_id"], ["web_sessions.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "kind IN ('device_app', 'device_webview', 'web', 'unknown')",
            name="ck_device_run_origins_kind",
        ),
    )
    op.create_table(
        "device_tool_declarations",
        sa.Column("device_id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("tools", JSONB, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "device_grants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("target_device_id", UUID, nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("run_id", UUID, nullable=True),
        sa.Column("requester_kind", sa.String(24), nullable=False),
        sa.Column("requester_label", sa.String(128), nullable=False),
        sa.Column("requester_key", sa.String(80), nullable=True),
        sa.Column("requester_device_id", UUID, nullable=True),
        sa.Column("requester_web_session_id", UUID, nullable=True),
        sa.Column("approval_id", UUID, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requester_device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requester_web_session_id"], ["web_sessions.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("scope IN ('run', 'persistent')", name="ck_device_grants_scope"),
        sa.CheckConstraint(
            "(scope = 'run') = (run_id IS NOT NULL)", name="ck_device_grants_run_scope"
        ),
    )
    op.create_index(
        "ix_device_grants_lookup",
        "device_grants",
        ["user_id", "target_device_id", "capability"],
    )
    op.create_table(
        "device_capability_settings",
        sa.Column("device_id", UUID, primary_key=True),
        sa.Column("capability", sa.String(64), primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("remote", sa.String(8), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "remote IN ('ask', 'deny')", name="ck_device_capability_settings_remote"
        ),
    )
    op.create_table(
        "device_access_log",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("device_id", UUID, nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(128), nullable=False),
        sa.Column("requester_kind", sa.String(24), nullable=False),
        sa.Column("requester_label", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("run_id", UUID, nullable=True),
        sa.Column("approval_id", UUID, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_device_access_log_device",
        "device_access_log",
        ["user_id", "device_id", "created_at"],
    )
    op.create_index("ix_device_access_log_approval", "device_access_log", ["approval_id"])


def downgrade() -> None:
    op.drop_index("ix_device_access_log_approval", table_name="device_access_log")
    op.drop_index("ix_device_access_log_device", table_name="device_access_log")
    op.drop_table("device_access_log")
    op.drop_table("device_capability_settings")
    op.drop_index("ix_device_grants_lookup", table_name="device_grants")
    op.drop_table("device_grants")
    op.drop_table("device_tool_declarations")
    op.drop_table("device_run_origins")
    op.drop_constraint("web_sessions_device_id_fkey", "web_sessions", type_="foreignkey")
    op.drop_column("web_sessions", "device_id")
    op.drop_column("tool_approvals", "device_consent")
