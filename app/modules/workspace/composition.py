"""Workspace mounting hook for the application composition owner."""

from __future__ import annotations

from fastapi import FastAPI

from app.core.database import Database
from app.modules.auth.composition import request_authenticator
from app.modules.workspace.catalog import PluginCatalog, StaticPluginCatalog
from app.modules.workspace.errors import WorkspaceError, workspace_error_handler
from app.modules.workspace.router import create_workspace_router
from app.modules.workspace.service import WorkspaceService


def install_workspace(
    app: FastAPI,
    *,
    database: Database,
    catalog: PluginCatalog | None = None,
    service: WorkspaceService | None = None,
) -> WorkspaceService:
    """Mount after :func:`app.modules.auth.composition.install_auth`.

    ``catalog`` is supplied by M14/M15; the default empty catalog refuses every
    catalog install, so nothing unreviewed can be installed meanwhile.
    """
    workspace_service = service or WorkspaceService(
        database.session_factory, catalog or StaticPluginCatalog()
    )
    app.add_exception_handler(WorkspaceError, workspace_error_handler)
    app.include_router(create_workspace_router(workspace_service, request_authenticator(app)))
    app.state.workspace_service = workspace_service
    return workspace_service
