"""M17 Memory Ledger: facts, people, preferences, privacy settings, access log.

Revision ID: 20261009_m17_memory
Revises: 20261009_m16_jobs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_m17_memory"
down_revision: str | None = "20261009_m16_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = "status IN ('suggested', 'active', 'rejected')"
SENSITIVITY = "sensitivity IN ('normal', 'sensitive')"
CATEGORY = "category IN ('profile', 'work', 'home', 'health', 'finance', 'interests', 'other')"
SOURCE = (
    "source_type IN ('user_said', 'turn', 'plugin', 'import', 'app', 'device_contacts', 'telegram')"
)
NOW = sa.text("now()")


def _user_fk() -> sa.Column[object]:
    return sa.Column(
        "user_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )


def _stamp(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=NOW)


def upgrade() -> None:
    op.create_table(
        "memory_people",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _user_fk(),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sensitivity", sa.String(16), nullable=False),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(256)),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(STATUS, name="ck_memory_people_status"),
        sa.CheckConstraint(SENSITIVITY, name="ck_memory_people_sensitivity"),
        sa.CheckConstraint(SOURCE, name="ck_memory_people_source"),
    )
    op.create_index("ix_memory_people_user_status", "memory_people", ["user_id", "status"])

    op.create_table(
        "memory_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _user_fk(),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sensitivity", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="user"),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(256)),
        sa.Column("text_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("person_draft_ciphertext", sa.LargeBinary()),
        sa.Column(
            "person_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_people.id", ondelete="SET NULL"),
        ),
        sa.Column("search_index", postgresql.TSVECTOR(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(STATUS, name="ck_memory_facts_status"),
        sa.CheckConstraint(SENSITIVITY, name="ck_memory_facts_sensitivity"),
        sa.CheckConstraint(CATEGORY, name="ck_memory_facts_category"),
        sa.CheckConstraint(SOURCE, name="ck_memory_facts_source"),
        sa.CheckConstraint("scope IN ('user', 'automation')", name="ck_memory_facts_scope"),
    )
    op.create_index(
        "ix_memory_facts_user_status", "memory_facts", ["user_id", "status", "created_at"]
    )
    op.create_index(
        "ix_memory_facts_search", "memory_facts", ["search_index"], postgresql_using="gin"
    )

    op.create_table(
        "memory_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _user_fk(),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(256)),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.CheckConstraint(SOURCE, name="ck_memory_preferences_source"),
    )
    op.create_index(
        "uq_memory_preferences_user_key", "memory_preferences", ["user_id", "key"], unique=True
    )

    op.create_table(
        "memory_settings",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "autosave_categories",
            postgresql.ARRAY(sa.String(32)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("history_retention", sa.String(16), nullable=False, server_default="forever"),
        _stamp("updated_at"),
        sa.CheckConstraint(
            "history_retention IN ('30d', '1y', 'forever', 'none')",
            name="ck_memory_settings_retention",
        ),
    )

    op.create_table(
        "memory_access_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _user_fk(),
        _stamp("at"),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("app_client_id", sa.String(512)),
    )
    op.create_index("ix_memory_access_log_user_at", "memory_access_log", ["user_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_memory_access_log_user_at", table_name="memory_access_log")
    op.drop_table("memory_access_log")
    op.drop_table("memory_settings")
    op.drop_index("uq_memory_preferences_user_key", table_name="memory_preferences")
    op.drop_table("memory_preferences")
    op.drop_index("ix_memory_facts_search", table_name="memory_facts")
    op.drop_index("ix_memory_facts_user_status", table_name="memory_facts")
    op.drop_table("memory_facts")
    op.drop_index("ix_memory_people_user_status", table_name="memory_people")
    op.drop_table("memory_people")
