"""Move web-POC SQLite accounts into the Postgres workspace schema (M10).

Usage (run from the repository root; always on a copy of the production file):

    DATABASE_URL=postgresql+asyncpg://… \\
      .venv/bin/python scripts/migrate_web_sqlite.py --sqlite /path/to/copy.sqlite3 \\
      [--owner-sub <google sub> …] [--identity-map map.json] [--dry-run]

The target URL is read from the process environment only (``DATABASE_URL`` or
``MIA_TEST_DATABASE_URL``); no dotenv or credential file is opened. The SQLite
file is opened read-only. Output is a JSON report with ids and counts only.
Rules: ``app/modules/workspace/legacy_import.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.workspace.legacy_import import (  # noqa: E402
    ImportOptions,
    LegacySchemaError,
    import_legacy_snapshot,
    read_legacy_database,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", type=Path, required=True, help="web-POC SQLite file (copy)")
    parser.add_argument("--source-id", default="web_poc", help="stable id of this source")
    parser.add_argument(
        "--owner-sub",
        action="append",
        default=None,
        help="import only this verified Google sub (repeatable)",
    )
    parser.add_argument(
        "--identity-map",
        type=Path,
        help='JSON object {"hub:<uuid>": "<verified Google sub>"} for hub accounts',
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    return parser.parse_args(argv)


async def run(argv: list[str]) -> int:
    args = parse_args(argv)
    url = os.environ.get("DATABASE_URL") or os.environ.get("MIA_TEST_DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set in the process environment", file=sys.stderr)
        return 2
    identity_map: dict[str, str] = {}
    if args.identity_map is not None:
        loaded = json.loads(args.identity_map.read_text())
        if not isinstance(loaded, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in loaded.items()
        ):
            print("identity map must be a JSON object of strings", file=sys.stderr)
            return 2
        identity_map = loaded
    try:
        snapshot = read_legacy_database(args.sqlite)
    except LegacySchemaError as exc:
        print(f"source rejected: {exc}", file=sys.stderr)
        return 2
    engine = create_async_engine(url)
    try:
        report = await import_legacy_snapshot(
            async_sessionmaker(engine, expire_on_commit=False),
            snapshot,
            ImportOptions(
                source_id=args.source_id,
                owner_subs=frozenset(args.owner_sub) if args.owner_sub else None,
                identity_map=identity_map,
                dry_run=args.dry_run,
            ),
        )
    except LegacySchemaError as exc:
        print(f"target rejected: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(sys.argv[1:])))
