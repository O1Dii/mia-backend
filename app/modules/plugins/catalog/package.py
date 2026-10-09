"""A plugin package on disk: ``mia.json`` plus its non-code files, read as data.

Python code (``server.py``) is never stored in a release: builtin servers ship with the
core image, remote servers run elsewhere, declarative ones have no code. Only the files the
host serves or shows are kept: ``SKILL.md``, ``ui/*.html`` and listing images.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

MAX_MANIFEST_BYTES: Final = 256 * 1024
MAX_FILE_BYTES: Final = 512 * 1024
MAX_PACKAGE_BYTES: Final = 4 * 1024 * 1024
MAX_FILES: Final = 64
TEXT_SUFFIXES: Final = frozenset({".md", ".html", ".txt"})
IMAGE_SUFFIXES: Final = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})
ALLOWED_SUFFIXES: Final = TEXT_SUFFIXES | IMAGE_SUFFIXES
_SKIP_DIRS: Final = frozenset({"__pycache__", "node_modules"})
_SAFE_PATH: Final = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class PackageReadError(ValueError):
    """The directory is not a readable plugin package."""


@dataclass(frozen=True, slots=True)
class PackageFile:
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class PluginPackage:
    manifest: dict[str, Any]
    files: Mapping[str, PackageFile] = field(default_factory=dict)
    directory: Path | None = None
    """Where the package was read from; builtin servers also use it as ``plugin_dir``."""

    @property
    def plugin_id(self) -> str:
        return str(self.manifest.get("id", ""))

    @property
    def version(self) -> str:
        return str(self.manifest.get("version", ""))

    def digest(self) -> str:
        """``sha256:<hex>`` over the canonical manifest and the sorted file digests."""
        canonical = json.dumps(
            {
                "manifest": self.manifest,
                "files": {path: self.files[path].sha256 for path in sorted(self.files)},
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def text(self, path: str) -> str | None:
        file = self.files.get(path)
        if file is None:
            return None
        try:
            return file.content.decode("utf-8")
        except UnicodeDecodeError:
            return None


def load_package(directory: Path) -> PluginPackage:
    """Read ``mia.json`` and the allowed data files below ``directory``."""
    root = directory.resolve()
    manifest_path = root / "mia.json"
    if not manifest_path.is_file():
        raise PackageReadError("mia.json is missing")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise PackageReadError("mia.json is too large")
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise PackageReadError("mia.json is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise PackageReadError("mia.json must be an object")
    files: dict[str, PackageFile] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts):
            continue
        if path.is_symlink():
            raise PackageReadError(f"{relative.as_posix()}: symbolic links are not allowed")
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        if not _SAFE_PATH.match(relative.as_posix()):
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise PackageReadError(f"{relative.as_posix()}: file exceeds {MAX_FILE_BYTES} bytes")
        total += size
        files[relative.as_posix()] = PackageFile(path.read_bytes())
        if len(files) > MAX_FILES or total > MAX_PACKAGE_BYTES:
            raise PackageReadError("package has too many or too large files")
    return PluginPackage(manifest=manifest, files=files, directory=root)


def encode_files(files: Mapping[str, PackageFile]) -> dict[str, dict[str, str]]:
    """JSON form stored in ``plugin_releases.files``."""
    encoded: dict[str, dict[str, str]] = {}
    for path, file in files.items():
        try:
            text = file.content.decode("utf-8")
        except UnicodeDecodeError:
            encoded[path] = {
                "encoding": "base64",
                "content": base64.b64encode(file.content).decode("ascii"),
                "sha256": file.sha256,
            }
        else:
            encoded[path] = {"encoding": "utf8", "content": text, "sha256": file.sha256}
    return encoded


def decode_files(stored: Mapping[str, Any]) -> dict[str, PackageFile]:
    files: dict[str, PackageFile] = {}
    for path, entry in stored.items():
        if not isinstance(entry, Mapping):
            continue
        content = str(entry.get("content", ""))
        if entry.get("encoding") == "base64":
            files[path] = PackageFile(base64.b64decode(content))
        else:
            files[path] = PackageFile(content.encode("utf-8"))
    return files


def media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"
