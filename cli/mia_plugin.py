"""``mia plugin``: validate and publish plugin packages (M15).

Run on the backend host from the repository root, with the backend's environment
(``DATABASE_URL``, ``STORE_ADMIN_USER_IDS``, ``TOOLS_MCP_HOST_ALLOWLIST``)::

    python -m cli.mia_plugin validate app/plugins/builtin/timer [--discover]
    python -m cli.mia_plugin publish  app/plugins/builtin/timer --as-user <users.id>
    python -m cli.mia_plugin publish-builtin --as-user <users.id>
    python -m cli.mia_plugin yank builtin.timer 1.0.0 --as-user <users.id>

- ``validate`` checks ``mia.json`` against ``contracts/mvp/v1``, the files it references and
  the tile markup; with ``--discover`` it also connects (SSRF-guarded) and compares
  ``tools/list`` with the manifest. No database is touched.
- ``publish`` runs the same checks plus discovery, then stores an immutable ``PluginRelease``.
  Only users listed in ``STORE_ADMIN_USER_IDS`` may publish (``--as-user`` or
  ``MIA_ADMIN_USER_ID``). A remote server that needs a token for ``tools/list`` takes
  ``--token-env NAME`` (the token is read from that environment variable, never from argv).
- Exit codes: 0 ok, 1 checks failed, 2 usage/permission/infrastructure problem.

Output is JSON lines; tokens and package contents are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):  # ``python cli/mia_plugin.py`` from the repository root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.core.database import Database  # noqa: E402
from app.modules.plugins.builtin import (  # noqa: E402
    BUILTIN_ROOT,
    HostServices,
    make_inprocess_factory,
)
from app.modules.plugins.catalog.discovery import (  # noqa: E402
    DiscoveryError,
    discover,
    token_connector,
)
from app.modules.plugins.catalog.errors import (  # noqa: E402
    PackageInvalidError,
    PublishForbiddenError,
    ReleaseExistsError,
    StoreUnavailableError,
)
from app.modules.plugins.catalog.package import PackageReadError, load_package  # noqa: E402
from app.modules.plugins.catalog.service import CatalogService  # noqa: E402
from app.modules.plugins.catalog.showcase import showcase_for  # noqa: E402
from app.modules.plugins.catalog.validation import (  # noqa: E402
    ContractsUnavailableError,
    Problem,
    package_problems,
)
from app.modules.plugins.composition import RouterServerFactory  # noqa: E402
from app.modules.plugins.install.composition import admin_user_ids  # noqa: E402
from app.modules.tools.composition import host_allowlist  # noqa: E402
from app.modules.workspace.errors import WorkspaceError  # noqa: E402

OK, CHECKS_FAILED, USAGE = 0, 1, 2
ServiceFactory = Callable[[Settings], tuple[CatalogService, Callable[[], Awaitable[None]]]]


def _emit(out: TextIO, **fields: Any) -> None:
    out.write(json.dumps(fields, ensure_ascii=False, sort_keys=True) + "\n")


def _problems(out: TextIO, problems: Sequence[Problem | dict[str, str]]) -> None:
    for problem in problems:
        body = (
            problem
            if isinstance(problem, dict)
            else {"code": problem.code, "path": problem.path, "message": problem.message}
        )
        _emit(out, problem=body)


def default_service(settings: Settings) -> tuple[CatalogService, Callable[[], Awaitable[None]]]:
    """The catalog over the configured database; builtin servers run with in-memory state."""
    database = Database(settings.database_url)
    factory = RouterServerFactory(make_inprocess_factory(HostServices()))
    allowlist = host_allowlist(settings)
    service = CatalogService(
        database.session_factory,
        server_factory=factory,
        admin_user_ids=admin_user_ids(settings),
        connector_factory=lambda token: token_connector(
            token,
            server_factory=factory,
            global_allowlist=allowlist,
            list_timeout_s=settings.tools_list_timeout_seconds,
        ),
    )
    return service, database.close


def _token(args: argparse.Namespace) -> str | None:
    name = getattr(args, "token_env", None)
    if not name:
        return None
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"environment variable {name} is empty")
    return value


async def _validate(args: argparse.Namespace, out: TextIO, settings: Settings) -> int:
    package = load_package(Path(args.directory))
    problems = package_problems(package)
    if not problems and args.discover:
        factory = RouterServerFactory(make_inprocess_factory(HostServices()))
        connector = token_connector(
            _token(args),
            server_factory=factory,
            global_allowlist=host_allowlist(settings),
            list_timeout_s=settings.tools_list_timeout_seconds,
        )
        try:
            found = await discover(package, connector=connector, server_factory=factory)
        except DiscoveryError as error:
            problems = error.problems
        else:
            _emit(out, tools=sorted(tool.name for tool in found.tools), tools_digest=found.digest)
    _problems(out, problems)
    _emit(out, plugin_id=package.plugin_id, version=package.version, valid=not problems)
    return OK if not problems else CHECKS_FAILED


async def _publish_one(
    service: CatalogService, directory: Path, args: argparse.Namespace, out: TextIO
) -> int:
    package = load_package(directory)
    result = await service.publish(
        package,
        actor_user_id=args.as_user,
        store_meta=showcase_for(package.plugin_id),
        discover_token=_token(args),
    )
    release = result.release
    _emit(
        out,
        plugin_id=release.plugin_id,
        version=release.version,
        created=result.created,
        tools_digest=release.tools_digest,
        package_digest=release.package_digest,
        grants=release.grants,
    )
    return OK


async def _publish(args: argparse.Namespace, out: TextIO, service: CatalogService) -> int:
    return await _publish_one(service, Path(args.directory), args, out)


async def _publish_builtin(args: argparse.Namespace, out: TextIO, service: CatalogService) -> int:
    status = OK
    for manifest in sorted(BUILTIN_ROOT.glob("*/mia.json")):
        status = max(status, await _publish_one(service, manifest.parent, args, out))
    return status


async def _yank(args: argparse.Namespace, out: TextIO, service: CatalogService) -> int:
    await service.yank(args.plugin_id, args.version, actor_user_id=args.as_user)
    _emit(out, plugin_id=args.plugin_id, version=args.version, status="yanked")
    return OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mia plugin", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser, *, publishing: bool) -> None:
        if publishing:
            sub.add_argument(
                "--as-user",
                default=os.environ.get("MIA_ADMIN_USER_ID"),
                help="users.id of the administrator (default: $MIA_ADMIN_USER_ID)",
            )

    validate = commands.add_parser("validate", help="check a package without publishing")
    validate.add_argument("directory")
    validate.add_argument("--discover", action="store_true", help="also compare tools/list")
    validate.add_argument("--token-env", help="env var with a bearer token for tools/list")
    publish = commands.add_parser("publish", help="publish a package as a release")
    publish.add_argument("directory")
    publish.add_argument("--token-env", help="env var with a bearer token for tools/list")
    common(publish, publishing=True)
    builtin = commands.add_parser("publish-builtin", help="publish every builtin package")
    common(builtin, publishing=True)
    yank = commands.add_parser("yank", help="hide a release from the catalog and new installs")
    yank.add_argument("plugin_id")
    yank.add_argument("version")
    common(yank, publishing=True)
    return parser


async def run(
    argv: Sequence[str],
    *,
    out: TextIO = sys.stdout,
    settings: Settings | None = None,
    service_factory: ServiceFactory = default_service,
) -> int:
    args = _parser().parse_args(argv)
    settings = settings or Settings()
    try:
        if args.command == "validate":
            return await _validate(args, out, settings)
        if not args.as_user:
            _emit(out, error="--as-user (or MIA_ADMIN_USER_ID) is required")
            return USAGE
        service, close = service_factory(settings)
        try:
            handler = {"publish": _publish, "publish-builtin": _publish_builtin, "yank": _yank}[
                args.command
            ]
            return await handler(args, out, service)
        finally:
            await close()
    except PackageInvalidError as error:
        _problems(out, error.problems)
        _emit(out, error=error.message)
        return CHECKS_FAILED
    except PackageReadError as error:
        _emit(out, error=str(error))
        return CHECKS_FAILED
    except (
        PublishForbiddenError,
        ReleaseExistsError,
        StoreUnavailableError,
        WorkspaceError,
    ) as error:
        _emit(out, error=error.message, code=error.code)
        return USAGE
    except ContractsUnavailableError as error:
        _emit(out, error=str(error))
        return USAGE


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(list(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    raise SystemExit(main())
