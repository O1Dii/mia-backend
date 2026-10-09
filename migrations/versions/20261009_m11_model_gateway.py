"""Model gateway: user data keys, model connections, gateway settings.

Revision ID: 20261009_m11_models
Revises: 20260913_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m11_models"
down_revision: str | None = "20260913_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_data_keys",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("master_key_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("rewrapped_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "model_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=True),
        sa.Column("capability", sa.String(16), server_default="tools", nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("key_last4", sa.String(4), nullable=True),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_reason", sa.String(64), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("capability IN ('tools', 'chat_only')", name="ck_model_connections_cap"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'needs_reauth')", name="ck_model_connections_status"
        ),
    )
    op.create_index("ix_model_connections_user", "model_connections", ["user_id", "created_at"])
    op.create_index(
        "uq_model_connections_primary",
        "model_connections",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_index(
        "uq_model_connections_chatgpt_plan",
        "model_connections",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("provider = 'chatgpt-plan'"),
    )
    op.create_table(
        "model_gateway_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("model_gateway_settings")
    op.drop_table("model_connections")
    op.drop_table("user_data_keys")
