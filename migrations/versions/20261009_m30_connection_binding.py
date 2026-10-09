"""M30: bind a web OAuth connect flow to the browser that started it (OI-M15-003).

Revision ID: 20261009_m30_connection_binding
Revises: 20261009_m17_memory
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_m30_connection_binding"
down_revision: str | None = "20261009_m17_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plugin_connections", sa.Column("flow_binding_hash", sa.String(64)))


def downgrade() -> None:
    op.drop_column("plugin_connections", "flow_binding_hash")
