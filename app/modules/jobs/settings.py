"""Jobs, scheduler, worker and push settings (M16), read from the process environment.

Kept next to the module (not in ``app.core.config``) so parallel sessions do not edit
one shared settings class. Names are documented in ``.env.example``; values never
appear in logs.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    jobs_worker_concurrency: int = Field(default=4, ge=1, le=64)
    """Jobs one worker process executes at once."""
    jobs_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    jobs_lease_seconds: float = Field(default=60.0, ge=5, le=3600)
    """A claimed job whose lease is not renewed for this long is reclaimed by any worker."""
    jobs_max_attempts: int = Field(default=3, ge=1, le=10)
    jobs_retry_base_seconds: float = Field(default=30.0, ge=0, le=3600)
    jobs_max_active_per_user: int = Field(default=10, ge=1, le=1000)
    jobs_max_schedules_per_user: int = Field(default=25, ge=1, le=1000)
    jobs_min_schedule_interval_seconds: int = Field(default=300, ge=60, le=86400)
    jobs_misfire_grace_seconds: int = Field(default=120, ge=1, le=86400)
    """An occurrence later than this counts as missed and follows the schedule's policy."""
    jobs_deadline_scan_seconds: float = Field(default=2.0, gt=0, le=300)
    """How often the worker looks for due timer/alarm/focus deadlines (M14 plugin state)."""
    jobs_shutdown_grace_seconds: float = Field(default=20.0, ge=0, le=600)
    """On SIGTERM the worker stops claiming and waits this long for running jobs."""
    notify_max_per_hour: int = Field(default=30, ge=1, le=10000)
    notify_delivery_max_attempts: int = Field(default=5, ge=1, le=20)

    fcm_project_id: str | None = Field(default=None, min_length=1, max_length=200)
    fcm_credentials_file: str | None = Field(default=None, min_length=1, max_length=4096)
    """Path to a Firebase service-account JSON file; read only by the FCM token provider."""
    fcm_endpoint: str = Field(default="https://fcm.googleapis.com", pattern=r"^https?://")
    fcm_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @property
    def push_configured(self) -> bool:
        return self.fcm_project_id is not None and self.fcm_credentials_file is not None
