from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("verify-mintlify-score.py")
SPEC = importlib.util.spec_from_file_location("verify_mintlify_score", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def valid_score() -> dict[str, object]:
    return {
        "canonicalUrl": "https://docs.latchway.dev/",
        "slug": "latchway",
        "displayDomain": "docs.latchway.dev",
        "displayName": "Latchway",
        "computedAt": "2026-09-02T05:00:00.000Z",
        "overallScore": 94,
        "overallGrade": "A",
        "passedChecks": 2,
        "totalChecks": 3,
        "checks": [
            {"id": "llms", "name": "llms.txt", "status": "pass"},
            {
                "id": "discovery",
                "name": "Discovery",
                "status": "warn",
                "children": [
                    {
                        "id": "mcp",
                        "name": "MCP server",
                        "status": "skip",
                        "message": "Not enabled for this site.",
                    }
                ],
            },
        ],
        "status": "ready",
    }


class VerifyMintlifyScoreTests(unittest.TestCase):
    def test_accepts_exact_canonical_score_without_failures(self) -> None:
        score = valid_score()
        self.assertEqual(MODULE.validate_score(score), score)

    def test_rejects_failed_or_error_check_at_any_depth(self) -> None:
        for status in ("fail", "error"):
            with self.subTest(status=status):
                score = valid_score()
                score["checks"][1]["children"][0]["status"] = status
                with self.assertRaisesRegex(MODULE.ScoreError, "did not pass"):
                    MODULE.validate_score(score)

    def test_rejects_wrong_origin_or_incomplete_response(self) -> None:
        score = valid_score()
        score["canonicalUrl"] = "https://latchway.mintlify.app"
        with self.assertRaisesRegex(MODULE.ScoreError, "canonicalUrl"):
            MODULE.validate_score(score)
        score = valid_score()
        score["status"] = "queued"
        with self.assertRaisesRegex(MODULE.ScoreError, "completed response"):
            MODULE.validate_score(score)

    def test_rejects_unknown_fields_or_duplicate_check_ids(self) -> None:
        score = valid_score()
        score["unreviewed"] = True
        with self.assertRaisesRegex(MODULE.ScoreError, "reviewed response fields"):
            MODULE.validate_score(score)
        score = valid_score()
        score["checks"][1]["id"] = "llms"
        with self.assertRaisesRegex(MODULE.ScoreError, "duplicate check id"):
            MODULE.validate_score(score)

    def test_loader_rejects_duplicate_json_keys_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"canonicalUrl":"a","canonicalUrl":"b"}\n')
            with self.assertRaisesRegex(MODULE.ScoreError, "duplicate JSON key"):
                MODULE.load_score(duplicate)

            target = root / "target.json"
            target.write_text(json.dumps(valid_score()) + "\n")
            link = root / "score.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(MODULE.ScoreError, "regular file"):
                MODULE.load_score(link)

    def test_rejects_impossible_counts(self) -> None:
        score = copy.deepcopy(valid_score())
        score["passedChecks"] = 4
        with self.assertRaisesRegex(MODULE.ScoreError, "cannot exceed"):
            MODULE.validate_score(score)

    def test_rejects_malformed_or_offset_free_computed_time(self) -> None:
        for computed_at in ("not-a-time", "2026-09-02T05:00:00"):
            with self.subTest(computed_at=computed_at):
                score = copy.deepcopy(valid_score())
                score["computedAt"] = computed_at
                with self.assertRaisesRegex(MODULE.ScoreError, "computedAt"):
                    MODULE.validate_score(score)


if __name__ == "__main__":
    unittest.main()
