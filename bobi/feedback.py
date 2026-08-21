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
#: Override the REST base, for a GitHub Enterprise host or a local fake.
GITHUB_API_URL_ENV = "BOBI_GITHUB_API_URL"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ISSUE_URL_RE = re.compile(
    r"^https://github\.com/[^/\s]+/[^/\s]+/issues/[0-9]+$"
)

#: Operator opt-out for the self-diagnosis path. Unset means ON; a set value is
#: read with config._as_bool semantics, so "0"/"false"/"off" turn it off.
RCA_ENV = "BOBI_FRAMEWORK_RCA"
#: Set on the RCA sub-agent's own environment so it cannot start another one.
#: Distinct from RCA_ENV on purpose: the sub-agent must still be able to FILE
#: its finding, which the operator switch would also forbid.
RCA_ACTIVE_ENV = "BOBI_FRAMEWORK_RCA_ACTIVE"

#: Brevity is an acceptance criterion for generated reports, not a nicety, so
#: it is enforced here rather than left to the prompt.
MAX_TITLE_CHARS = 120
MAX_RCA_BODY_CHARS = 1200

#: How many search hits to consider, and how much of a candidate title's
#: significant vocabulary an existing title must cover to count as the same bug.
DUPLICATE_SEARCH_LIMIT = 10
DUPLICATE_MATCH_RATIO = 0.6

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_TITLE_STOPWORDS = frozenset({
    "a", "about", "after", "an", "and", "are", "because", "been", "before",
    "but", "did", "does", "for", "from", "has", "have", "into", "is", "not",
    "onto", "since", "that", "the", "their", "then", "there", "these", "this",
    "those", "to", "until", "was", "were", "what", "when", "where", "which",
    "while", "who", "why", "with", "without",
})


def github_api_url() -> str:
    """The REST base, overridable for GitHub Enterprise or a local fake."""
    return (os.environ.get(GITHUB_API_URL_ENV) or GITHUB_API_URL).rstrip("/")


def rca_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the operator allows framework-bug self-diagnosis."""
    from bobi.config import _as_bool

    raw = (os.environ if env is None else env).get(RCA_ENV)
    return True if raw is None else _as_bool(raw)


def rca_in_progress(env: dict[str, str] | None = None) -> bool:
    """Whether this process is already running inside an RCA sub-agent."""
    from bobi.config import _as_bool

    return _as_bool((os.environ if env is None else env).get(RCA_ACTIVE_ENV, ""))


def rca_guide(error: str = "") -> str:
    """The self-diagnosis prompt, with the failure appended when known."""
    from bobi.prompts import FRAMEWORK_BUG_RCA_PATH

    try:
        guide = FRAMEWORK_BUG_RCA_PATH.read_text(encoding="utf-8").rstrip()
    except OSError as exc:
        raise FeedbackError(
            "rca_guide_missing",
            "The framework bug RCA prompt is missing from this bobi install.",
        ) from exc
    if not error.strip():
        return guide
    return f"{guide}\n\n## The failure to analyze\n\n{error.strip()}"


def clamp_text(value: str, limit: int) -> str:
    """Trim *value* to *limit* characters on a word boundary, marking the cut."""
    text = value.strip()
    if len(text) <= limit:
        return text
    marker = " [truncated]"
    head = text[: max(0, limit - len(marker))]
    spaced = head.rsplit(" ", 1)[0] if " " in head else head
    return f"{spaced.rstrip()}{marker}".strip()


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


@dataclass(frozen=True)
class IssueMatch:
    """An existing issue the destination repo already carries."""

    number: int
    title: str
    url: str
    state: str


@dataclass(frozen=True)
class FeedbackOutcome:
    """What one filing did: opened an issue, or added to an existing one."""

    action: str
    url: str
    number: int
    repo: str
    kind: str
    title: str
    comment_url: str = ""
    warning: str = ""


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


def _auth_headers(token: str) -> dict[str, str]:
    if not token:
        raise FeedbackError(
            "github_auth_missing",
            "No GitHub write credential configured. Set the github service token, GITHUB_TOKEN, or GH_TOKEN.",
        )
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _raise_for_status(response: httpx.Response, repo: str) -> None:
    """Map one GitHub status onto the stable FeedbackError vocabulary."""
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


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    client: httpx.Client | None,
    **kwargs: Any,
) -> httpx.Response:
    """One GitHub call, with transport failures mapped to FeedbackError."""
    owns_client = client is None
    request_client = client or httpx.Client(timeout=timeout)
    try:
        return request_client.request(method, url, headers=headers, **kwargs)
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
    api_url: str | None = None,
) -> IssueResult:
    """Create one issue through GitHub's REST API."""
    repo = validate_repo(repo)
    headers = _auth_headers(token)
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    response = _request(
        "POST",
        f"{api_url or github_api_url()}/repos/{repo}/issues",
        headers=headers,
        timeout=timeout,
        client=client,
        json=payload,
    )

    _raise_for_status(response, repo)
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


def _significant_tokens(title: str) -> set[str]:
    """The words of *title* that carry meaning, for duplicate comparison."""
    words = _TOKEN_SPLIT_RE.split(title.casefold())
    return {w for w in words if len(w) > 2 and w not in _TITLE_STOPWORDS}


def duplicate_ratio(candidate: str, existing: str) -> float:
    """How much of *candidate*'s significant vocabulary *existing* covers."""
    wanted = _significant_tokens(candidate)
    if not wanted:
        return 0.0
    return len(wanted & _significant_tokens(existing)) / len(wanted)


