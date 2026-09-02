#!/usr/bin/env python3
"""Validate the authenticated Mintlify agent-readiness score fail closed."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import stat
import sys
from typing import Any


EXPECTED_ORIGIN = "https://docs.latchway.dev"
EXPECTED_TOP_LEVEL_KEYS = {
    "canonicalUrl",
    "checks",
    "computedAt",
    "displayDomain",
    "displayName",
    "overallGrade",
    "overallScore",
    "passedChecks",
    "slug",
    "status",
    "totalChecks",
}
EXPECTED_CHECK_KEYS = {"category", "children", "id", "message", "name", "status"}
ALLOWED_GRADES = {"A+", "A", "B", "C", "D", "F"}
ALLOWED_RESPONSE_STATUSES = {"ready", "stale_refresh_queued"}
ALLOWED_CHECK_STATUSES = {"pass", "warn", "skip"}
KNOWN_CHECK_STATUSES = ALLOWED_CHECK_STATUSES | {"fail", "error"}
MAXIMUM_BYTES = 1024 * 1024


class ScoreError(ValueError):
    """The score output does not meet the protected production policy."""


def rejecting_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScoreError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def require_nonempty_string(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ScoreError(f"{label} must be a non-empty canonical string")
    return value


def require_integer(label: str, value: Any, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ScoreError(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def validate_check(value: Any, path: str, identifiers: set[str]) -> None:
    if not isinstance(value, dict):
        raise ScoreError(f"{path} must be an object")
    unknown = set(value) - EXPECTED_CHECK_KEYS
    required = {"id", "name", "status"}
    missing = required - set(value)
    if unknown or missing:
        raise ScoreError(f"{path} has unsupported or missing fields")

    identifier = require_nonempty_string(f"{path}.id", value["id"])
    if identifier in identifiers:
        raise ScoreError(f"duplicate check id: {identifier}")
    identifiers.add(identifier)
    require_nonempty_string(f"{path}.name", value["name"])

    status_value = value["status"]
    if status_value not in KNOWN_CHECK_STATUSES:
        raise ScoreError(f"{path}.status is unknown")
    if status_value not in ALLOWED_CHECK_STATUSES:
        raise ScoreError(f"{path} did not pass the agent-readiness policy: {status_value}")

    for optional in ("category", "message"):
        if optional in value:
            require_nonempty_string(f"{path}.{optional}", value[optional])

    children = value.get("children", [])
    if not isinstance(children, list):
        raise ScoreError(f"{path}.children must be an array")
    for index, child in enumerate(children):
        validate_check(child, f"{path}.children[{index}]", identifiers)


def validate_score(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != EXPECTED_TOP_LEVEL_KEYS:
        raise ScoreError("score must contain exactly the reviewed response fields")

    canonical_url = require_nonempty_string("canonicalUrl", value["canonicalUrl"])
    if canonical_url not in {EXPECTED_ORIGIN, f"{EXPECTED_ORIGIN}/"}:
        raise ScoreError(f"score canonicalUrl must be {EXPECTED_ORIGIN}")
    if value["displayDomain"] != "docs.latchway.dev":
        raise ScoreError("score displayDomain must be docs.latchway.dev")
    require_nonempty_string("displayName", value["displayName"])
    require_nonempty_string("slug", value["slug"])
    computed_at = require_nonempty_string("computedAt", value["computedAt"])
    try:
        parsed_time = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ScoreError("score computedAt must be an RFC 3339 timestamp") from error
    if parsed_time.tzinfo is None:
        raise ScoreError("score computedAt must include a UTC offset")

    if value["status"] not in ALLOWED_RESPONSE_STATUSES:
        raise ScoreError("score status is not a completed response")
    if value["overallGrade"] not in ALLOWED_GRADES:
        raise ScoreError("score overallGrade is unknown")
    require_integer("overallScore", value["overallScore"], minimum=0, maximum=100)
    passed = require_integer("passedChecks", value["passedChecks"], minimum=0, maximum=10000)
    total = require_integer("totalChecks", value["totalChecks"], minimum=1, maximum=10000)
    if passed > total:
        raise ScoreError("passedChecks cannot exceed totalChecks")

    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise ScoreError("score checks must be a non-empty array")
    identifiers: set[str] = set()
    for index, check in enumerate(checks):
        validate_check(check, f"checks[{index}]", identifiers)
    return value


def load_score(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ScoreError(f"cannot inspect score output: {error}") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ScoreError("score output must be a regular file")
    if metadata.st_size <= 0 or metadata.st_size > MAXIMUM_BYTES:
        raise ScoreError("score output size is outside the allowed range")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=rejecting_object_pairs,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScoreError(f"score output is not valid JSON: {error}") from error
    return validate_score(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        score = load_score(arguments.score)
    except ScoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        "Mintlify agent-readiness score passed: "
        f"{score['overallScore']} ({score['overallGrade']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
