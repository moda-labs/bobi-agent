"""Local event server launcher.

The event server codebase is TypeScript in event-server/. This module
provides Python helpers to start it locally and register deployments.
The same TypeScript core runs on Cloudflare Workers (production) or
Node.js (local development).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Registration (deployment MINT/JOIN) HTTP timeout. The read leg is generous
# because the cloud event server's registration path occasionally cold-starts
# or runs slow, and a too-tight read timeout was killing agent sessions at init
# (#409: "Event server registration failed … The read operation timed out").
# A long-but-bounded read timeout lets those slow registrations land instead of
# tripping a retry. Connect stays short — a dead host should fail fast.
REGISTER_READ_TIMEOUT = 30.0
REGISTER_TIMEOUT = httpx.Timeout(REGISTER_READ_TIMEOUT, connect=5.0)


def _find_event_server_dir() -> Path:
    pkg_dir = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_dir / "event-server",         # bundled in the installed package
        pkg_dir.parent / "event-server",  # repo checkout
    ]
    for es_dir in candidates:
        if (es_dir / "package.json").exists():
            return es_dir
    raise FileNotFoundError(
        "event-server directory not found (checked "
        + ", ".join(str(c) for c in candidates) + ")."
    )


def _needs_build(es_dir: Path) -> bool:
    dist = es_dir / "dist" / "local.js"
    if not dist.exists():
        return True
    src_mtime = max(f.stat().st_mtime for f in (es_dir / "src").rglob("*.ts"))
    return dist.stat().st_mtime < src_mtime


def health(base_url: str, timeout: float = 2) -> dict | None:
    """Probe an event server's /health endpoint.

    Returns the parsed health payload when the server reports ok, else None.
    The single definition of "what counts as healthy" — used by ensure_running,
    `bobi agent <name> stop`, `bobi agent <name> event-server status`, and doctor.
    """
    from bobi import http as pooled

    try:
        resp = pooled.get(f"{base_url}/health", timeout=timeout)
        data = resp.json()
        return data if data.get("status") == "ok" else None
    except Exception:
        return None


def _is_local_url(url: str) -> bool:
    """Whether *url* points at the local machine (localhost / loopback).

    An empty string is treated as local (no URL configured → local default).
    """
    if not url:
        return True
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def _run_npm(args: list[str], es_dir: Path) -> None:
    """Run an npm command, surfacing its output on failure.

    npm failures here used to raise a bare CalledProcessError with the
    output captured but never shown — the real cause (e.g. ENOSPC)
    was invisible in manager.log.
    """
    result = subprocess.run(
        args, cwd=str(es_dir), capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        log.error(f"{' '.join(args)} failed (exit {result.returncode}):\n{detail}")
        raise RuntimeError(
            f"{' '.join(args)} failed (exit {result.returncode}): "
            f"{detail or 'no output'}"
        )


def ensure_running(port: int, webhook_secret: str = "",
                   slack_signing_secret: str = "",
                   bind: str = "",
                   project_path: Path | None = None,
                   extra_env: dict[str, str] | None = None) -> str:
    """Start the local event server if not already running.

    Returns "connected" if an existing server was found, "started" if
    a new one was launched.

    ``bind`` controls the listen address (passed as ``BOBI_ES_BIND``).
    When empty, the env var is forwarded from the parent process if set;
    the Node server itself defaults to ``127.0.0.1`` (loopback-only).

    ``extra_env`` passes additional environment variables to the child
    process (e.g. eviction thresholds for testing).
    """
    # ── Remote-URL guard (containerized-6) ────────────────────────────
    # When the project configures a remote event_server_url, the local
    # Node server must never start — the container may not even have Node.
    if project_path is not None:
        try:
            from bobi.config import Config
            configured_url = Config.load(project_path).event_server_url
        except Exception:
            configured_url = ""
        if configured_url and not _is_local_url(configured_url):
            log.info(
                "Remote event_server_url configured (%s) — skipping local server",
                configured_url,
            )
            return "skipped"

    if health(f"http://localhost:{port}"):
        if project_path is not None:
            from bobi import paths
            (paths.state_dir(project_path) / "event-server.port").write_text(str(port))
        log.info(f"Event server already running on port {port}")
        return "connected"

    es_dir = _find_event_server_dir()

    if not (es_dir / "node_modules").exists():
        log.info("Installing event server dependencies...")
        _run_npm(["npm", "install", "--no-audit", "--no-fund"], es_dir)

    if _needs_build(es_dir):
        log.info("Building local event server...")
        _run_npm(["npm", "run", "build:local"], es_dir)

    from bobi import paths
    state = paths.state_dir(project_path)
    log_file = state / "event-server.log"
    pid_file = state / "event-server.pid"

    env = dict(os.environ)
    env["BOBI_ES_PORT"] = str(port)
    if webhook_secret:
        env["BOBI_ES_WEBHOOK_SECRET"] = webhook_secret
    if slack_signing_secret:
        env["BOBI_ES_SLACK_SIGNING_SECRET"] = slack_signing_secret
    if bind:
        env["BOBI_ES_BIND"] = bind
    if extra_env:
        env.update(extra_env)

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            ["node", str(es_dir / "dist" / "local.js")],
            stdout=lf, stderr=lf,
            env=env, start_new_session=True,
        )

    pid_file.write_text(str(proc.pid))

    for _ in range(30):
        time.sleep(0.5)
        if health(f"http://localhost:{port}"):
            (state / "event-server.port").write_text(str(port))
            log.info(f"Event server started on port {port} (pid {proc.pid})")
            return "started"
    log.error("Event server failed to start within 15 seconds")
    return "failed"


class BubbleRejected(Exception):
    """A signed JOIN was rejected (403) — the server does not recognize the
    bubble (e.g. it restarted and lost its in-memory bubbles). The caller
    should re-mint and re-join."""


class UnauthorizedTopics(Exception):
    """A register / update was rejected (400) because one or more GLOBAL resource
    topics lack a server-verified grant for this bubble (#488). Carries the
    offending ``topics`` so the caller can surface a configuration error."""

    def __init__(self, topics: list[str]):
        self.topics = topics
        super().__init__(f"unauthorized resource topics: {topics}")


# Map each global topic service to the (config service, credential key) that
# unlocks it. Slack is absent — it authorizes via the signed workspace
# registration (register_slack_workspaces), not /resources/authorize.
_RESOURCE_CRED_KEYS = {"github": ("github", "token"), "linear": ("linear", "api_key")}


def _authorize_one_resource(base_url: str, service: str, resource: str,
                            credential: str, bubble_id: str, bubble_key: str) -> bool:
    """POST /resources/authorize for a single resource. Returns True iff the
    server granted (200). The credential is signed-over and transmitted but is
    NEVER logged here (the server stores only the grant)."""
    from bobi import http as pooled
    from bobi.events.signing import serialize_body, sign_headers

    body = serialize_body({"service": service, "resource": resource, "credential": credential})
    headers = {"Content-Type": "application/json"}
    headers.update(sign_headers(bubble_id, bubble_key, "POST", "/resources/authorize", body))
    resp = pooled.post(
        f"{base_url}/resources/authorize",
        content=body,
        headers=headers,
        timeout=10.0,
    )
    return resp.status_code == 200


def _seed_test_resource_grant(base_url: str, service: str, resource: str,
                              bubble_id: str, bubble_key: str) -> bool:
    """Seed a resource grant through the event server's test-only endpoint.

    This is used only by integration tests that run a black-box event server
    without live GitHub/Linear/Slack credentials. The server route is disabled
    unless it was started with a matching test secret.
    """
    from bobi import http as pooled
    from bobi.events.signing import serialize_body, sign_headers

    secret = os.environ.get("BOBI_ES_TEST_GRANTS_SECRET", "")
    if not secret:
        return False
    body = serialize_body({"grants": [{"service": service, "resource": resource}]})
    headers = {"Content-Type": "application/json", "x-moda-test-secret": secret}
    headers.update(sign_headers(bubble_id, bubble_key, "POST", "/__test/resource-grants", body))
    resp = pooled.post(
        f"{base_url}/__test/resource-grants",
        content=body,
        headers=headers,
        timeout=5.0,
    )
    return resp.status_code == 200


def authorize_resources(base_url: str, cfg, subscribe: list[str],
                        bubble_id: str, bubble_key: str,
                        *, filter_unauthorized: bool = True) -> list[str]:
    """Obtain a bubble-scoped resource grant for each global ``github:``/``linear:``
    topic in ``subscribe`` so the subsequent ``register`` / ``update_subscriptions``
    passes the server's #488 grant check.

    By default, returns the subset of ``subscribe`` that is safe to register:
    every non-global topic, every ``slack:`` topic (authorized out-of-band by
    :func:`register_slack_workspaces`), and every ``github:``/``linear:`` topic
    we successfully authorized. A topic whose credential is MISSING or REJECTED
    by the upstream is logged LOUDLY and DROPPED, so it never triggers the
    server's hard-reject during fresh registration.

    When ``filter_unauthorized`` is false, authorization is still attempted, but
    unverified topics are kept. This is used for saved deployments: the server
    may already hold a no-expiry grant from an earlier start, so replacing the
    deployment's subscriptions with a filtered list would silently unsubscribe a
    valid existing deployment. The server remains authoritative and will reject
    the update if the grant is truly absent.
    """
    if not (bubble_id and bubble_key):
        return list(subscribe)  # can't sign — leave the set unchanged

    kept: list[str] = []
    for sub in subscribe:
        service = sub.split(":", 1)[0] if ":" in sub else ""
        if service in ("github", "linear", "slack") and ":" in sub:
            resource = sub.split(":", 1)[1]
            try:
                if _seed_test_resource_grant(base_url, service, resource, bubble_id, bubble_key):
                    kept.append(sub)
                    continue
            except Exception as e:
                log.debug("Test resource-grant seed failed for %r: %s", sub, e)
        if service not in _RESOURCE_CRED_KEYS:
            kept.append(sub)  # non-global, or slack (granted via workspace reg)
            continue
        resource = sub.split(":", 1)[1]
        cfg_service, cred_key = _RESOURCE_CRED_KEYS[service]
        try:
            credential = cfg.credential(cfg_service, cred_key)
        except Exception:
            credential = ""
        if not credential:
            action = "dropping it from" if filter_unauthorized else "keeping it in"
            log.warning(
                "No %s credential to authorize %r — %s this "
                "session's subscriptions (a resource grant is required, #488)",
                service, sub, action,
            )
            if not filter_unauthorized:
                kept.append(sub)
            continue
        try:
            granted = _authorize_one_resource(
                base_url, service, resource, credential, bubble_id, bubble_key,
            )
        except Exception as e:  # transport hiccup — drop, never block startup
            action = "dropping" if filter_unauthorized else "keeping"
            log.warning("Resource authorize failed for %r: %s — %s", sub, e, action)
            if not filter_unauthorized:
                kept.append(sub)
            continue
        if granted:
            kept.append(sub)
        else:
            action = "dropping from" if filter_unauthorized else "keeping in"
            log.warning(
                "Event server denied a resource grant for %r — the configured "
                "%s credential cannot read it; %s subscriptions (#488)",
                sub, service, action,
            )
            if not filter_unauthorized:
                kept.append(sub)
    return kept


def _post_register(base_url: str, name: str, subscriptions: list[str],
                   bubble_id: str = "", bubble_key: str = "") -> dict:
    """POST /deployments. MINT when no bubble_key (server generates a bubble +
    returns its key once); JOIN when signed with an existing bubble's key.

    Signs over the exact transmitted bytes (content=, not json=) so the
    server's HMAC verification reproduces the signature. Raises BubbleRejected
    on a 403 join so callers can re-mint.
    """
    from bobi import http as pooled
    from bobi.events.signing import serialize_body, sign_headers

    body = serialize_body({"name": name, "subscriptions": subscriptions})
    headers = {"Content-Type": "application/json"}
    if bubble_key:
        headers.update(sign_headers(bubble_id, bubble_key, "POST", "/deployments", body))

    resp = pooled.post(
        f"{base_url}/deployments",
        content=body,
        headers=headers,
        timeout=REGISTER_TIMEOUT,
    )
    if resp.status_code == 403:
        raise BubbleRejected(f"join rejected for bubble {bubble_id}")
    if resp.status_code == 400:
        try:
            data = resp.json()
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("error") == "unauthorized_topics":
            raise UnauthorizedTopics(list(data.get("topics") or []))
    return resp.json()


def register(base_url: str, name: str, subscriptions: list[str],
             bubble_id: str = "", bubble_key: str = "",
             _retry_unauthorized: bool = True) -> tuple[str, str]:
    """JOIN a deployment into the instance's bubble. Returns (deployment_id,
    api_key). Callers pass the bubble credential from :func:`ensure_bubble`;
    the bubble must already exist (mint happens only in ensure_bubble).

    On a ``400 unauthorized_topics`` (#488) — which, after a successful
    :func:`authorize_resources`, almost always means Cloudflare KV has not yet
    propagated a just-written grant — retry ONCE after a short delay before
    surfacing the configuration error, so transient propagation lag does not
    look like a misconfiguration.
    """
    try:
        result = _post_register(base_url, name, subscriptions, bubble_id, bubble_key)
    except UnauthorizedTopics as e:
        if _retry_unauthorized:
            time.sleep(0.5)  # absorb KV read-your-writes propagation lag
            return register(base_url, name, subscriptions, bubble_id, bubble_key,
                            _retry_unauthorized=False)
        log.error(
            "Event server rejected subscriptions as unauthorized — no resource "
            "grant for %s. Check the upstream credential for these resources (#488).",
            e.topics,
        )
        raise
    return result["deployment_id"], result["api_key"]


def _is_loopback_or_tls(base_url: str) -> bool:
    """Whether the bubble key may safely transit to this URL at mint time."""
    from urllib.parse import urlsplit

    if base_url.startswith("https://"):
        return True
    host = urlsplit(base_url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def ensure_bubble(base_url: str, project_path: Path,
                  force_remint_of: str = "") -> dict:
    """Return the instance's bubble credential, minting it once if absent.

    The SINGLE seam all deployments go through: every session/agent/reply
    channel JOINs the bubble this returns. Minting is lock-protected
    (O_CREAT|O_EXCL) so two concurrent first-registrations converge on one
    bubble instead of splitting the instance. Minting transmits the key once,
    so it is refused over a non-loopback cleartext URL.

    ``force_remint_of`` is a compare-and-swap: when a JOIN was rejected because
    the server forgot the bubble (restart), the caller passes the stale
    bubble_id. We re-mint ONLY if the on-disk bubble still matches it — if
    another session already re-minted, we return the new one instead of
    splitting the instance into a third bubble.
    """
    import os

    from bobi.config import load_bubble_state, save_bubble_state, bubble_state_path

    existing = load_bubble_state(project_path)
    if existing.get("bubble_id") and existing.get("bubble_key"):
        if not force_remint_of or existing["bubble_id"] != force_remint_of:
            return existing
        # else: caller flagged this bubble stale — fall through to re-mint.

    lock_path = bubble_state_path(project_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        # Another process is minting — wait for it to publish bubble.json.
        # The budget MUST exceed the mint's HTTP timeout (_post_register, now
        # REGISTER_READ_TIMEOUT=30s) plus margin — otherwise a slow-but-alive
        # first minter outlasts the wait and the waiter forks its own bubble.
        # 45s covers it; only a crashed minter holding the lock falls through
        # to mint ourselves.
        for _ in range(450):
            time.sleep(0.1)
            existing = load_bubble_state(project_path)
            if existing.get("bubble_id") and existing["bubble_id"] != force_remint_of:
                return existing
        # Stale lock (minter died) — fall through and mint ourselves.

    try:
        existing = load_bubble_state(project_path)
        if existing.get("bubble_id") and existing["bubble_id"] != force_remint_of:
            return existing  # someone already (re)minted under the lock

        if not _is_loopback_or_tls(base_url):
            raise RuntimeError(
                f"Refusing to mint a bubble over cleartext remote URL {base_url} "
                "— the bubble key would transit in the clear. Use https:// or a "
                "loopback event server."
            )

        # MINT via a throwaway bootstrap deployment (the server mints a bubble
        # as part of registration). One idle deployment per bubble — negligible.
        result = _post_register(base_url, "bubble-bootstrap", ["_bootstrap"])
        save_bubble_state(project_path, result["bubble_id"], result["bubble_key"])
        return load_bubble_state(project_path)
    finally:
        lock_path.unlink(missing_ok=True)


def deregister(base_url: str, deployment_id: str, api_key: str) -> bool:
    """Deregister a deployment. Returns True on success, False on failure."""
    from bobi import http as pooled

    try:
        resp = pooled.delete(
            f"{base_url}/deployments/{deployment_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        return resp.status_code == 200
    except Exception as e:
        log.debug("Deployment deregister failed for %s: %s", deployment_id, e)
        return False


def _slack_auth_info(token: str) -> tuple[str, str, str]:
    """Resolve (team_id, bot_id, bot_user_id) from a bot token via auth.test."""
    from bobi import http as pooled

    try:
        resp = pooled.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        data = resp.json()
        if data.get("ok"):
            return (
                data.get("team_id", "") or "",
                data.get("bot_id", "") or "",
                data.get("user_id", "") or "",
            )
    except Exception as e:  # best-effort — never block startup
        log.debug("Slack auth.test failed during workspace registration: %s", e)
    return "", "", ""


def _slack_app_id(token: str, bot_id: str) -> str:
    """Resolve api_app_id from a bot id via bots.info.

    ``auth.test`` does not return the app id, but the event server keys each
    bot's record by ``api_app_id`` (the only id unique per app AND present on
    every inbound event) so two bots can share one workspace. Best-effort.
    """
    if not bot_id:
        return ""
    from urllib.parse import quote

    from bobi import http as pooled

    try:
        resp = pooled.get(
            f"https://slack.com/api/bots.info?bot={quote(bot_id)}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        data = resp.json()
        if data.get("ok"):
            return (data.get("bot", {}) or {}).get("app_id", "") or ""
    except Exception as e:  # best-effort — server can fall back to bot_id keying
        log.debug("Slack bots.info failed during workspace registration: %s", e)
    return ""


def register_slack_workspaces(base_url: str, cfg, bubble_id: str = "",
                              bubble_key: str = "") -> list[str]:
    """Register the agent's Slack workspace(s) with the event server.

    The event server uses the registered ``bot_id`` to skip the bot's OWN
    messages (``event.bot_id == selfBotId``). Without this, an agent's own
    Slack reply is re-ingested as a fresh inbound event and it loops on
    itself. This wires the missing registration so that loop prevention
    actually engages. Best-effort: logs and continues on any failure so a
    registration hiccup never blocks startup. Returns the workspace ids
    successfully registered.

    When ``bubble_key`` is supplied the request is HMAC-signed (same scheme as
    :func:`_post_register`). A signed registration tells the server to ALSO
    store a bubble-scoped workspace record, which is the only credential
    outbound ``POST /slack/send`` will accept (#487) — so this bubble can send
    through its own Slack bot. The global record (inbound self-reply loop
    prevention) is written either way, so an unsigned call still works.
    """
    from bobi import http as pooled

    try:
        token = cfg.credential("slack", "bot_token")
    except Exception:
        token = ""
    if not token:
        return []
    try:
        signing_secret = cfg.credential("slack", "signing_secret")
    except Exception:
        signing_secret = ""
    team_id, bot_id, bot_user_id = _slack_auth_info(token)
    if not team_id:
        return []
    app_id = _slack_app_id(token, bot_id)
    try:
        # Send bot_id explicitly when known: the server's own auth.test
        # fallback is best-effort, and a registration without bot_id
        # silently disables self-reply filtering for the whole workspace.
        # app_id keys the per-bot record so two bots can share a workspace
        # without clobbering each other; signing_secret lets the server verify
        # THIS app's inbound events (a second app signs with its own secret).
        record: dict = {"workspace_id": team_id, "bot_token": token}
        if bot_id:
            record["bot_id"] = bot_id
        if bot_user_id:
            record["bot_user_id"] = bot_user_id
        if app_id:
            record["app_id"] = app_id
        if signing_secret:
            record["signing_secret"] = signing_secret
        # Sign over the EXACT transmitted bytes (content=, not json=) when we
        # hold a bubble key, so the server reproduces the HMAC and writes the
        # bubble-scoped record outbound /slack/send requires. Unsigned otherwise
        # (still writes the global self-reply record).
        from bobi.events.signing import serialize_body, sign_headers
        body = serialize_body(record)
        headers = {"Content-Type": "application/json"}
        if bubble_key:
            headers.update(
                sign_headers(bubble_id, bubble_key, "POST", "/slack/workspaces", body)
            )
        pooled.post(
            f"{base_url}/slack/workspaces",
            content=body,
            headers=headers,
            timeout=10.0,
        )
        log.info(
            "Registered Slack workspace %s (app %s) with event server "
            "(self-reply loop prevention)", team_id, app_id or "?",
        )
        return [team_id]
    except Exception as e:
        log.warning("Slack workspace registration failed for %s: %s", team_id, e)
        return []