def search_existing_issues(
    repo: str,
    title: str,
    *,
    token: str,
    timeout: float = 30,
    client: httpx.Client | None = None,
    api_url: str | None = None,
) -> list[IssueMatch]:
    """Issues in *repo* whose titles look like *title*, best match first.

    Closed issues are included on purpose: a framework bug that comes back
    belongs on its original report, not on a fresh one.
    """
    repo = validate_repo(repo)
    terms = " ".join(sorted(_significant_tokens(title)))
    if not terms:
        return []
    response = _request(
        "GET",
        f"{api_url or github_api_url()}/search/issues",
        headers=_auth_headers(token),
        timeout=timeout,
        client=client,
        params={
            "q": f"repo:{repo} is:issue in:title {terms}",
            "per_page": DUPLICATE_SEARCH_LIMIT,
            "advanced_search": "true",
        },
    )
    _raise_for_status(response, repo)
    try:
        items = response.json().get("items", [])
    except (ValueError, AttributeError) as exc:
        raise FeedbackError(
            "github_invalid_response", "GitHub returned an invalid search response.",
        ) from exc
    if not isinstance(items, list):
        raise FeedbackError(
            "github_invalid_response", "GitHub returned an invalid search response.",
        )
    matches = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("number"))
        except (TypeError, ValueError):
            continue
        matches.append(IssueMatch(
            number=number,
            title=str(item.get("title", "")),
            url=f"https://github.com/{repo}/issues/{number}",
            state=str(item.get("state", "")),
        ))
    return matches


def pick_duplicate(title: str, matches: list[IssueMatch]) -> IssueMatch | None:
    """The existing issue *title* should join, or None to open a new one.

    GitHub already ranks by relevance; the ratio floor is what stops a merely
    top-ranked result from swallowing an unrelated report.
    """
    scored = [
        (duplicate_ratio(title, m.title), m.state != "closed", -m.number, m)
        for m in matches
    ]
    qualified = [row for row in scored if row[0] >= DUPLICATE_MATCH_RATIO]
    if not qualified:
        return None
    return max(qualified, key=lambda row: row[:3])[3]


def build_recurrence_comment(body: str, context: FeedbackContext) -> str:
    """The short comment added to an existing report, not a second issue."""
    footer = " | ".join(part for part in [
        f"bobi {context.bobi_version}" if context.bobi_version else "",
        f"{context.package} {context.package_version}".strip()
        if context.package else "",
        f"{context.platform} py{context.python}" if context.platform else "",
    ] if part)
    lines = ["Seen again, reported by the Bobi feedback tool.", "", body.strip()]
    if footer:
        lines.extend(["", f"_{footer}_"])
    return "\n".join(lines)


def comment_on_issue(
    repo: str,
    number: int,
    body: str,
    *,
    token: str,
    timeout: float = 30,
    client: httpx.Client | None = None,
    api_url: str | None = None,
) -> str:
    """Add one comment to an existing issue, returning its URL."""
    repo = validate_repo(repo)
    response = _request(
        "POST",
        f"{api_url or github_api_url()}/repos/{repo}/issues/{number}/comments",
        headers=_auth_headers(token),
        timeout=timeout,
        client=client,
        json={"body": body},
    )
    _raise_for_status(response, repo)
    try:
        url = str(response.json().get("html_url", ""))
    except (ValueError, AttributeError) as exc:
        raise FeedbackError(
            "github_invalid_response", "GitHub returned an invalid comment response.",
        ) from exc
    if not url.startswith(f"https://github.com/{repo}/issues/{number}#"):
        raise FeedbackError(
            "github_invalid_response", "GitHub returned an invalid comment URL.",
        )
    return url


def submit_feedback(
    repo: str,
    kind: str,
    title: str,
    body: str,
    labels: list[str],
    context: FeedbackContext,
    *,
    token: str,
    dedupe: bool = True,
    dedupe_required: bool = False,
    timeout: float = 30,
    client: httpx.Client | None = None,
    api_url: str | None = None,
) -> FeedbackOutcome:
    """File one piece of feedback: the single path to the issue tracker.

    Searches for an existing report first and comments on the match rather than
    opening a duplicate, because one recurring framework bug must not become
    fifty issues. When *dedupe_required* the search failing aborts the filing,
    which is what an automated caller wants: no search means no duplicate
    protection, and an unattended writer must not open issues blind. An
    interactive caller instead proceeds with a warning, because a human's
    explicit report should not be lost to a rate limit.
    """
    repo = validate_repo(repo)
    warning = ""
    # An automated filing always dedupes; the invariant lives here rather than
    # at each call site, so no caller can opt a robot out of it.
    if dedupe or dedupe_required:
        try:
            match = pick_duplicate(title, search_existing_issues(
                repo, title, token=token, timeout=timeout,
                client=client, api_url=api_url,
            ))
        except FeedbackError as exc:
            if dedupe_required:
                raise FeedbackError(
                    "dedupe_unavailable",
                    f"Could not check for an existing report, so nothing was filed: {exc.message}",
                ) from exc
            match, warning = None, (
                f"Duplicate check failed ({exc.code}: {exc.message}); "
                "filing a new issue anyway."
            )
        if match is not None:
            comment_url = comment_on_issue(
                repo, match.number,
                build_recurrence_comment(body, context),
                token=token, timeout=timeout, client=client, api_url=api_url,
            )
            return FeedbackOutcome(
                action="commented", url=match.url, number=match.number,
                repo=repo, kind=kind, title=match.title,
                comment_url=comment_url,
            )
    issue = create_github_issue(
        repo, kind, title, build_issue_body(kind, body, context), labels,
        token=token, timeout=timeout, client=client, api_url=api_url,
    )
    return FeedbackOutcome(
        action="created", url=issue.url, number=issue.number, repo=repo,
        kind=kind, title=issue.title, warning=warning,
    )
