"""M17-R1: journal of account deletions (``DELETE /v1/account``).

One row per deleted account: when, through which client, how many plugin accounts were
revoked and whether the data key was shredded. No content and no identity: ``user_id`` has
no foreign key (the ``users`` row is deleted with everything that cascades from it).

Revision ID: 20261011_m17r1_account_deletions
Revises: 20261011_m74r1_job_notes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_m17r1_account_deletions"
down_revision: str | None = "20261011_m74r1_job_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_deletions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("requested_via", sa.String(16), nullable=False),
        sa.Column("provider_connections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("memory_shredded", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_account_deletions_user", "account_deletions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_account_deletions_user", table_name="account_deletions")
    op.drop_table("account_deletions")
