"""Semantic versions of plugin releases and a sortable key for SQL ordering."""

from __future__ import annotations

import re

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?$")


class InvalidVersionError(ValueError):
    """Not a ``MAJOR.MINOR.PATCH[-pre]`` version (as in ``plugin-manifest.schema.json``)."""


def parse_version(version: str) -> tuple[int, int, int, str | None]:
    match = SEMVER.match(version)
    if match is None:
        raise InvalidVersionError(f"{version!r} is not a semantic version")
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), pre


def version_key(version: str) -> str:
    """A string that sorts like semver precedence (a release sorts above its pre-releases).

    Pre-release identifiers compare as plain text, which is enough for ``-rc1`` / ``-beta``.
    """
    major, minor, patch, pre = parse_version(version)
    tail = "1" if pre is None else "0" + pre
    return f"{major:09d}.{minor:09d}.{patch:09d}.{tail}"
