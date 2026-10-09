"""Add web cookie sessions and the workspace domain (M10).

Revision ID: 20261009_m10_0001
Revises: 20260913_0003
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m10_0001"
down_revision: str | None = "20260913_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TILE_SIZES = "('1x1', '2x1', '2x2', '4x2', '4x4')"


def _timestamps(*, updated: bool = True) -> list[sa.Column[object]]:
    columns: list[sa.Column[object]] = [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        )
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )
    return columns


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        *_timestamps(updated=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_web_sessions_user_expires", "web_sessions", ["user_id", "expires_at"])

    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), server_default="Personal", nullable=False),
        sa.Column("layout_version", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.CheckConstraint("layout_version >= 0", name="ck_workspaces_layout_version_nonnegative"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", name="uq_workspaces_owner_user_id"),
    )

    op.create_table(
        "installations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "grants", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("tools_digest", sa.String(128), nullable=True),
        sa.Column("custom_url", sa.String(2048), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=True),
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'needs_connection', 'needs_consent', 'disabled')",
            name="ck_installations_status",
        ),
        sa.CheckConstraint(
            "source IN ('catalog', 'custom', 'legacy_web')", name="ck_installations_source"
        ),
        sa.CheckConstraint("state_version >= 0", name="ck_installations_state_version_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "plugin_id", name="uq_installations_workspace_plugin"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_installations_id_workspace"),
    )

    op.create_table(
        "plugin_states",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("state_version >= 0", name="ck_plugin_states_state_version_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "plugin_id"),
    )

    op.create_table(
        "widget_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tile_id", sa.String(64), nullable=False),
        sa.Column("desktop_size", sa.String(8), nullable=False),
        sa.Column("desktop_position", sa.Integer(), nullable=False),
        sa.Column("mobile_size", sa.String(8), nullable=False),
        sa.Column("mobile_position", sa.Integer(), nullable=False),
        sa.Column(
            "settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            f"desktop_size IN {TILE_SIZES}", name="ck_widget_instances_desktop_size"
        ),
        sa.CheckConstraint(f"mobile_size IN {TILE_SIZES}", name="ck_widget_instances_mobile_size"),
        sa.CheckConstraint(
            "desktop_position >= 0 AND mobile_position >= 0",
            name="ck_widget_instances_position_nonnegative",
        ),
        sa.CheckConstraint(
            "state_version >= 0", name="ck_widget_instances_state_version_nonnegative"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["installation_id", "workspace_id"],
            ["installations.id", "installations.workspace_id"],
            name="fk_widget_instances_installation_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_widget_instances_workspace_id", "widget_instances", ["workspace_id"])

    op.create_table(
        "plugin_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.String(2000), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_plugin_reviews_rating"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "user_id", name="uq_plugin_reviews_plugin_user"),
    )
    op.create_index(
        "ix_plugin_reviews_plugin_updated", "plugin_reviews", ["plugin_id", "updated_at"]
    )

    op.create_table(
        "workspace_mutations",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        *_timestamps(updated=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "idempotency_key"),
    )

    op.create_table(
        "legacy_web_imports",
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("legacy_user_id", sa.String(64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_digest", sa.String(64), nullable=False),
        sa.Column(
            "counts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id", "legacy_user_id"),
    )


def downgrade() -> None:
    op.drop_table("legacy_web_imports")
    op.drop_table("workspace_mutations")
    op.drop_table("plugin_reviews")
    op.drop_table("widget_instances")
    op.drop_table("plugin_states")
    op.drop_table("installations")
    op.drop_table("workspaces")
    op.drop_table("web_sessions")
