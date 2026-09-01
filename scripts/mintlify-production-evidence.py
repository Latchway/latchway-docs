#!/usr/bin/env python3
"""Create or verify fail-closed evidence for a production Mintlify deployment.

The GitHub deployment record is the control-plane authority for the deployed
commit. The production site is then checked independently as the data plane.
Only public documentation is fetched; this command never accepts a credential
for the deployed site.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import ssl
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


SCHEMA_VERSION = 1
KIND = "latchway_mintlify_production_deployment_evidence"
EXPECTED_REPOSITORY = "Latchway/latchway-docs"
EXPECTED_REPOSITORY_URL = "https://github.com/Latchway/latchway-docs.git"
EXPECTED_WORKFLOW_PATH = ".github/workflows/mintlify-production-evidence.yml"
EXPECTED_BRANCH = "main"
EXPECTED_ENVIRONMENT = "production"
EXPECTED_PRODUCTION_ORIGIN = "https://docs.latchway.dev"
EXPECTED_MINTLIFY_ACTOR_ID = 109931778
EXPECTED_MINTLIFY_ACTOR_LOGIN = "mintlify[bot]"
EXPECTED_MINTLIFY_ACTOR_TYPE = "Bot"
ALLOWED_DEPLOYMENT_ORIGINS = {
    "https://docs.latchway.dev",
    "https://latchway.mintlify.app",
}
MAXIMUM_AGE_SECONDS = 86_400
MAXIMUM_CLOCK_SKEW_SECONDS = 300
MINIMUM_PRODUCTION_PAGES = 100
MINIMUM_AI_INDEX_ENTRIES = 20
MAXIMUM_JSON_BYTES = 4 * 1024 * 1024
MAXIMUM_EVIDENCE_BYTES = 32 * 1024 * 1024
MAXIMUM_HTML_BYTES = 4 * 1024 * 1024
MAXIMUM_MARKDOWN_BYTES = 4 * 1024 * 1024
MAXIMUM_LLMS_FULL_BYTES = 32 * 1024 * 1024
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LLMS_LINK_RE = re.compile(
    r"^- \[(?P<title>[^\]]+)\]\((?P<url>https://[^\s)]+\.md)\): "
    r"(?P<description>.+)$",
    re.MULTILINE,
)
CLAIM_KEYS = {
    "documentation_commit_verified",
    "github_deployment_success_verified",
    "live_accessibility_baseline_verified",
    "live_ai_outputs_verified",
    "live_internal_links_verified",
    "live_redirects_verified",
    "live_source_checkpoint_verified",
    "mintlify_actor_verified",
    "production_environment_verified",
}
ACCESSIBILITY_RULES = [
    "document-language-en",
    "single-main-landmark",
    "single-source-matched-h1",
    "nonempty-document-title",
    "source-matched-meta-description",
    "image-alt-attribute",
]
FORBIDDEN_REPOSITORY_PARTS = {".git", "__pycache__", "node_modules"}


class EvidenceError(ValueError):
    """A production-evidence invariant failed."""


def rejecting_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def compact_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def result_set_sha256(value: Any) -> str:
    return sha256_bytes(compact_json(value))


def read_regular_bytes(path: Path, maximum: int = MAXIMUM_JSON_BYTES) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise EvidenceError(f"required file is missing: {path}") from error
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"required path is not a regular file: {path}")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        raise EvidenceError(f"required file has an invalid size: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise EvidenceError(f"cannot read {path}: {error}") from error


def read_json(path: Path, maximum: int = MAXIMUM_JSON_BYTES) -> Any:
    payload = read_regular_bytes(path, maximum=maximum)
    try:
        return json.loads(payload, object_pairs_hook=rejecting_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"invalid JSON in {path}: {error}") from error


def require_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise EvidenceError(f"{label} has an invalid field set")
    return value


def require_positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise EvidenceError(f"{label} must be a positive integer")
    return value


def require_hex(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise EvidenceError(f"{label} has an invalid digest or commit")
    return value


def parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or DATE_TIME_RE.fullmatch(value) is None:
        raise EvidenceError(f"{label} must be canonical UTC seconds")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise EvidenceError(f"{label} is not a valid timestamp") from error
    return parsed


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def normalized_text(value: str) -> str:
    return " ".join(value.split())


def canonical_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise EvidenceError(f"{label} must be an absolute URL path")
    decoded = unquote(value)
    if "\\" in decoded or any(ord(character) < 32 for character in decoded):
        raise EvidenceError(f"{label} is not canonical")
    pure = PurePosixPath(decoded)
    if any(part in {"", ".", ".."} for part in pure.parts[1:]):
        raise EvidenceError(f"{label} is not canonical")
    normalized = pure.as_posix()
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def canonical_origin(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise EvidenceError(f"{label} must be a credential-free HTTPS origin")
    port = parsed.port
    if port not in {None, 443}:
        raise EvidenceError(f"{label} must use the default HTTPS port")
    return f"https://{parsed.hostname.lower()}"


def canonical_url(value: str, allowed_hosts: set[str], label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise EvidenceError(f"{label} is not an allowed HTTPS URL")
    path = canonical_path(parsed.path or "/", label)
    return urlunsplit(("https", parsed.hostname.lower(), path, "", ""))


@dataclass(frozen=True)
class Policy:
    production_origin: str = EXPECTED_PRODUCTION_ORIGIN
    minimum_pages: int = MINIMUM_PRODUCTION_PAGES
    minimum_ai_index_entries: int = MINIMUM_AI_INDEX_ENTRIES
    maximum_age_seconds: int = MAXIMUM_AGE_SECONDS
    concurrency: int = 8


@dataclass(frozen=True)
class ExpectedPage:
    path: str
    source_path: str
    title: str
    description: str
    source_sha256: str


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes


class Fetcher(Protocol):
    def fetch(
        self, url: str, *, follow_redirects: bool = True, maximum: int
    ) -> FetchResult: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


class _SameOriginRedirect(HTTPRedirectHandler):
    def __init__(self, expected_origin: str) -> None:
        super().__init__()
        self.expected_origin = expected_origin

    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Any:
        parsed = urlsplit(new_url)
        redirect_origin = canonical_origin(
            f"{parsed.scheme}://{parsed.netloc}", "HTTP redirect origin"
        )
        if redirect_origin != self.expected_origin:
            raise EvidenceError("HTTP redirect left the production origin")
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


class HTTPSFetcher:
    def __init__(self, attempts: int = 3, timeout_seconds: int = 20) -> None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.following = build_opener(
            HTTPSHandler(context=context),
            _SameOriginRedirect(EXPECTED_PRODUCTION_ORIGIN),
        )
        self.not_following = build_opener(
            HTTPSHandler(context=context), _NoRedirect()
        )
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds

    def fetch(
        self, url: str, *, follow_redirects: bool = True, maximum: int
    ) -> FetchResult:
        request = Request(
            url,
            headers={
                "Accept": "text/html,text/markdown,text/plain;q=0.9,*/*;q=0.1",
                "User-Agent": "LatchwayDocsProductionEvidence/1",
            },
            method="GET",
        )
        opener = self.following if follow_redirects else self.not_following
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            try:
                try:
                    response = opener.open(request, timeout=self.timeout_seconds)
                except HTTPError as error:
                    response = error
                with response:
                    body = response.read(maximum + 1)
                    if len(body) > maximum:
                        raise EvidenceError(f"response exceeds size limit: {url}")
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    return FetchResult(
                        url=url,
                        final_url=response.geturl(),
                        status=int(response.status),
                        headers=headers,
                        body=body,
                    )
            except (OSError, URLError) as error:
                last_error = error
                if attempt + 1 < self.attempts:
                    time.sleep(0.25 * (attempt + 1))
        raise EvidenceError(f"cannot fetch {url}: {last_error}")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_lang: str | None = None
        self.main_count = 0
        self.h1_count = 0
        self.image_count = 0
        self.missing_alt_count = 0
        self.links: list[str] = []
        self.meta_descriptions: list[str] = []
        self._h1_depth = 0
        self._title_depth = 0
        self._h1_parts: list[str] = []
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        tag = tag.lower()
        if tag == "html" and self.html_lang is None:
            self.html_lang = values.get("lang")
        elif tag == "main":
            self.main_count += 1
        elif tag == "h1":
            self.h1_count += 1
            self._h1_depth += 1
        elif tag == "title":
            self._title_depth += 1
        elif tag == "img":
            self.image_count += 1
            if "alt" not in values:
                self.missing_alt_count += 1
        elif tag == "a" and values.get("href") is not None:
            self.links.append(values["href"] or "")
        elif (
            tag == "meta"
            and (values.get("name") or "").lower() == "description"
            and values.get("content") is not None
        ):
            self.meta_descriptions.append(values["content"] or "")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "h1" and self._h1_depth:
            self._h1_depth -= 1
        elif tag == "title" and self._title_depth:
            self._title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._h1_depth:
            self._h1_parts.append(data)
        if self._title_depth:
            self._title_parts.append(data)

    @property
    def h1_text(self) -> str:
        return normalized_text("".join(self._h1_parts))

    @property
    def title_text(self) -> str:
        return normalized_text("".join(self._title_parts))


def read_frontmatter(path: Path) -> tuple[str, str]:
    try:
        text = read_regular_bytes(path, maximum=8 * 1024 * 1024).decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError(f"public page is not UTF-8: {path}") from error
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise EvidenceError(f"public page has no frontmatter: {path}")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise EvidenceError(f"public page frontmatter is unterminated: {path}") from error
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*):\s*(.+)", line)
        if not match:
            continue
        key, raw = match.groups()
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            try:
                value = json.loads(value) if value[0] == '"' else value[1:-1]
            except json.JSONDecodeError as error:
                raise EvidenceError(f"invalid quoted frontmatter in {path}") from error
        fields[key] = value
    title = normalized_text(fields.get("title", ""))
    description = normalized_text(fields.get("description", ""))
    if not title or not description:
        raise EvidenceError(f"public page lacks title or description: {path}")
    return title, description


def validate_source_checkpoint(
    root: Path, expected_documentation_commit: str
) -> tuple[dict[str, Any], dict[str, ExpectedPage], str]:
    require_hex(expected_documentation_commit, COMMIT_RE, "expected documentation commit")
    manifest_path = root / ".latchway-docs-source.json"
    manifest_bytes = read_regular_bytes(manifest_path)
    try:
        manifest = json.loads(manifest_bytes, object_pairs_hook=rejecting_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"invalid source manifest: {error}") from error
    manifest = require_keys(
        manifest,
        {"files", "format", "source", "source_commit", "source_tree_sha256"},
        "source manifest",
    )
    if manifest["format"] != 1 or manifest["source"] != "latchway/docs/public":
        raise EvidenceError("source manifest identity is invalid")
    core_commit = require_hex(manifest["source_commit"], COMMIT_RE, "canonical core commit")
    source_tree = require_hex(
        manifest["source_tree_sha256"], SHA256_RE, "source tree SHA-256"
    )
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise EvidenceError("source manifest files are invalid")
    actual_files: dict[str, str] = {}
    pages: dict[str, ExpectedPage] = {}
    for relative, digest in sorted(files.items()):
        if not isinstance(relative, str) or not relative:
            raise EvidenceError("source manifest contains an invalid path")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} | FORBIDDEN_REPOSITORY_PARTS for part in pure.parts)
        ):
            raise EvidenceError(f"source manifest path is unsafe: {relative}")
        expected_sha = require_hex(digest, SHA256_RE, f"source digest for {relative}")
        candidate = root.joinpath(*pure.parts)
        payload = read_regular_bytes(candidate, maximum=32 * 1024 * 1024)
        actual_sha = sha256_bytes(payload)
        if actual_sha != expected_sha:
            raise EvidenceError(f"source checkpoint differs: {relative}")
        actual_files[relative] = actual_sha
        if candidate.suffix == ".mdx":
            route = "/" if relative == "index.mdx" else "/" + relative[:-4]
            route = canonical_path(route, f"route for {relative}")
            if route in pages:
                raise EvidenceError(f"duplicate public route: {route}")
            title, description = read_frontmatter(candidate)
            pages[route] = ExpectedPage(
                path=route,
                source_path=relative,
                title=title,
                description=description,
                source_sha256=actual_sha,
            )
    computed_tree = sha256_bytes(compact_json(actual_files))
    if computed_tree != source_tree:
        raise EvidenceError("source tree SHA-256 is not reproducible")
    return (
        {
            "canonical_core_commit": core_commit,
            "documentation_commit": expected_documentation_commit,
            "owned_file_count": len(files),
            "source_manifest_sha256": sha256_bytes(manifest_bytes),
            "source_tree_sha256": source_tree,
        },
        pages,
        sha256_bytes(read_regular_bytes(root / "llms.txt", maximum=MAXIMUM_MARKDOWN_BYTES)),
    )


def validate_actor(actor: Any, label: str) -> dict[str, Any]:
    if not isinstance(actor, dict):
        raise EvidenceError(f"{label} actor is missing")
    if (
        actor.get("id") != EXPECTED_MINTLIFY_ACTOR_ID
        or actor.get("login") != EXPECTED_MINTLIFY_ACTOR_LOGIN
        or actor.get("type") != EXPECTED_MINTLIFY_ACTOR_TYPE
    ):
        raise EvidenceError(f"{label} was not created by the trusted Mintlify app")
    return {
        "id": EXPECTED_MINTLIFY_ACTOR_ID,
        "login": EXPECTED_MINTLIFY_ACTOR_LOGIN,
        "type": EXPECTED_MINTLIFY_ACTOR_TYPE,
    }


def validate_workflow_identity(
    *,
    event: str,
    run_id: int,
    run_attempt: int,
    head_sha: str,
) -> dict[str, Any]:
    if event not in {"deployment_status", "workflow_dispatch"}:
        raise EvidenceError("production evidence workflow event is not allowed")
    require_positive_integer(run_id, "workflow run ID")
    require_positive_integer(run_attempt, "workflow run attempt")
    require_hex(head_sha, COMMIT_RE, "workflow head SHA")
    return {
        "event": event,
        "expected_conclusion": "success",
        "head_sha": head_sha,
        "path": EXPECTED_WORKFLOW_PATH,
        "ref": "refs/heads/main",
        "repository": EXPECTED_REPOSITORY,
        "run_attempt": run_attempt,
        "run_id": run_id,
        "run_url": f"https://github.com/{EXPECTED_REPOSITORY}/actions/runs/{run_id}",
    }


def validate_deployment(
    deployment: Any,
    statuses: Any,
    *,
    expected_deployment_id: int,
    expected_documentation_commit: str,
    policy: Policy,
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(deployment, dict):
        raise EvidenceError("GitHub deployment response is invalid")
    if deployment.get("id") != expected_deployment_id:
        raise EvidenceError("GitHub deployment ID mismatch")
    if deployment.get("sha") != expected_documentation_commit:
        raise EvidenceError("GitHub deployment does not target the expected docs commit")
    if deployment.get("ref") != EXPECTED_BRANCH or deployment.get("task") != "deploy":
        raise EvidenceError("GitHub deployment does not target the production branch")
    if (
        deployment.get("environment") != EXPECTED_ENVIRONMENT
        or deployment.get("production_environment") is not True
        or deployment.get("transient_environment") is not False
    ):
        raise EvidenceError("GitHub deployment is not a non-transient production deployment")
    actor = validate_actor(deployment.get("creator"), "deployment")
    created_at = parse_time(deployment.get("created_at"), "deployment created_at")
    if not isinstance(statuses, list) or not statuses:
        raise EvidenceError("GitHub deployment has no status records")
    status = statuses[0]
    if not isinstance(status, dict):
        raise EvidenceError("GitHub deployment status is invalid")
    if status.get("state") != "success" or status.get("environment") != EXPECTED_ENVIRONMENT:
        raise EvidenceError("latest GitHub deployment status is not production success")
    status_actor = validate_actor(status.get("creator"), "deployment status")
    if status_actor != actor:
        raise EvidenceError("GitHub deployment actor changed across records")
    updated_at = parse_time(status.get("updated_at"), "deployment status updated_at")
    if updated_at < created_at:
        raise EvidenceError("GitHub deployment status predates the deployment")
    age = (now.astimezone(timezone.utc) - updated_at).total_seconds()
    if age < -MAXIMUM_CLOCK_SKEW_SECONDS or age > policy.maximum_age_seconds:
        raise EvidenceError("GitHub deployment status is outside the evidence window")
    environment_url_value = status.get("environment_url")
    if not isinstance(environment_url_value, str):
        raise EvidenceError("GitHub deployment status has no environment URL")
    production_origin = canonical_origin(policy.production_origin, "production origin")
    environment_url = canonical_origin(
        environment_url_value, "GitHub deployment environment URL"
    )
    if environment_url not in ALLOWED_DEPLOYMENT_ORIGINS:
        raise EvidenceError("GitHub deployment environment URL is not trusted")
    return {
        "actor": actor,
        "created_at": format_time(created_at),
        "environment": EXPECTED_ENVIRONMENT,
        "environment_url": environment_url,
        "id": expected_deployment_id,
        "production_environment": True,
        "production_url": production_origin,
        "state": "success",
        "status_id": require_positive_integer(status.get("id"), "deployment status ID"),
        "transient_environment": False,
        "updated_at": format_time(updated_at),
    }


def content_type(result: FetchResult) -> str:
    return result.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def live_url(origin: str, path: str) -> str:
    return origin + ("/" if path == "/" else path)


def same_route(url: str, expected_origin: str, expected_path: str) -> bool:
    parsed = urlsplit(url)
    return (
        canonical_origin(f"{parsed.scheme}://{parsed.netloc}", "response origin")
        == expected_origin
        and canonical_path(parsed.path or "/", "response path") == expected_path
        and not parsed.query
        and not parsed.fragment
    )


def internal_link_path(href: str, page_url: str, production_origin: str) -> str | None:
    if not href or href.startswith("#"):
        return canonical_path(urlsplit(page_url).path or "/", "fragment link")
    parsed = urlsplit(urljoin(page_url, href))
    if parsed.scheme in {"mailto", "tel"}:
        return None
    if parsed.scheme not in {"http", "https"}:
        raise EvidenceError(f"live page contains an unsafe link scheme: {href}")
    origin = canonical_origin(f"{parsed.scheme}://{parsed.netloc}", "link origin")
    if origin != production_origin:
        return None
    return canonical_path(parsed.path or "/", "internal link")


def validate_html_page(
    fetcher: Fetcher,
    origin: str,
    page: ExpectedPage,
) -> tuple[dict[str, Any], list[str]]:
    url = live_url(origin, page.path)
    result = fetcher.fetch(url, maximum=MAXIMUM_HTML_BYTES)
    if result.status != 200 or not same_route(result.final_url, origin, page.path):
        raise EvidenceError(f"live page did not resolve exactly: {page.path}")
    if content_type(result) != "text/html":
        raise EvidenceError(f"live page is not HTML: {page.path}")
    try:
        text = result.body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError(f"live page is not UTF-8: {page.path}") from error
    parser = PageParser()
    parser.feed(text)
    parser.close()
    if parser.html_lang != "en":
        raise EvidenceError(f"live page lacks the English document language: {page.path}")
    if parser.main_count != 1 or parser.h1_count != 1 or not parser.title_text:
        raise EvidenceError(f"live page fails the landmark or heading baseline: {page.path}")
    if normalized_text(parser.h1_text) != page.title:
        raise EvidenceError(f"live page title differs from source: {page.path}")
    if page.description not in {normalized_text(item) for item in parser.meta_descriptions}:
        raise EvidenceError(f"live page description differs from source: {page.path}")
    if parser.missing_alt_count:
        raise EvidenceError(f"live page has an image without alt text: {page.path}")
    links = sorted(
        {
            linked
            for href in parser.links
            if (linked := internal_link_path(href, url, origin)) is not None
        }
    )
    observation = {
        "body_sha256": sha256_bytes(result.body),
        "bytes": len(result.body),
        "content_type": content_type(result),
        "description": page.description,
        "final_url": result.final_url,
        "h1_count": parser.h1_count,
        "image_count": parser.image_count,
        "internal_link_count": len(links),
        "lang": parser.html_lang,
        "main_count": parser.main_count,
        "missing_alt_count": parser.missing_alt_count,
        "path": page.path,
        "source_path": page.source_path,
        "source_sha256": page.source_sha256,
        "status": result.status,
        "title": page.title,
        "url": url,
    }
    return observation, links


def run_parallel_pages(
    fetcher: Fetcher,
    origin: str,
    pages: Mapping[str, ExpectedPage],
    concurrency: int,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    observations: list[dict[str, Any]] = []
    relationships: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(validate_html_page, fetcher, origin, page): path
            for path, page in pages.items()
        }
        for future in as_completed(futures):
            source = futures[future]
            observation, links = future.result()
            observations.append(observation)
            relationships.extend((source, target) for target in links)
    observations.sort(key=lambda item: item["path"])
    relationships.sort()
    return observations, relationships


def validate_link_target(fetcher: Fetcher, origin: str, path: str) -> dict[str, Any]:
    url = live_url(origin, path)
    maximum = MAXIMUM_MARKDOWN_BYTES if path.endswith(".md") else MAXIMUM_HTML_BYTES
    result = fetcher.fetch(url, maximum=maximum)
    if result.status < 200 or result.status >= 300:
        raise EvidenceError(f"live internal link failed: {path}")
    final = urlsplit(result.final_url)
    final_origin = canonical_origin(f"{final.scheme}://{final.netloc}", "link response")
    if final_origin != origin:
        raise EvidenceError(f"live internal link left the production origin: {path}")
    return {
        "body_sha256": sha256_bytes(result.body),
        "bytes": len(result.body),
        "content_type": content_type(result),
        "final_url": result.final_url,
        "path": path,
        "status": result.status,
        "url": url,
    }


def validate_links(
    fetcher: Fetcher,
    origin: str,
    pages: Mapping[str, ExpectedPage],
    page_observations: Iterable[Mapping[str, Any]],
    relationships: list[tuple[str, str]],
    redirect_sources: set[str],
    concurrency: int,
) -> list[dict[str, Any]]:
    targets = sorted({target for _, target in relationships})
    if any(target in redirect_sources for target in targets):
        raise EvidenceError("live HTML links to a legacy redirect instead of its canonical page")
    to_fetch = [target for target in targets if target not in pages]
    page_results = {item["path"]: item for item in page_observations}
    observations: list[dict[str, Any]] = []
    for target in targets:
        if target not in pages:
            continue
        item = page_results.get(target)
        if item is None:
            raise EvidenceError(f"linked canonical page lacks a live observation: {target}")
        observations.append(
            {
                "body_sha256": item["body_sha256"],
                "bytes": item["bytes"],
                "content_type": item["content_type"],
                "final_url": item["final_url"],
                "path": target,
                "status": item["status"],
                "url": item["url"],
            }
        )
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(validate_link_target, fetcher, origin, target): target
            for target in to_fetch
        }
        for future in as_completed(futures):
            observations.append(future.result())
    observations.sort(key=lambda item: item["path"])
    return observations


def load_redirects(root: Path) -> list[dict[str, Any]]:
    redirects = read_json(root / "config" / "redirects.json")
    if not isinstance(redirects, list) or not redirects:
        raise EvidenceError("redirect registry is empty or invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in redirects:
        item = require_keys(item, {"source", "destination", "permanent"}, "redirect")
        source = canonical_path(item["source"], "redirect source")
        destination = canonical_path(item["destination"], "redirect destination")
        if item["permanent"] is not True or source in seen or source == destination:
            raise EvidenceError(f"redirect registry entry is invalid: {source}")
        seen.add(source)
        result.append({"source": source, "destination": destination})
    return sorted(result, key=lambda item: item["source"])


def validate_redirect(
    fetcher: Fetcher, origin: str, redirect: Mapping[str, str]
) -> dict[str, Any]:
    source = redirect["source"]
    destination = redirect["destination"]
    url = live_url(origin, source)
    result = fetcher.fetch(url, follow_redirects=False, maximum=MAXIMUM_HTML_BYTES)
    if result.status not in {301, 308}:
        raise EvidenceError(f"live permanent redirect has status {result.status}: {source}")
    location = result.headers.get("location")
    if not location:
        raise EvidenceError(f"live redirect has no Location header: {source}")
    resolved = urlsplit(urljoin(url, location))
    resolved_origin = canonical_origin(
        f"{resolved.scheme}://{resolved.netloc}", "redirect destination origin"
    )
    resolved_path = canonical_path(resolved.path or "/", "redirect Location")
    if (
        resolved_origin != origin
        or resolved_path != destination
        or resolved.query
        or resolved.fragment
    ):
        raise EvidenceError(f"live redirect destination differs from source: {source}")
    return {
        "destination": destination,
        "location": location,
        "source": source,
        "status": result.status,
        "url": url,
    }


def validate_redirects(
    fetcher: Fetcher,
    origin: str,
    redirects: list[dict[str, Any]],
    concurrency: int,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(validate_redirect, fetcher, origin, redirect)
            for redirect in redirects
        ]
        for future in as_completed(futures):
            observations.append(future.result())
    return sorted(observations, key=lambda item: item["source"])


def parse_llms_index(text: str, label: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for match in LLMS_LINK_RE.finditer(text):
        parsed = urlsplit(match.group("url"))
        path = canonical_path(parsed.path, f"{label} link")
        if path in entries:
            raise EvidenceError(f"{label} contains a duplicate AI route: {path}")
        entries[path] = (
            normalized_text(match.group("title")),
            normalized_text(match.group("description")),
        )
    if not entries:
        raise EvidenceError(f"{label} has no canonical Markdown links")
    return entries


def ai_observation(kind: str, path: str, result: FetchResult, title: str | None) -> dict[str, Any]:
    return {
        "body_sha256": sha256_bytes(result.body),
        "bytes": len(result.body),
        "content_type": content_type(result),
        "final_url": result.final_url,
        "kind": kind,
        "path": path,
        "status": result.status,
        "title": title,
        "url": result.url,
    }


def validate_ai_outputs(
    fetcher: Fetcher,
    root: Path,
    origin: str,
    pages: Mapping[str, ExpectedPage],
    expected_llms_sha256: str,
    minimum_entries: int,
    concurrency: int,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
    local_llms_bytes = read_regular_bytes(root / "llms.txt", maximum=MAXIMUM_MARKDOWN_BYTES)
    if sha256_bytes(local_llms_bytes) != expected_llms_sha256:
        raise EvidenceError("local llms.txt changed during observation")
    try:
        local_llms = local_llms_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError("local llms.txt is not UTF-8") from error
    expected_entries = parse_llms_index(local_llms, "source llms.txt")
    if len(expected_entries) < minimum_entries:
        raise EvidenceError("source llms.txt does not cover enough public tasks")
    llms_result = fetcher.fetch(
        live_url(origin, "/llms.txt"), maximum=MAXIMUM_MARKDOWN_BYTES
    )
    if (
        llms_result.status != 200
        or content_type(llms_result) != "text/plain"
        or not same_route(llms_result.final_url, origin, "/llms.txt")
    ):
        raise EvidenceError("live llms.txt is unavailable or has the wrong content type")
    try:
        live_llms = llms_result.body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError("live llms.txt is not UTF-8") from error
    live_entries = parse_llms_index(live_llms, "live llms.txt")
    missing = set(expected_entries) - set(live_entries)
    if missing:
        raise EvidenceError(f"live llms.txt omits source routes: {sorted(missing)}")
    for path, (index_title, index_description) in expected_entries.items():
        page_path = path[:-3]
        page_route = "/" if page_path == "/index" else page_path
        page = pages.get(page_route)
        if page is None:
            raise EvidenceError(f"source llms.txt references no public source page: {path}")
        live_title, live_description = live_entries[path]
        if (live_title, live_description) not in {
            (index_title, index_description),
            (page.title, page.description),
        }:
            raise EvidenceError(f"live llms.txt metadata differs from source: {path}")
    llms_full_result = fetcher.fetch(
        live_url(origin, "/llms-full.txt"), maximum=MAXIMUM_LLMS_FULL_BYTES
    )
    if (
        llms_full_result.status != 200
        or content_type(llms_full_result) != "text/plain"
        or not same_route(llms_full_result.final_url, origin, "/llms-full.txt")
    ):
        raise EvidenceError("live llms-full.txt is unavailable or has the wrong content type")
    try:
        llms_full = llms_full_result.body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError("live llms-full.txt is not UTF-8") from error
    if len(llms_full_result.body) < 512:
        raise EvidenceError("live llms-full.txt is unexpectedly small")
    normalized_llms_full = normalized_text(llms_full)

    def validate_markdown(path: str) -> dict[str, Any]:
        page_path = path[:-3]
        page_route = "/" if page_path == "/index" else page_path
        page = pages.get(page_route)
        if page is None:
            raise EvidenceError(f"source llms.txt references no public source page: {path}")
        result = fetcher.fetch(live_url(origin, path), maximum=MAXIMUM_MARKDOWN_BYTES)
        if (
            result.status != 200
            or content_type(result) != "text/markdown"
            or not same_route(result.final_url, origin, path)
        ):
            raise EvidenceError(f"live Markdown output is unavailable: {path}")
        try:
            text = result.body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EvidenceError(f"live Markdown output is not UTF-8: {path}") from error
        normalized = normalized_text(text)
        if f"# {page.title}" not in text or page.description not in normalized:
            raise EvidenceError(f"live Markdown output differs from source metadata: {path}")
        if f"# {page.title}" not in llms_full or page.description not in normalized_llms_full:
            raise EvidenceError(f"llms-full.txt omits source content: {path}")
        return ai_observation("markdown_page", path, result, page.title)

    observations: list[dict[str, Any]] = [
        ai_observation("llms_txt", "/llms.txt", llms_result, None),
        ai_observation("llms_full_txt", "/llms-full.txt", llms_full_result, None),
    ]
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(validate_markdown, path): path
            for path in sorted(expected_entries)
        }
        for future in as_completed(futures):
            observations.append(future.result())
    observations.sort(key=lambda item: (item["kind"], item["path"]))
    return observations, expected_entries


def current_git_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EvidenceError(f"cannot resolve documentation commit: {error}") from error
    commit = result.stdout.strip()
    if result.returncode != 0 or COMMIT_RE.fullmatch(commit) is None:
        raise EvidenceError("cannot resolve an exact documentation commit")
    return commit


def observe(
    *,
    root: Path,
    deployment: Any,
    statuses: Any,
    expected_deployment_id: int,
    expected_documentation_commit: str,
    workflow_event: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    workflow_head_sha: str,
    fetcher: Fetcher,
    policy: Policy = Policy(),
    now: datetime | None = None,
) -> dict[str, Any]:
    started = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        microsecond=0
    )
    origin = canonical_origin(policy.production_origin, "production origin")
    if origin != EXPECTED_PRODUCTION_ORIGIN:
        raise EvidenceError("production origin is not the canonical Latchway docs origin")
    checkpoint, pages, expected_llms_sha256 = validate_source_checkpoint(
        root, expected_documentation_commit
    )
    if len(pages) < policy.minimum_pages:
        raise EvidenceError("source checkpoint does not contain the required public page set")
    deployment_record = validate_deployment(
        deployment,
        statuses,
        expected_deployment_id=expected_deployment_id,
        expected_documentation_commit=expected_documentation_commit,
        policy=policy,
        now=started,
    )
    workflow = validate_workflow_identity(
        event=workflow_event,
        run_id=workflow_run_id,
        run_attempt=workflow_run_attempt,
        head_sha=workflow_head_sha,
    )
    redirects = load_redirects(root)
    page_observations, relationships = run_parallel_pages(
        fetcher, origin, pages, policy.concurrency
    )
    link_observations = validate_links(
        fetcher,
        origin,
        pages,
        page_observations,
        relationships,
        {item["source"] for item in redirects},
        policy.concurrency,
    )
    redirect_observations = validate_redirects(
        fetcher, origin, redirects, policy.concurrency
    )
    ai_observations, ai_entries = validate_ai_outputs(
        fetcher,
        root,
        origin,
        pages,
        expected_llms_sha256,
        policy.minimum_ai_index_entries,
        policy.concurrency,
    )
    finished = datetime.now(timezone.utc).replace(microsecond=0)
    if now is not None:
        finished = started
    observations = {
        "ai_outputs": ai_observations,
        "link_relationships": [
            {"source": source, "target": target} for source, target in relationships
        ],
        "link_targets": link_observations,
        "pages": page_observations,
        "redirects": redirect_observations,
    }
    page_digest = result_set_sha256(page_observations)
    relationships_digest = result_set_sha256(relationships)
    document = {
        "claims": {key: True for key in sorted(CLAIM_KEYS)},
        "deployment": deployment_record,
        "finished_at": format_time(finished),
        "kind": KIND,
        "maximum_age_seconds": policy.maximum_age_seconds,
        "observations": observations,
        "postdeploy": {
            "accessibility": {
                "pages_checked": len(page_observations),
                "results_sha256": page_digest,
                "rules": ACCESSIBILITY_RULES,
            },
            "ai_outputs": {
                "index_entries_checked": len(ai_entries),
                "outputs_checked": len(ai_observations),
                "results_sha256": result_set_sha256(ai_observations),
                "source_llms_txt_sha256": expected_llms_sha256,
            },
            "links": {
                "relationships_checked": len(relationships),
                "relationships_sha256": relationships_digest,
                "results_sha256": result_set_sha256(link_observations),
                "targets_checked": len(link_observations),
            },
            "pages": {
                "checked": len(page_observations),
                "results_sha256": page_digest,
            },
            "redirects": {
                "checked": len(redirect_observations),
                "results_sha256": result_set_sha256(redirect_observations),
            },
        },
        "repository": EXPECTED_REPOSITORY_URL,
        "schema_version": SCHEMA_VERSION,
        "source_checkpoint": checkpoint,
        "started_at": format_time(started),
        "status": "passed",
        "workflow": workflow,
    }
    verify_evidence(document, expected_documentation_commit, expected_deployment_id)
    return document


def verify_observation_hashes(document: Mapping[str, Any]) -> None:
    observations = require_keys(
        document["observations"],
        {"ai_outputs", "link_relationships", "link_targets", "pages", "redirects"},
        "observations",
    )
    for key, value in observations.items():
        if not isinstance(value, list) or not value:
            raise EvidenceError(f"evidence observation set is empty: {key}")
    postdeploy = require_keys(
        document["postdeploy"],
        {"accessibility", "ai_outputs", "links", "pages", "redirects"},
        "postdeploy summary",
    )
    pages = observations["pages"]
    links = observations["link_targets"]
    relationships = observations["link_relationships"]
    redirects = observations["redirects"]
    ai_outputs = observations["ai_outputs"]
    page_summary = require_keys(
        postdeploy["pages"], {"checked", "results_sha256"}, "page summary"
    )
    accessibility_summary = require_keys(
        postdeploy["accessibility"],
        {"pages_checked", "results_sha256", "rules"},
        "accessibility summary",
    )
    link_summary = require_keys(
        postdeploy["links"],
        {
            "relationships_checked",
            "relationships_sha256",
            "results_sha256",
            "targets_checked",
        },
        "link summary",
    )
    redirect_summary = require_keys(
        postdeploy["redirects"], {"checked", "results_sha256"}, "redirect summary"
    )
    ai_summary = require_keys(
        postdeploy["ai_outputs"],
        {
            "index_entries_checked",
            "outputs_checked",
            "results_sha256",
            "source_llms_txt_sha256",
        },
        "AI-output summary",
    )
    page_fields = {
        "body_sha256",
        "bytes",
        "content_type",
        "description",
        "final_url",
        "h1_count",
        "image_count",
        "internal_link_count",
        "lang",
        "main_count",
        "missing_alt_count",
        "path",
        "source_path",
        "source_sha256",
        "status",
        "title",
        "url",
    }
    link_fields = {
        "body_sha256",
        "bytes",
        "content_type",
        "final_url",
        "path",
        "status",
        "url",
    }
    for index, item in enumerate(pages):
        item = require_keys(item, page_fields, f"page observation {index}")
        if (
            item["status"] != 200
            or item["content_type"] != "text/html"
            or item["lang"] != "en"
            or item["main_count"] != 1
            or item["h1_count"] != 1
            or item["missing_alt_count"] != 0
            or type(item["bytes"]) is not int
            or item["bytes"] <= 0
            or type(item["image_count"]) is not int
            or item["image_count"] < 0
            or type(item["internal_link_count"]) is not int
            or item["internal_link_count"] < 0
            or not isinstance(item["title"], str)
            or not item["title"]
            or not isinstance(item["description"], str)
            or not item["description"]
            or not isinstance(item["source_path"], str)
            or not item["source_path"].endswith(".mdx")
        ):
            raise EvidenceError("page observation is not a passing source-matched page")
        require_hex(item["body_sha256"], SHA256_RE, "page body SHA-256")
        require_hex(item["source_sha256"], SHA256_RE, "page source SHA-256")
        path = canonical_path(item["path"], "page observation path")
        if item["url"] != live_url(EXPECTED_PRODUCTION_ORIGIN, path) or not same_route(
            item["final_url"], EXPECTED_PRODUCTION_ORIGIN, path
        ):
            raise EvidenceError("page observation URL is not canonical production")
    if [item["path"] for item in pages] != sorted({item["path"] for item in pages}):
        raise EvidenceError("page observations are not a unique canonical sequence")
    for index, item in enumerate(relationships):
        item = require_keys(item, {"source", "target"}, f"link relationship {index}")
        canonical_path(item["source"], "link relationship source")
        canonical_path(item["target"], "link relationship target")
    relationship_pairs = [(item["source"], item["target"]) for item in relationships]
    if relationship_pairs != sorted(set(relationship_pairs)):
        raise EvidenceError("link relationships are not a unique canonical sequence")
    for index, item in enumerate(links):
        item = require_keys(item, link_fields, f"link observation {index}")
        if (
            type(item["status"]) is not int
            or not 200 <= item["status"] < 300
            or type(item["bytes"]) is not int
            or item["bytes"] <= 0
            or not isinstance(item["content_type"], str)
            or not item["content_type"]
        ):
            raise EvidenceError("link observation is not a successful response")
        require_hex(item["body_sha256"], SHA256_RE, "link body SHA-256")
        path = canonical_path(item["path"], "link observation path")
        if item["url"] != live_url(EXPECTED_PRODUCTION_ORIGIN, path):
            raise EvidenceError("link observation URL is not canonical production")
        final = urlsplit(item["final_url"])
        if (
            canonical_origin(
                f"{final.scheme}://{final.netloc}", "link final origin"
            )
            != EXPECTED_PRODUCTION_ORIGIN
        ):
            raise EvidenceError("link observation left the production origin")
    if [item["path"] for item in links] != sorted({item["path"] for item in links}):
        raise EvidenceError("link observations are not a unique canonical sequence")
    for index, item in enumerate(redirects):
        item = require_keys(
            item,
            {"destination", "location", "source", "status", "url"},
            f"redirect observation {index}",
        )
        source = canonical_path(item["source"], "redirect observation source")
        destination = canonical_path(
            item["destination"], "redirect observation destination"
        )
        if (
            item["status"] not in {301, 308}
            or item["url"] != live_url(EXPECTED_PRODUCTION_ORIGIN, source)
            or not isinstance(item["location"], str)
        ):
            raise EvidenceError("redirect observation is not a permanent production redirect")
        location = urlsplit(urljoin(item["url"], item["location"]))
        if (
            canonical_origin(
                f"{location.scheme}://{location.netloc}", "redirect observation origin"
            )
            != EXPECTED_PRODUCTION_ORIGIN
            or canonical_path(location.path or "/", "redirect observation Location")
            != destination
            or location.query
            or location.fragment
        ):
            raise EvidenceError("redirect observation Location is invalid")
    if [item["source"] for item in redirects] != sorted(
        {item["source"] for item in redirects}
    ):
        raise EvidenceError("redirect observations are not a unique canonical sequence")
    ai_fields = link_fields | {"kind", "title"}
    for index, item in enumerate(ai_outputs):
        item = require_keys(item, ai_fields, f"AI-output observation {index}")
        if item["status"] != 200 or type(item["bytes"]) is not int or item["bytes"] <= 0:
            raise EvidenceError("AI-output observation is not a successful response")
        require_hex(item["body_sha256"], SHA256_RE, "AI-output body SHA-256")
        path = canonical_path(item["path"], "AI-output observation path")
        if item["url"] != live_url(EXPECTED_PRODUCTION_ORIGIN, path) or not same_route(
            item["final_url"], EXPECTED_PRODUCTION_ORIGIN, path
        ):
            raise EvidenceError("AI-output observation URL is not canonical production")
        if item["kind"] == "markdown_page":
            if item["content_type"] != "text/markdown" or not isinstance(
                item["title"], str
            ) or not item["title"]:
                raise EvidenceError("Markdown AI-output observation is invalid")
        elif item["kind"] in {"llms_txt", "llms_full_txt"}:
            if item["content_type"] != "text/plain" or item["title"] is not None:
                raise EvidenceError("LLM index observation is invalid")
        else:
            raise EvidenceError("unknown AI-output observation kind")
    ai_sequence = [(item["kind"], item["path"]) for item in ai_outputs]
    if ai_sequence != sorted(set(ai_sequence)):
        raise EvidenceError("AI-output observations are not a unique canonical sequence")
    if page_summary["checked"] != len(pages):
        raise EvidenceError("page observation count mismatch")
    page_digest = result_set_sha256(pages)
    if (
        page_summary["results_sha256"] != page_digest
        or accessibility_summary["results_sha256"] != page_digest
        or accessibility_summary["pages_checked"] != len(pages)
        or accessibility_summary["rules"] != ACCESSIBILITY_RULES
    ):
        raise EvidenceError("page or accessibility observation digest mismatch")
    if (
        link_summary["targets_checked"] != len(links)
        or link_summary["results_sha256"] != result_set_sha256(links)
        or link_summary["relationships_checked"] != len(relationships)
        or link_summary["relationships_sha256"] != result_set_sha256(relationship_pairs)
    ):
        raise EvidenceError("link observation digest mismatch")
    if (
        redirect_summary["checked"] != len(redirects)
        or redirect_summary["results_sha256"] != result_set_sha256(redirects)
    ):
        raise EvidenceError("redirect observation digest mismatch")
    if (
        ai_summary["outputs_checked"] != len(ai_outputs)
        or ai_summary["index_entries_checked"]
        != len([item for item in ai_outputs if item["kind"] == "markdown_page"])
        or ai_summary["outputs_checked"] != ai_summary["index_entries_checked"] + 2
        or ai_summary["results_sha256"] != result_set_sha256(ai_outputs)
        or SHA256_RE.fullmatch(str(ai_summary["source_llms_txt_sha256"])) is None
    ):
        raise EvidenceError("AI-output observation digest mismatch")


def verify_evidence(
    document: Any,
    expected_documentation_commit: str,
    expected_deployment_id: int,
) -> None:
    require_hex(
        expected_documentation_commit, COMMIT_RE, "expected documentation commit"
    )
    require_positive_integer(expected_deployment_id, "expected deployment ID")
    document = require_keys(
        document,
        {
            "claims",
            "deployment",
            "finished_at",
            "kind",
            "maximum_age_seconds",
            "observations",
            "postdeploy",
            "repository",
            "schema_version",
            "source_checkpoint",
            "started_at",
            "status",
            "workflow",
        },
        "production evidence",
    )
    if (
        document["schema_version"] != SCHEMA_VERSION
        or document["kind"] != KIND
        or document["repository"] != EXPECTED_REPOSITORY_URL
        or document["status"] != "passed"
    ):
        raise EvidenceError("production evidence identity is invalid")
    claims = require_keys(document["claims"], CLAIM_KEYS, "production claims")
    if any(value is not True for value in claims.values()):
        raise EvidenceError("production evidence contains an unproven claim")
    checkpoint = require_keys(
        document["source_checkpoint"],
        {
            "canonical_core_commit",
            "documentation_commit",
            "owned_file_count",
            "source_manifest_sha256",
            "source_tree_sha256",
        },
        "production evidence source checkpoint",
    )
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("documentation_commit") != expected_documentation_commit
        or COMMIT_RE.fullmatch(str(checkpoint.get("canonical_core_commit", ""))) is None
        or SHA256_RE.fullmatch(str(checkpoint.get("source_manifest_sha256", ""))) is None
        or SHA256_RE.fullmatch(str(checkpoint.get("source_tree_sha256", ""))) is None
        or type(checkpoint.get("owned_file_count")) is not int
        or not 1 <= checkpoint["owned_file_count"] <= 4096
    ):
        raise EvidenceError("production evidence source checkpoint is invalid")
    deployment = require_keys(
        document["deployment"],
        {
            "actor",
            "created_at",
            "environment",
            "environment_url",
            "id",
            "production_environment",
            "production_url",
            "state",
            "status_id",
            "transient_environment",
            "updated_at",
        },
        "production evidence deployment",
    )
    if (
        not isinstance(deployment, dict)
        or deployment.get("id") != expected_deployment_id
        or deployment.get("state") != "success"
        or deployment.get("environment") != EXPECTED_ENVIRONMENT
        or deployment.get("production_environment") is not True
        or deployment.get("transient_environment") is not False
        or deployment.get("production_url") != EXPECTED_PRODUCTION_ORIGIN
        or deployment.get("actor")
        != {
            "id": EXPECTED_MINTLIFY_ACTOR_ID,
            "login": EXPECTED_MINTLIFY_ACTOR_LOGIN,
            "type": EXPECTED_MINTLIFY_ACTOR_TYPE,
        }
    ):
        raise EvidenceError("production evidence deployment is invalid")
    require_positive_integer(deployment.get("status_id"), "deployment status ID")
    environment_url = deployment.get("environment_url")
    if not isinstance(environment_url, str):
        raise EvidenceError("production evidence environment URL is invalid")
    if (
        canonical_origin(environment_url, "production evidence environment URL")
        != environment_url
        or environment_url not in ALLOWED_DEPLOYMENT_ORIGINS
    ):
        raise EvidenceError("production evidence environment URL is invalid")
    started = parse_time(document["started_at"], "evidence started_at")
    finished = parse_time(document["finished_at"], "evidence finished_at")
    deployment_created = parse_time(
        deployment["created_at"], "evidence deployment created_at"
    )
    deployment_updated = parse_time(
        deployment["updated_at"], "evidence deployment updated_at"
    )
    if finished < started or (finished - started).total_seconds() > 3600:
        raise EvidenceError("production evidence collection window is invalid")
    if (
        deployment_updated < deployment_created
        or (deployment_updated - started).total_seconds() > MAXIMUM_CLOCK_SKEW_SECONDS
        or (started - deployment_updated).total_seconds() > MAXIMUM_AGE_SECONDS
    ):
        raise EvidenceError("production deployment and evidence times are not bound")
    if document["maximum_age_seconds"] != MAXIMUM_AGE_SECONDS:
        raise EvidenceError("production evidence maximum age is invalid")
    workflow = require_keys(
        document["workflow"],
        {
            "event",
            "expected_conclusion",
            "head_sha",
            "path",
            "ref",
            "repository",
            "run_attempt",
            "run_id",
            "run_url",
        },
        "production evidence workflow",
    )
    expected_workflow = validate_workflow_identity(
        event=workflow["event"],
        run_id=workflow["run_id"],
        run_attempt=workflow["run_attempt"],
        head_sha=workflow["head_sha"],
    )
    if workflow != expected_workflow:
        raise EvidenceError("production evidence workflow identity is invalid")
    verify_observation_hashes(document)


def write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise EvidenceError(f"refusing to overwrite evidence output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    observe_parser = subparsers.add_parser("observe")
    observe_parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    observe_parser.add_argument("--deployment-json", type=Path, required=True)
    observe_parser.add_argument("--statuses-json", type=Path, required=True)
    observe_parser.add_argument("--deployment-id", type=int, required=True)
    observe_parser.add_argument("--documentation-commit", required=True)
    observe_parser.add_argument(
        "--workflow-event",
        choices=("deployment_status", "workflow_dispatch"),
        required=True,
    )
    observe_parser.add_argument("--workflow-run-id", type=int, required=True)
    observe_parser.add_argument("--workflow-run-attempt", type=int, required=True)
    observe_parser.add_argument("--workflow-head-sha", required=True)
    observe_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--evidence", type=Path, required=True)
    verify_parser.add_argument("--deployment-id", type=int, required=True)
    verify_parser.add_argument("--documentation-commit", required=True)
    arguments = parser.parse_args()
    try:
        require_positive_integer(arguments.deployment_id, "deployment ID")
        require_hex(arguments.documentation_commit, COMMIT_RE, "documentation commit")
        if arguments.command == "verify":
            document = read_json(arguments.evidence, maximum=MAXIMUM_EVIDENCE_BYTES)
            verify_evidence(document, arguments.documentation_commit, arguments.deployment_id)
            print("Mintlify production evidence verification passed")
            return 0
        root = arguments.repository_root.resolve(strict=True)
        if current_git_commit(root) != arguments.documentation_commit:
            raise EvidenceError("checked-out documentation commit differs from deployment")
        document = observe(
            root=root,
            deployment=read_json(arguments.deployment_json),
            statuses=read_json(arguments.statuses_json),
            expected_deployment_id=arguments.deployment_id,
            expected_documentation_commit=arguments.documentation_commit,
            workflow_event=arguments.workflow_event,
            workflow_run_id=arguments.workflow_run_id,
            workflow_run_attempt=arguments.workflow_run_attempt,
            workflow_head_sha=arguments.workflow_head_sha,
            fetcher=HTTPSFetcher(),
        )
        write_new(arguments.output, canonical_json(document))
        print(
            "Mintlify production evidence passed: "
            f"deployment {arguments.deployment_id}, "
            f"{document['postdeploy']['pages']['checked']} pages"
        )
        return 0
    except (EvidenceError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
