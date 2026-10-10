"""M77 notification channels the user configures: per-channel switches, the «event type ×
channel» table, channel names open for plugin channels, and jobs/schedules created without
explicit channels follow the settings (``delivery.auto``).

Revision ID: 20261010_m77_notify_channels
Revises: 20261010_m70_device_access
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261010_m77_notify_channels"
down_revision: str | None = "20261010_m70_device_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
CHANNEL_NAME = "channel ~ '^[a-z][a-z0-9_.:-]{0,47}$' AND channel <> 'inbox'"


def upgrade() -> None:
    op.create_table(
        "notification_channel_settings",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("channel", sa.String(48), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("decided_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "channel"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(CHANNEL_NAME, name="ck_notification_channel_settings_channel"),
    )
    op.create_table(
        "notification_routes",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("channel", sa.String(48), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "event_type", "channel"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "event_type IN ('timer_alarm', 'job_result', 'approval', 'plugin_message')",
            name="ck_notification_routes_event_type",
        ),
        sa.CheckConstraint(CHANNEL_NAME, name="ck_notification_routes_channel"),
    )
    op.alter_column(
        "notification_deliveries",
        "channel",
        existing_type=sa.String(16),
        type_=sa.String(48),
        existing_nullable=False,
    )
    op.drop_constraint("ck_deliveries_channel", "notification_deliveries", type_="check")
    op.create_check_constraint("ck_deliveries_channel", "notification_deliveries", CHANNEL_NAME)
    # Jobs and schedules that carried the old code default (and nothing the user chose) now
    # follow the settings; an explicit list of any other shape stays as it was.
    for table in ("jobs", "schedules"):
        op.execute(
            f"UPDATE {table} SET delivery = delivery || '{{\"auto\": true}}'::jsonb "
            "WHERE delivery -> 'channels' = '[\"inbox\", \"push\"]'::jsonb"
        )


def downgrade() -> None:
    for table in ("jobs", "schedules"):
        op.execute(f"UPDATE {table} SET delivery = delivery - 'auto' WHERE delivery ? 'auto'")
    op.execute("DELETE FROM notification_deliveries WHERE channel NOT IN ('push', 'telegram')")
    op.drop_constraint("ck_deliveries_channel", "notification_deliveries", type_="check")
    op.alter_column(
        "notification_deliveries",
        "channel",
        existing_type=sa.String(48),
        type_=sa.String(16),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_deliveries_channel", "notification_deliveries", "channel IN ('push', 'telegram')"
    )
    op.drop_table("notification_routes")
    op.drop_table("notification_channel_settings")
