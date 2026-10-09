"""M16 jobs: queue + attempts, schedules, fired deadlines, inbox, push tokens, deliveries.

Revision ID: 20261009_m16_jobs
Revises: 20261009_m15_store
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m16_jobs"
down_revision: str | None = "20261009_m15_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("origin_type", sa.String(16), nullable=False),
        sa.Column("origin_id", sa.String(256), nullable=False),
        sa.Column("run_id", UUID, nullable=True),
        sa.Column("budget", postgresql.JSONB(), nullable=False),
        sa.Column("delivery", postgresql.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", TS, nullable=False),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", TS, nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'failed', "
            "'cancelled')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("kind IN ('run', 'reminder')", name="ck_jobs_kind"),
        sa.CheckConstraint(
            "origin_type IN ('run', 'schedule', 'app', 'user')", name="ck_jobs_origin_type"
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_jobs_attempt_positive"),
        sa.UniqueConstraint("dedupe_key", name="uq_jobs_dedupe_key"),
    )
    op.create_index("ix_jobs_user_created", "jobs", ["user_id", "created_at"])
    op.create_index(
        "ix_jobs_queued", "jobs", ["run_after"], postgresql_where=sa.text("status = 'queued'")
    )
    op.create_index(
        "ix_jobs_leased",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status IN ('running', 'waiting_approval')"),
    )
    op.create_index("ix_jobs_run_id", "jobs", ["run_id"])

    op.create_table(
        "job_attempts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("job_id", UUID, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("run_id", UUID, nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "attempt", name="uq_job_attempts_job_attempt"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded', 'failed', 'cancelled', 'retry', "
            "'interrupted', 'lease_lost')",
            name="ck_job_attempts_outcome",
        ),
    )

    op.create_table(
        "schedules",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("rule", sa.String(1000), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("dtstart", TS, nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("delivery", postgresql.JSONB(), nullable=False),
        sa.Column("misfire_policy", sa.String(24), nullable=False),
        sa.Column("catch_up_limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("next_run_at", TS, nullable=True),
        sa.Column("last_run_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('active', 'paused')", name="ck_schedules_status"),
        sa.CheckConstraint("kind IN ('run', 'reminder')", name="ck_schedules_kind"),
        sa.CheckConstraint(
            "misfire_policy IN ('skip', 'run_once', 'limited_catch_up')",
            name="ck_schedules_misfire_policy",
        ),
        sa.CheckConstraint(
            "catch_up_limit BETWEEN 1 AND 24", name="ck_schedules_catch_up_limit_range"
        ),
    )
    op.create_index("ix_schedules_user_created", "schedules", ["user_id", "created_at"])
    op.create_index(
        "ix_schedules_due",
        "schedules",
        ["next_run_at"],
        postgresql_where=sa.text("status = 'active' AND next_run_at IS NOT NULL"),
    )

    op.create_table(
        "deadline_fires",
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("deadline_ms", sa.BigInteger(), nullable=False),
        sa.Column("fired_at", TS, nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "plugin_id", "item_id", "deadline_ms", name="pk_deadline_fires"
        ),
    )
    op.create_index("ix_deadline_fires_fired_at", "deadline_fires", ["fired_at"])

    op.create_table(
        "inbox_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(256), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("read_at", TS, nullable=True),
        sa.Column("announced_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "source_type IN ('job', 'schedule', 'plugin', 'system')",
            name="ck_inbox_items_source_type",
        ),
    )
    op.create_index("ix_inbox_items_user_created", "inbox_items", ["user_id", "created_at"])
    op.create_index(
        "ix_inbox_items_unannounced",
        "inbox_items",
        ["user_id", "created_at"],
        postgresql_where=sa.text("announced_at IS NULL AND read_at IS NULL"),
    )

    op.create_table(
        "push_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("device_id", UUID, nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("invalidated_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.CheckConstraint("provider IN ('fcm')", name="ck_push_tokens_provider"),
        sa.UniqueConstraint("device_id", "provider", name="uq_push_tokens_device_provider"),
    )
    op.create_index("ix_push_tokens_user", "push_tokens", ["user_id"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("inbox_item_id", UUID, nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", TS, nullable=False),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inbox_item_id"], ["inbox_items.id"], ondelete="CASCADE"),
        sa.CheckConstraint("channel IN ('push', 'telegram')", name="ck_deliveries_channel"),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'skipped', 'failed')", name="ck_deliveries_status"
        ),
        sa.UniqueConstraint("inbox_item_id", "channel", name="uq_deliveries_item_channel"),
    )
    op.create_index(
        "ix_deliveries_pending",
        "notification_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("push_tokens")
    op.drop_table("inbox_items")
    op.drop_table("deadline_fires")
    op.drop_table("schedules")
    op.drop_table("job_attempts")
    op.drop_table("jobs")
