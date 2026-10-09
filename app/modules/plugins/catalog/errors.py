"""Store errors. They derive from :class:`WorkspaceError`, so the handler that
``install_workspace`` registers renders them in the public error envelope."""

from __future__ import annotations

from typing import Any

from app.modules.workspace.errors import WorkspaceError


class PublishForbiddenError(WorkspaceError):
    status_code = 403
    code = "permission_denied"


class PackageInvalidError(WorkspaceError):
    """The package failed a publication check; ``details.problems`` lists them."""

    status_code = 422
    code = "plugin_package_invalid"

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__(
            "The plugin package failed publication checks.", details={"problems": problems}
        )
        self.problems = problems


class ReleaseExistsError(WorkspaceError):
    status_code = 409
    code = "release_exists"


class ConsentRequiredError(WorkspaceError):
    """The update asks for permissions the user has not consented to."""

    status_code = 409
    code = "consent_required"

    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(message, details=details)


class StoreUnavailableError(WorkspaceError):
    status_code = 503
    code = "service_unavailable"
