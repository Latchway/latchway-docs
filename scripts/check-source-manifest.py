#!/usr/bin/env python3
"""Verify the generated public-docs mirror against its checked-in manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any


MANIFEST_NAME = ".latchway-docs-source.json"
EXPECTED_FORMAT = 1
EXPECTED_SOURCE = "latchway/docs/public"
EXPECTED_TOP_LEVEL_KEYS = {
    "files",
    "format",
    "source",
    "source_commit",
    "source_tree_sha256",
}
FORBIDDEN_PARTS = {".git", "__pycache__", "node_modules"}
MIRROR_OWNED_FILES = {
    ".github/MINTLIFY_PRODUCTION_EVIDENCE.md",
    ".github/workflows/docs-checks.yml",
    ".github/workflows/docs-source-sync.yml",
    ".github/workflows/mintlify-production-evidence.yml",
    "schemas/mintlify-production-evidence.schema.json",
    "scripts/check-source-manifest.py",
    "scripts/mintlify-production-evidence.py",
    "scripts/test_check_source_manifest.py",
    "scripts/test_mintlify_production_evidence.py",
}


class ManifestError(ValueError):
    """A manifest or owned-path invariant failed."""


def rejecting_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ManifestError(f"manifest is missing: {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ManifestError(f"manifest is not a regular file: {path}")
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=rejecting_object_pairs,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read manifest: {error}") from error
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be an object")
    if set(data) != EXPECTED_TOP_LEVEL_KEYS:
        raise ManifestError(
            "manifest must contain exactly files, format, source, source_commit, "
            "and source_tree_sha256"
        )
    if type(data["format"]) is not int or data["format"] != EXPECTED_FORMAT:
        raise ManifestError(f"unsupported manifest format: {data['format']!r}")
    if data["source"] != EXPECTED_SOURCE:
        raise ManifestError(f"unexpected canonical source: {data['source']!r}")
    validate_lowercase_hex("source_commit", data["source_commit"], 40)
    validate_lowercase_hex("source_tree_sha256", data["source_tree_sha256"], 64)
    if not isinstance(data["files"], dict) or not data["files"]:
        raise ManifestError("manifest files must be a non-empty object")
    return data


def validate_lowercase_hex(field: str, value: Any, length: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ManifestError(f"{field} must be lowercase {length}-hex")
    return value


def validate_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ManifestError("owned paths must be non-empty strings")
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ManifestError(f"owned path is not canonical POSIX: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ManifestError(f"owned path is not canonical relative POSIX: {value!r}")
    if any(part in {"", ".", ".."} or part in FORBIDDEN_PARTS for part in path.parts):
        raise ManifestError(f"owned path escapes its allowed scope: {value!r}")
    if value == MANIFEST_NAME:
        raise ManifestError("the source manifest cannot own itself")
    return path


def validate_digest(relative: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ManifestError(f"invalid SHA-256 for {relative}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(repository_root: Path, manifest_path: Path) -> int:
    root = repository_root.resolve(strict=True)
    manifest = read_manifest(manifest_path)
    owned: dict[str, str] = manifest["files"]
    problems: list[str] = []
    canonical_paths: set[str] = set()

    for relative, digest_value in sorted(owned.items()):
        try:
            posix_path = validate_relative_path(relative)
            expected_digest = validate_digest(relative, digest_value)
        except ManifestError as error:
            problems.append(str(error))
            continue

        folded = posix_path.as_posix().casefold()
        if posix_path.as_posix() in MIRROR_OWNED_FILES:
            problems.append(f"source manifest claims mirror-owned file: {relative}")
            continue
        if folded in canonical_paths:
            problems.append(f"case-insensitive owned-path collision: {relative}")
            continue
        canonical_paths.add(folded)

        candidate = root.joinpath(*posix_path.parts)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            problems.append(f"owned file is missing: {relative}")
            continue
        except OSError as error:
            problems.append(f"cannot inspect owned file {relative}: {error}")
            continue
        if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            problems.append(f"owned path is not a regular file: {relative}")
            continue
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            problems.append(f"owned file resolves outside repository: {relative}")
            continue
        try:
            actual_digest = sha256(candidate)
        except OSError as error:
            problems.append(f"cannot hash owned file {relative}: {error}")
            continue
        if actual_digest != expected_digest:
            problems.append(f"owned file differs from source checkpoint: {relative}")

    for relative in sorted(MIRROR_OWNED_FILES):
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            problems.append(f"mirror-owned file is missing: {relative}")
            continue
        except OSError as error:
            problems.append(f"cannot inspect mirror-owned file {relative}: {error}")
            continue
        if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            problems.append(f"mirror-owned path is not a regular file: {relative}")

    declared = set(owned) | MIRROR_OWNED_FILES | {MANIFEST_NAME}
    for candidate in sorted(root.rglob("*")):
        relative_path = candidate.relative_to(root)
        if any(part in FORBIDDEN_PARTS for part in relative_path.parts):
            continue
        if candidate.name == ".DS_Store" or candidate.suffix in {".pyc", ".pyo"}:
            continue
        relative = relative_path.as_posix()
        if relative in declared:
            continue
        try:
            metadata = candidate.lstat()
        except OSError as error:
            problems.append(f"cannot inspect repository path {relative}: {error}")
            continue
        if stat.S_ISDIR(metadata.st_mode) and not candidate.is_symlink():
            continue
        problems.append(f"repository file is outside the source checkpoint: {relative}")

    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    print(f"source manifest integrity passed: {len(owned)} owned files")
    return 0


def main() -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--manifest", type=Path)
    arguments = parser.parse_args()
    root = arguments.root
    manifest_path = arguments.manifest or root / MANIFEST_NAME
    try:
        return verify(root, manifest_path)
    except (ManifestError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
