"""M15 store: published plugin releases and plugin account connections.

Revision ID: 20261009_m15_store
Revises: 20261009_m13_tools
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m15_store"
down_revision: str | None = "20261009_m13_tools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_ARRAY = sa.text("'[]'::jsonb")
_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "plugin_releases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("version_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="published"),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("description", sa.String(240), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("author", sa.String(60), nullable=True),
        sa.Column("server_kind", sa.String(16), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("tools", postgresql.JSONB(), nullable=False),
        sa.Column("tools_digest", sa.String(80), nullable=False),
        sa.Column("grants", postgresql.JSONB(), nullable=False, server_default=_EMPTY_ARRAY),
        sa.Column("store_meta", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("files", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("package_digest", sa.String(80), nullable=False),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("plugin_id", "version", name="uq_plugin_releases_plugin_version"),
        sa.CheckConstraint("status IN ('published', 'yanked')", name="ck_plugin_releases_status"),
    )
    op.create_index(
        "ix_plugin_releases_plugin_version_key", "plugin_releases", ["plugin_id", "version_key"]
    )
    op.create_index("ix_plugin_releases_category", "plugin_releases", ["category"])

    op.create_table(
        "plugin_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resource", sa.String(2048), nullable=True),
        sa.Column("issuer", sa.String(2048), nullable=True),
        sa.Column("authorization_endpoint", sa.String(2048), nullable=True),
        sa.Column("token_endpoint", sa.String(2048), nullable=True),
        sa.Column("revocation_endpoint", sa.String(2048), nullable=True),
        sa.Column("client_id", sa.String(512), nullable=True),
        sa.Column("auth_method", sa.String(32), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("account_label", sa.String(200), nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flow_state_hash", sa.String(64), nullable=True),
        sa.Column("flow_verifier_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("flow_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flow_return_to", sa.String(300), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["installation_id", "workspace_id"],
            ["installations.id", "installations.workspace_id"],
            name="fk_plugin_connections_installation_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("installation_id", name="uq_plugin_connections_installation"),
        sa.CheckConstraint(
            "status IN ('pending', 'connected', 'expired', 'revoked')",
            name="ck_plugin_connections_status",
        ),
    )
    op.create_index("ix_plugin_connections_user", "plugin_connections", ["user_id"])
    op.create_index(
        "uq_plugin_connections_flow_state",
        "plugin_connections",
        ["flow_state_hash"],
        unique=True,
        postgresql_where=sa.text("flow_state_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("plugin_connections")
    op.drop_table("plugin_releases")
