"""M50 «Войти через Mia»: grants, authorization requests, codes, tokens, app spend.

Revision ID: 20261009_m50_oauth
Revises: 20261009_m41_telegram
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m50_oauth"
down_revision: str | None = "20261009_m41_telegram"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID = postgresql.UUID(as_uuid=True)


def _user(nullable: bool = False) -> sa.Column[object]:
    return sa.Column(
        "user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=nullable
    )


def _grant() -> sa.Column[object]:
    return sa.Column(
        "grant_id", UUID, sa.ForeignKey("oauth_grants.id", ondelete="CASCADE"), nullable=False
    )


def _at(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=NOW)


def upgrade() -> None:
    op.create_table(
        "oauth_grants",
        sa.Column("id", UUID, primary_key=True),
        _user(),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("client_uri", sa.String(2048)),
        sa.Column("logo_uri", sa.String(2048)),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("memory_categories", sa.Text(), nullable=False),
        sa.Column("monthly_limit_usd", sa.Numeric(10, 2), nullable=False),
        _at("created_at"),
        _at("updated_at"),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("monthly_limit_usd >= 0", name="ck_oauth_grants_limit"),
    )
    op.create_index(
        "uq_oauth_grants_active",
        "oauth_grants",
        ["user_id", "client_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "oauth_authorization_requests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("handle_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("client_uri", sa.String(2048)),
        sa.Column("logo_uri", sa.String(2048)),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("state", sa.String(1024), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("resource", sa.String(2048)),
        sa.Column("nonce", sa.String(512)),
        _user(nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decision_key_hash", sa.String(64)),
        sa.Column("redirect_to", sa.Text()),
        _at("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied')", name="ck_oauth_requests_status"
        ),
    )
    op.create_index("ix_oauth_requests_expires", "oauth_authorization_requests", ["expires_at"])
    op.create_table(
        "oauth_codes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        _grant(),
        sa.Column("request_id", UUID),
        _user(),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("resource", sa.String(2048)),
        sa.Column("nonce", sa.String(512)),
        sa.Column("auth_time", sa.DateTime(timezone=True), nullable=False),
        _at("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "oauth_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(8), nullable=False),
        _grant(),
        sa.Column("family_id", UUID, nullable=False),
        sa.Column("code_id", UUID),
        _user(),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("resource", sa.String(2048)),
        _at("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_oauth_tokens_kind"),
    )
    op.create_index("ix_oauth_tokens_grant", "oauth_tokens", ["grant_id"])
    op.create_index("ix_oauth_tokens_family", "oauth_tokens", ["family_id"])
    op.create_table(
        "oauth_app_usage",
        sa.Column("id", UUID, primary_key=True),
        _grant(),
        _user(),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("run_id", UUID),
        sa.Column("surface", sa.String(32), nullable=False),
        _at("at"),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("estimated", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "surface IN ('chat_completions', 'responses', 'mcp')", name="ck_oauth_usage_surface"
        ),
    )
    op.create_index("ix_oauth_app_usage_grant_at", "oauth_app_usage", ["grant_id", "at"])


def downgrade() -> None:
    op.drop_table("oauth_app_usage")
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_authorization_requests")
    op.drop_index("uq_oauth_grants_active", table_name="oauth_grants")
    op.drop_table("oauth_grants")
