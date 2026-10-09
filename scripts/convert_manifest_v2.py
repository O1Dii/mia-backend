"""Convert a POC manifest (recipe v1 or application v2) to a declarative ``mia.json``.

Usage (from the repository root)::

    .venv/bin/python scripts/convert_manifest_v2.py app.json --out-dir plugins/my-app \\
        [--category productivity]

Writes ``mia.json`` and ``SKILL.md`` into ``--out-dir`` (created if needed) and prints a
JSON report with the plugin id and file names. Without ``--out-dir`` the ``mia.json`` is
printed to stdout. Exit code 2 on an invalid manifest. The logic is
``app.modules.plugins.declarative.convert`` (importable as ``convert_file``/``main``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.plugins.declarative.convert import (  # noqa: E402
    CATEGORIES,
    ConversionError,
    ConvertedPlugin,
    convert_manifest,
)


def convert_file(path: Path, *, category: str = "productivity") -> ConvertedPlugin:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConversionError(f"cannot read {path.name}: {type(error).__name__}") from None
    if not isinstance(source, dict):
        raise ConversionError("manifest must be a JSON object")
    return convert_manifest(source, category=category)


def write_package(converted: ConvertedPlugin, out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mia.json").write_text(
        json.dumps(converted.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "SKILL.md").write_text(converted.skill_md, encoding="utf-8")
    return ["mia.json", "SKILL.md"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("manifest", type=Path, help="v1 recipe or v2 application JSON")
    parser.add_argument("--out-dir", type=Path, help="directory for mia.json and SKILL.md")
    parser.add_argument("--category", default="productivity", choices=CATEGORIES)
    args = parser.parse_args(argv)
    try:
        converted = convert_file(args.manifest, category=args.category)
    except ConversionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.out_dir is None:
        print(json.dumps(converted.manifest, ensure_ascii=False, indent=2))
        return 0
    files = write_package(converted, args.out_dir)
    report = {"id": converted.manifest["id"], "files": files, "warnings": converted.warnings}
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
