from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("check-source-manifest.py")


class SourceManifestCheckTests(unittest.TestCase):
    SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
    SOURCE_TREE_SHA256 = (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    MIRROR_OWNED_FILES = (
        ".github/MINTLIFY_PRODUCTION_EVIDENCE.md",
        ".github/workflows/docs-checks.yml",
        ".github/workflows/docs-source-sync.yml",
        ".github/workflows/mintlify-production-evidence.yml",
        "schemas/mintlify-production-evidence.schema.json",
        "scripts/check-source-manifest.py",
        "scripts/mintlify-production-evidence.py",
        "scripts/test_check_source_manifest.py",
        "scripts/test_mintlify_production_evidence.py",
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for relative in self.MIRROR_OWNED_FILES:
            self.write_file(relative, b"mirror-owned\n")
        self.write_file("index.mdx", b"# Latchway\n")
        self.write_manifest({"index.mdx": self.digest(b"# Latchway\n")})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def digest(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def write_file(self, relative: str, value: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def write_manifest(self, files: dict[str, str], **overrides: object) -> None:
        payload: dict[str, object] = {
            "files": files,
            "format": 1,
            "source": "latchway/docs/public",
            "source_commit": self.SOURCE_COMMIT,
            "source_tree_sha256": self.SOURCE_TREE_SHA256,
        }
        payload.update(overrides)
        (self.root / ".latchway-docs-source.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def run_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root)],
            check=False,
            capture_output=True,
            text=True,
        )

    def assert_rejected(self, fragment: str) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(fragment, result.stderr)

    def test_accepts_exact_owned_files_and_ignores_unowned_workflow(self) -> None:
        self.write_file(".github/workflows/docs-checks.yml", b"name: checks\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 owned files", result.stdout)

    def test_rejects_unmanifested_publishable_file(self) -> None:
        self.write_file("unreviewed.mdx", b"# Unreviewed\n")
        self.assert_rejected(
            "repository file is outside the source checkpoint: unreviewed.mdx"
        )

    def test_rejects_tampered_owned_file(self) -> None:
        self.write_file("index.mdx", b"tampered\n")
        self.assert_rejected("owned file differs from source checkpoint: index.mdx")

    def test_rejects_missing_owned_file(self) -> None:
        (self.root / "index.mdx").unlink()
        self.assert_rejected("owned file is missing: index.mdx")

    def test_rejects_symlinked_owned_file(self) -> None:
        (self.root / "index.mdx").unlink()
        (self.root / "target.mdx").write_text("# Latchway\n", encoding="utf-8")
        (self.root / "index.mdx").symlink_to("target.mdx")
        self.assert_rejected("owned path is not a regular file: index.mdx")

    def test_rejects_path_traversal(self) -> None:
        self.write_manifest({"../outside.mdx": "0" * 64})
        self.assert_rejected("owned path escapes its allowed scope")

    def test_rejects_absolute_path(self) -> None:
        self.write_manifest({"/tmp/outside.mdx": "0" * 64})
        self.assert_rejected("owned path is not canonical relative POSIX")

    def test_rejects_backslash_path(self) -> None:
        self.write_manifest({"docs\\index.mdx": "0" * 64})
        self.assert_rejected("owned path is not canonical POSIX")

    def test_rejects_case_insensitive_collision(self) -> None:
        self.write_file("Index.mdx", b"second\n")
        self.write_manifest(
            {
                "index.mdx": self.digest(b"# Latchway\n"),
                "Index.mdx": self.digest(b"second\n"),
            }
        )
        self.assert_rejected("case-insensitive owned-path collision")

    def test_rejects_non_regular_owned_path(self) -> None:
        (self.root / "index.mdx").unlink()
        (self.root / "index.mdx").mkdir()
        self.assert_rejected("owned path is not a regular file: index.mdx")

    def test_rejects_uppercase_or_short_digest(self) -> None:
        self.write_manifest({"index.mdx": "A" * 64})
        self.assert_rejected("invalid SHA-256 for index.mdx")
        self.write_manifest({"index.mdx": "0" * 63})
        self.assert_rejected("invalid SHA-256 for index.mdx")

    def test_rejects_unsupported_metadata(self) -> None:
        self.write_manifest(
            {"index.mdx": self.digest(b"# Latchway\n")},
            format=2,
        )
        self.assert_rejected("unsupported manifest format")
        self.write_manifest(
            {"index.mdx": self.digest(b"# Latchway\n")},
            format=1.0,
        )
        self.assert_rejected("unsupported manifest format")
        self.write_manifest(
            {"index.mdx": self.digest(b"# Latchway\n")},
            source="other/source",
        )
        self.assert_rejected("unexpected canonical source")

    def test_rejects_invalid_source_commit(self) -> None:
        invalid_values: tuple[object, ...] = (
            "A" * 40,
            "0" * 39,
            "g" * 40,
            0,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.write_manifest(
                    {"index.mdx": self.digest(b"# Latchway\n")},
                    source_commit=value,
                )
                self.assert_rejected("source_commit must be lowercase 40-hex")

    def test_rejects_invalid_source_tree_sha256(self) -> None:
        invalid_values: tuple[object, ...] = (
            "A" * 64,
            "0" * 63,
            "g" * 64,
            0,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.write_manifest(
                    {"index.mdx": self.digest(b"# Latchway\n")},
                    source_tree_sha256=value,
                )
                self.assert_rejected("source_tree_sha256 must be lowercase 64-hex")

    def test_rejects_missing_provenance_field(self) -> None:
        manifest = self.root / ".latchway-docs-source.json"
        for field in ("source_commit", "source_tree_sha256"):
            with self.subTest(field=field):
                self.write_manifest({"index.mdx": self.digest(b"# Latchway\n")})
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                del payload[field]
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                self.assert_rejected("manifest must contain exactly")

    def test_rejects_manifest_claiming_mirror_owned_workflow(self) -> None:
        workflow = self.write_file(
            ".github/workflows/docs-checks.yml", b"name: checks\n"
        )
        self.write_manifest(
            {
                "index.mdx": self.digest(b"# Latchway\n"),
                ".github/workflows/docs-checks.yml": self.digest(
                    workflow.read_bytes()
                ),
            }
        )
        self.assert_rejected("source manifest claims mirror-owned file")

    def test_rejects_unknown_top_level_field(self) -> None:
        manifest = self.root / ".latchway-docs-source.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["commit"] = "unverified"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        self.assert_rejected("manifest must contain exactly")

    def test_rejects_duplicate_json_keys(self) -> None:
        (self.root / ".latchway-docs-source.json").write_text(
            '{"format":1,"source":"latchway/docs/public",'
            '"files":{"index.mdx":"' + self.digest(b"# Latchway\n") + '",'
            '"index.mdx":"' + "0" * 64 + '"}}',
            encoding="utf-8",
        )
        self.assert_rejected("duplicate JSON key: index.mdx")

    def test_rejects_manifest_symlink(self) -> None:
        manifest = self.root / ".latchway-docs-source.json"
        target = self.root / "manifest.json"
        manifest.rename(target)
        manifest.symlink_to(target.name)
        self.assert_rejected("manifest is not a regular file")


if __name__ == "__main__":
    unittest.main()
