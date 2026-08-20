"""Framework-owned GitHub feedback submission helpers."""

from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from bobi import paths
from bobi.gitutil import github_token

DEFAULT_FEEDBACK_REPO = "moda-labs/bobi-agent"
GITHUB_API_URL = "https://api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ISSUE_URL_RE = re.compile(
    r"^https://github\.com/[^/\s]+/[^/\s]+/issues/[0-9]+$"
)


class FeedbackError(RuntimeError):
    """A user-facing feedback submission failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FeedbackConfig:
    repo: str = ""
    labels: dict[str, list[str]] | None = None

    def labels_for(self, kind: str) -> list[str]:
        configured = self.labels or {}
        values = configured.get(kind, [])
        return [str(value).strip() for value in values if str(value).strip()]


@dataclass(frozen=True)
class FeedbackContext:
    bobi_version: str
    agent_slot: str
    package: str
    package_version: str
    brain_kind: str
    platform: str
    python: str


@dataclass(frozen=True)
class IssueResult:
    url: str
    number: int
    repo: str
    kind: str
    title: str


def load_feedback_config(config: Any | None) -> FeedbackConfig:
    """Read the optional ``feedback:`` block from a loaded Bobi config."""
    raw = getattr(config, "feedback", {}) or {}
    if not isinstance(raw, dict):
        return FeedbackConfig()
    labels = raw.get("labels", {})
    if not isinstance(labels, dict):
        labels = {}
    normalized: dict[str, list[str]] = {}
    for kind in ("bug", "feature"):
        values = labels.get(kind, [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, (list, tuple)):
            normalized[kind] = [str(value) for value in values]
    return FeedbackConfig(
        repo=str(raw.get("repo", "") or "").strip(), labels=normalized,
    )


def validate_repo(repo: str) -> str:
    repo = repo.strip()
    if not _REPO_RE.fullmatch(repo):
        raise FeedbackError(
            "invalid_feedback_repo",
            "Feedback repo must use the owner/repo form, for example moda-labs/bobi-agent.",
        )
    return repo


def resolve_destination(
    cli_repo: str | None,
    env_repo: str | None,
    config: FeedbackConfig,
) -> str:
    """Resolve a destination without inspecting the current git remote."""
    for candidate in (cli_repo, env_repo, config.repo, DEFAULT_FEEDBACK_REPO):
        if candidate and candidate.strip():
            return validate_repo(candidate)
    raise AssertionError("the default feedback repository must always resolve")


def resolve_token(config_token: str = "") -> str:
    """Prefer runtime service credentials, then existing local GitHub auth."""
    if config_token.strip():
        return config_token.strip()
    return (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or github_token()
    ).strip()


def build_context(project_path, config: Any | None, bobi_version: str) -> FeedbackContext:
    """Build the allowlisted runtime metadata used in issue bodies."""
    return FeedbackContext(
        bobi_version=bobi_version,
        agent_slot=paths.agent_name_for_root(project_path) if project_path else "",
        package=str(getattr(config, "agent", "") or ""),
        package_version=str(getattr(config, "version", "") or ""),
        brain_kind=str(getattr(config, "brain_kind", "") or ""),
        platform=platform.system(),
        python=f"{sys.version_info.major}.{sys.version_info.minor}.x",
    )


def build_issue_body(kind: str, user_body: str, context: FeedbackContext) -> str:
    """Append only the documented, allowlisted context footer."""
    body = user_body.rstrip()
    footer = "\n".join([
        "---",
        "Filed by Bobi feedback tool.",
        "",
        "Generated context:",
        f"- kind: {kind}",
        f"- bobi_version: {context.bobi_version}",
        f"- agent_slot: {context.agent_slot}",
        f"- package: {context.package}",
        f"- package_version: {context.package_version}",
        f"- brain_kind: {context.brain_kind}",
        f"- platform: {context.platform}",
        f"- python: {context.python}",
    ])
    return f"{body}\n\n{footer}"


def _response_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "GitHub returned an invalid error response."
    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message).strip()[:300] if message else "GitHub rejected the request."


def create_github_issue(
    repo: str,
    kind: str,
    title: str,
    body: str,
    labels: list[str],
    *,
    token: str,
    timeout: float = 30,
    client: httpx.Client | None = None,
) -> IssueResult:
    """Create one issue through GitHub's REST API."""
    repo = validate_repo(repo)
    if not token:
        raise FeedbackError(
            "github_auth_missing",
            "No GitHub write credential configured. Set the github service token, GITHUB_TOKEN, or GH_TOKEN.",
        )
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    owns_client = client is None
    request_client = client or httpx.Client(timeout=timeout)
    try:
        try:
            response = request_client.post(
                f"{GITHUB_API_URL}/repos/{repo}/issues",
                headers=headers,
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise FeedbackError(
                "github_timeout",
                f"GitHub did not respond within {timeout:g} seconds.",
            ) from exc
        except httpx.HTTPError as exc:
            raise FeedbackError(
                "github_unavailable", f"Could not reach GitHub: {exc}",
            ) from exc
    finally:
        if owns_client:
            request_client.close()

    if response.status_code in (401, 403):
        raise FeedbackError("github_auth_failed", _response_message(response))
    if response.status_code == 404:
        raise FeedbackError(
            "github_repo_not_found",
            f"GitHub repository not found or inaccessible: {repo}.",
        )
    if response.status_code == 422:
        raise FeedbackError("github_validation_failed", _response_message(response))
    if response.status_code >= 400:
        raise FeedbackError("github_request_failed", _response_message(response))
    try:
        result = response.json()
        url = str(result.get("html_url", ""))
        number = int(result.get("number"))
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise FeedbackError(
            "github_invalid_response", "GitHub returned an invalid issue response.",
        ) from exc
    expected_url = f"https://github.com/{repo}/issues/{number}"
    if not _ISSUE_URL_RE.fullmatch(url) or url.casefold() != expected_url.casefold():
        raise FeedbackError(
            "github_invalid_response", "GitHub returned an invalid issue URL.",
        )
    return IssueResult(
        url=url, number=number, repo=repo, kind=kind, title=title,
    )
