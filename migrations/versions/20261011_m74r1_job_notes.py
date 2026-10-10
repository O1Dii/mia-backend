"""M74-R1: ``notify`` called inside a background job becomes part of the job's single result.

``jobs.notes`` holds what the job's model passed to ``notify`` while the job runs
(``[{"text": ..., "title": ...}]``); the worker folds it into the result when the job
finishes, so a job gives exactly one result card, inbox item and notification.

Revision ID: 20261011_m74r1_job_notes
Revises: 20261010_m77_notify_channels
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_m74r1_job_notes"
down_revision: str | None = "20261010_m77_notify_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("notes", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "notes")
