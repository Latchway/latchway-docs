from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("mintlify-production-evidence.py")
SPEC = importlib.util.spec_from_file_location("mintlify_production_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


COMMIT = "0123456789abcdef0123456789abcdef01234567"
CORE_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
DEPLOYMENT_ID = 6163376314
ACTOR = {"id": 109931778, "login": "mintlify[bot]", "type": "Bot"}
NOW = datetime(2026, 9, 1, 0, 10, 0, tzinfo=timezone.utc)


def compact_sha(value: object) -> str:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(payload).hexdigest()


def response(
    url: str,
    body: str,
    content_type: str,
    *,
    status: int = 200,
    final_url: str | None = None,
    headers: dict[str, str] | None = None,
):
    all_headers = {"content-type": content_type}
    all_headers.update(headers or {})
    return MODULE.FetchResult(
        url=url,
        final_url=final_url or url,
        status=status,
        headers=all_headers,
        body=body.encode(),
    )


class FakeFetcher:
    def __init__(self, results: dict[tuple[str, bool], object]) -> None:
        self.results = results

    def fetch(self, url: str, *, follow_redirects: bool = True, maximum: int):
        del maximum
        key = (url, follow_redirects)
        if key not in self.results:
            raise MODULE.EvidenceError(f"unexpected test fetch: {key}")
        result = self.results[key]
        if isinstance(result, Exception):
            raise result
        return result


class MintlifyProductionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.files = {
            "index.mdx": (
                "---\n"
                'title: "Home"\n'
                'description: "Production home description."\n'
                "---\n\nHome.\n"
            ).encode(),
            "guide.mdx": (
                "---\n"
                'title: "Guide"\n'
                'description: "Production guide description."\n'
                "---\n\nGuide.\n"
            ).encode(),
            "llms.txt": (
                "# Latchway public documentation\n\n"
                "- [Overview](https://docs.latchway.dev/index.md): "
                "Understand the production home.\n"
                "- [Production guide](https://docs.latchway.dev/guide.md): "
                "Complete the production guide.\n"
            ).encode(),
            "config/redirects.json": (
                '[{"source":"/old","destination":"/guide","permanent":true}]\n'
            ).encode(),
        }
        for relative, payload in self.files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        digests = {
            relative: hashlib.sha256(payload).hexdigest()
            for relative, payload in sorted(self.files.items())
        }
        manifest = {
            "files": digests,
            "format": 1,
            "source": "latchway/docs/public",
            "source_commit": CORE_COMMIT,
            "source_tree_sha256": compact_sha(digests),
        }
        (self.root / ".latchway-docs-source.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.deployment = {
            "id": DEPLOYMENT_ID,
            "sha": COMMIT,
            "ref": "main",
            "task": "deploy",
            "environment": "production",
            "production_environment": True,
            "transient_environment": False,
            "creator": copy.deepcopy(ACTOR),
            "created_at": "2026-09-01T00:00:00Z",
        }
        self.statuses = [
            {
                "id": 17519253886,
                "state": "success",
                "environment": "production",
                "environment_url": "https://latchway.mintlify.app",
                "creator": copy.deepcopy(ACTOR),
                "updated_at": "2026-09-01T00:05:00Z",
            }
        ]
        root_url = "https://docs.latchway.dev/"
        guide_url = "https://docs.latchway.dev/guide"
        index_html = (
            '<!doctype html><html lang="en"><head><title>Home - Latchway</title>'
            '<meta name="description" content="Production home description."></head>'
            '<body><main><h1>Home</h1><a href="/guide">Guide</a>'
            '<img src="/logo.svg" alt=""></main></body></html>'
        )
        guide_html = (
            '<!doctype html><html lang="en"><head><title>Guide - Latchway</title>'
            '<meta name="description" content="Production guide description."></head>'
            '<body><main><h1>Guide</h1><a href="/">Home</a></main></body></html>'
        )
        live_llms = self.files["llms.txt"].decode()
        llms_full = (
            "# Home\nSource: https://docs.latchway.dev/\n\n"
            "Production home description.\n\n"
            "# Guide\nSource: https://docs.latchway.dev/guide\n\n"
            "Production guide description.\n\n" + ("verified documentation\n" * 30)
        )
        index_markdown = (
            "> ## Documentation Index\n\n# Home\n\nProduction home description.\n"
        )
        guide_markdown = (
            "> ## Documentation Index\n\n# Guide\n\nProduction guide description.\n"
        )
        self.results = {
            (root_url, True): response(root_url, index_html, "text/html; charset=utf-8"),
            (guide_url, True): response(guide_url, guide_html, "text/html; charset=utf-8"),
            ("https://docs.latchway.dev/old", False): response(
                "https://docs.latchway.dev/old",
                "",
                "text/html",
                status=308,
                headers={"location": "/guide"},
            ),
            ("https://docs.latchway.dev/llms.txt", True): response(
                "https://docs.latchway.dev/llms.txt", live_llms, "text/plain; charset=utf-8"
            ),
            ("https://docs.latchway.dev/llms-full.txt", True): response(
                "https://docs.latchway.dev/llms-full.txt",
                llms_full,
                "text/plain; charset=utf-8",
            ),
            ("https://docs.latchway.dev/index.md", True): response(
                "https://docs.latchway.dev/index.md",
                index_markdown,
                "text/markdown; charset=utf-8",
            ),
            ("https://docs.latchway.dev/guide.md", True): response(
                "https://docs.latchway.dev/guide.md",
                guide_markdown,
                "text/markdown; charset=utf-8",
            ),
        }
        self.policy = MODULE.Policy(
            minimum_pages=2,
            minimum_ai_index_entries=2,
            concurrency=2,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def observe(self):
        return MODULE.observe(
            root=self.root,
            deployment=self.deployment,
            statuses=self.statuses,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_documentation_commit=COMMIT,
            workflow_event="workflow_dispatch",
            workflow_run_id=123456789,
            workflow_run_attempt=1,
            workflow_head_sha=COMMIT,
            fetcher=FakeFetcher(self.results),
            policy=self.policy,
            now=NOW,
        )

    def assert_rejected(self, fragment: str) -> None:
        with self.assertRaisesRegex(MODULE.EvidenceError, fragment):
            self.observe()

    def test_accepts_exact_production_deployment_and_live_site(self) -> None:
        evidence = self.observe()
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["deployment"]["id"], DEPLOYMENT_ID)
        self.assertEqual(evidence["source_checkpoint"]["documentation_commit"], COMMIT)
        self.assertEqual(evidence["postdeploy"]["pages"]["checked"], 2)
        self.assertEqual(evidence["postdeploy"]["redirects"]["checked"], 1)
        self.assertEqual(evidence["postdeploy"]["ai_outputs"]["outputs_checked"], 4)
        self.assertTrue(all(evidence["claims"].values()))
        MODULE.verify_evidence(evidence, COMMIT, DEPLOYMENT_ID)

    def test_rejects_nonproduction_or_transient_deployment(self) -> None:
        self.deployment["production_environment"] = False
        self.assert_rejected("non-transient production")
        self.deployment["production_environment"] = True
        self.deployment["transient_environment"] = True
        self.assert_rejected("non-transient production")

    def test_rejects_wrong_deployment_commit(self) -> None:
        self.deployment["sha"] = "f" * 40
        self.assert_rejected("expected docs commit")

    def test_rejects_untrusted_deployment_or_status_actor(self) -> None:
        self.deployment["creator"]["id"] = 1
        self.assert_rejected("trusted Mintlify app")
        self.deployment["creator"] = copy.deepcopy(ACTOR)
        self.statuses[0]["creator"]["login"] = "not-mintlify[bot]"
        self.assert_rejected("trusted Mintlify app")

    def test_rejects_stale_or_unsuccessful_status(self) -> None:
        self.deployment["created_at"] = "2026-08-29T23:00:00Z"
        self.statuses[0]["updated_at"] = "2026-08-30T00:00:00Z"
        self.assert_rejected("outside the evidence window")
        self.deployment["created_at"] = "2026-09-01T00:00:00Z"
        self.statuses[0]["updated_at"] = "2026-09-01T00:05:00Z"
        self.statuses[0]["state"] = "failure"
        self.assert_rejected("not production success")

    def test_rejects_live_source_title_or_description_drift(self) -> None:
        url = "https://docs.latchway.dev/guide"
        drifted = self.results[(url, True)].body.decode().replace(
            "<h1>Guide</h1>", "<h1>Different</h1>"
        )
        self.results[(url, True)] = response(url, drifted, "text/html")
        self.assert_rejected("title differs from source")

    def test_rejects_live_accessibility_baseline_failure(self) -> None:
        url = "https://docs.latchway.dev/"
        missing_alt = self.results[(url, True)].body.decode().replace(' alt=""', "")
        self.results[(url, True)] = response(url, missing_alt, "text/html")
        self.assert_rejected("image without alt text")

    def test_rejects_broken_internal_link(self) -> None:
        url = "https://docs.latchway.dev/"
        broken = self.results[(url, True)].body.decode().replace(
            'href="/guide"', 'href="/missing"'
        )
        self.results[(url, True)] = response(url, broken, "text/html")
        self.results[("https://docs.latchway.dev/missing", True)] = response(
            "https://docs.latchway.dev/missing", "not found", "text/html", status=404
        )
        self.assert_rejected("live internal link failed")

    def test_rejects_redirect_status_or_destination_drift(self) -> None:
        url = "https://docs.latchway.dev/old"
        self.results[(url, False)] = response(
            url, "", "text/html", status=307, headers={"location": "/guide"}
        )
        self.assert_rejected("permanent redirect has status")
        self.results[(url, False)] = response(
            url, "", "text/html", status=308, headers={"location": "/"}
        )
        self.assert_rejected("destination differs from source")

    def test_rejects_ai_index_or_markdown_drift(self) -> None:
        llms_url = "https://docs.latchway.dev/llms.txt"
        incomplete = "- [Overview](https://docs.latchway.dev/index.md): Home.\n"
        self.results[(llms_url, True)] = response(llms_url, incomplete, "text/plain")
        self.assert_rejected("omits source routes")
        self.results[(llms_url, True)] = response(
            llms_url, self.files["llms.txt"].decode(), "text/plain"
        )
        markdown_url = "https://docs.latchway.dev/guide.md"
        self.results[(markdown_url, True)] = response(
            markdown_url, "# Wrong\n", "text/markdown"
        )
        self.assert_rejected("differs from source metadata")

    def test_rejects_ai_output_redirect(self) -> None:
        markdown_url = "https://docs.latchway.dev/guide.md"
        original = self.results[(markdown_url, True)]
        self.results[(markdown_url, True)] = response(
            markdown_url,
            original.body.decode(),
            "text/markdown",
            final_url="https://docs.latchway.dev/index.md",
        )
        self.assert_rejected("Markdown output is unavailable")

    def test_offline_verifier_rejects_claim_or_digest_tampering(self) -> None:
        evidence = self.observe()
        tampered = copy.deepcopy(evidence)
        tampered["claims"]["live_redirects_verified"] = False
        with self.assertRaisesRegex(MODULE.EvidenceError, "unproven claim"):
            MODULE.verify_evidence(tampered, COMMIT, DEPLOYMENT_ID)
        tampered = copy.deepcopy(evidence)
        tampered["observations"]["pages"][0]["title"] = "Tampered"
        with self.assertRaisesRegex(MODULE.EvidenceError, "digest mismatch"):
            MODULE.verify_evidence(tampered, COMMIT, DEPLOYMENT_ID)
        tampered = copy.deepcopy(evidence)
        tampered["workflow"]["path"] = ".github/workflows/other.yml"
        with self.assertRaisesRegex(MODULE.EvidenceError, "workflow identity"):
            MODULE.verify_evidence(tampered, COMMIT, DEPLOYMENT_ID)

    def test_refuses_to_overwrite_evidence_output(self) -> None:
        path = self.root / "evidence.json"
        MODULE.write_new(path, b"first\n")
        with self.assertRaisesRegex(MODULE.EvidenceError, "refusing to overwrite"):
            MODULE.write_new(path, b"second\n")

    def test_schema_and_workflow_pin_the_production_authorities(self) -> None:
        repository = SCRIPT.parents[1]
        schema = json.loads(
            (repository / "schemas/mintlify-production-evidence.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["kind"]["const"], MODULE.KIND)
        self.assertEqual(
            schema["properties"]["repository"]["const"], MODULE.EXPECTED_REPOSITORY_URL
        )
        self.assertEqual(
            schema["$defs"]["deployment"]["properties"]["production_url"]["const"],
            MODULE.EXPECTED_PRODUCTION_ORIGIN,
        )
        self.assertEqual(
            schema["$defs"]["actor"]["properties"]["id"]["const"],
            MODULE.EXPECTED_MINTLIFY_ACTOR_ID,
        )
        self.assertEqual(
            schema["$defs"]["workflow"]["properties"]["path"]["const"],
            MODULE.EXPECTED_WORKFLOW_PATH,
        )
        workflow = (
            repository / ".github/workflows/mintlify-production-evidence.yml"
        ).read_text(encoding="utf-8")
        for fragment in (
            "deployment_status:",
            "workflow_dispatch:",
            "github.ref == 'refs/heads/main'",
            "github.event.deployment.creator.id == 109931778",
            "github.event.deployment.environment == 'production'",
            "github.event.deployment_status.state == 'success'",
            "environment: documentation-production-evidence",
            "deployments: read",
            "attestations: write",
            "id-token: write",
            "scripts/mintlify-production-evidence.py observe",
            "scripts/mintlify-production-evidence.py verify",
            "actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d",
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "persist-credentials: false",
            "latchway-mintlify-production-evidence.json",
            "latchway-mintlify-production-evidence.SHA256SUMS",
            "latchway-mintlify-production-evidence.attestation.sigstore.json",
            "latchway-mintlify-production-"
            "${{ steps.deployment.outputs.documentation_commit }}-"
            "${{ steps.deployment.outputs.deployment_id }}-"
            "${{ github.run_id }}-${{ github.run_attempt }}",
            "retention-days: 90",
        ):
            self.assertIn(fragment, workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
        mutable_action = re.compile(r"uses:\s+[^\s@]+@(?![0-9a-f]{40}(?:\s|$))")
        self.assertIsNone(mutable_action.search(workflow))


if __name__ == "__main__":
    unittest.main()
