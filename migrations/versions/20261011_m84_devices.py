"""M84: the «Устройства» screen — user-chosen names, presence and the phone's status report.

``devices``: ``display_name`` (rename), ``status_report`` + ``status_reported_at`` (the app's
heartbeat: OS, model, OS permission per capability — names and states only).
``web_sessions``: ``display_name``, ``client_label`` («браузер Chrome на macOS», derived from the
User-Agent once) and ``last_seen_at``. Only columns on tables that already cascade from
``users``; no new table.

Revision ID: 20261011_m84_devices
Revises: 20261011_m17r1_account_deletions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_m84_devices"
down_revision: str | None = "20261011_m17r1_account_deletions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("display_name", sa.String(128), nullable=True))
    op.add_column("devices", sa.Column("status_report", postgresql.JSONB(), nullable=True))
    op.add_column(
        "devices",
        sa.Column("status_reported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("web_sessions", sa.Column("display_name", sa.String(128), nullable=True))
    op.add_column("web_sessions", sa.Column("client_label", sa.String(128), nullable=True))
    op.add_column(
        "web_sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("web_sessions", "last_seen_at")
    op.drop_column("web_sessions", "client_label")
    op.drop_column("web_sessions", "display_name")
    op.drop_column("devices", "status_reported_at")
    op.drop_column("devices", "status_report")
    op.drop_column("devices", "display_name")
