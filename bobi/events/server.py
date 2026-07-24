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
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from bobi.events import artifact as event_server_artifact

log = logging.getLogger(__name__)

# Registration (deployment MINT/JOIN) HTTP timeout. The read leg is generous
# because the cloud event server's registration path occasionally cold-starts
# or runs slow, and a too-tight read timeout was killing agent sessions at init
# (#409: "Event server registration failed … The read operation timed out").
# A long-but-bounded read timeout lets those slow registrations land instead of
# tripping a retry. Connect stays short — a dead host should fail fast.
REGISTER_READ_TIMEOUT = 30.0
REGISTER_TIMEOUT = httpx.Timeout(REGISTER_READ_TIMEOUT, connect=5.0)
DEPENDENCY_STAMP_NAME = ".bobi-lock-digest"


class PackagedEventServerArtifactError(RuntimeError):
    """An installed Bobi distribution lacks its immutable server artifact."""


class NodeRuntimePrerequisiteError(RuntimeError):
    """The supported Node.js runtime needed by the embedded server is absent."""


def _installed_event_server_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "event-server"


def _find_event_server_dir() -> Path:
    candidates = [
        _installed_event_server_dir(),
        Path(__file__).resolve().parent.parent.parent / "event-server",
    ]
    for es_dir in candidates:
        if (es_dir / "package.json").exists():
            return es_dir
    raise FileNotFoundError(
        "event-server directory not found (checked "
        + ", ".join(str(c) for c in candidates) + ")."
    )


def _is_installed_event_server_dir(es_dir: Path) -> bool:
    try:
        return es_dir.resolve() == _installed_event_server_dir().resolve()
    except OSError:
        return False


def resolve_node_runtime() -> tuple[str, str]:
    """Return the supported Node executable and version, or fail actionably."""
    node = shutil.which("node")
    remediation = (
        "Install or upgrade Node.js 20+ and ensure `node` is on PATH, then "
        "restart Bobi."
    )
    if node is None:
        raise NodeRuntimePrerequisiteError(
            f"The local event server requires Node.js 20+, but `node` was not "
            f"found on PATH. {remediation}"
        )
    try:
        result = subprocess.run(
            [node, "--version"],
            capture_output=True,
            env=event_server_artifact.sanitized_node_environment(),
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NodeRuntimePrerequisiteError(
            f"Could not run `{node} --version`: {exc}. {remediation}"
        ) from exc
    version = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise NodeRuntimePrerequisiteError(
            f"`{node} --version` failed (exit {result.returncode}): "
            f"{version or 'no output'}. {remediation}"
        )
    try:
        major = int(version.removeprefix("v").split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise NodeRuntimePrerequisiteError(
            f"Could not parse the Node.js version reported by {node}: "
            f"{version!r}. {remediation}"
        ) from exc
    if major < 20:
        raise NodeRuntimePrerequisiteError(
            f"The local event server requires Node.js 20+; {node} reports "
            f"{version!r}. {remediation}"
        )
    return node, version


def _validate_packaged_artifact(es_dir: Path) -> None:
    try:
        event_server_artifact.validate_artifact(es_dir, verify_inputs=False)
    except event_server_artifact.ArtifactValidationError as exc:
        raise PackagedEventServerArtifactError(
            "The installed Bobi distribution has an incomplete or corrupt "
            f"local event-server artifact ({exc}). Reinstall or upgrade Bobi; "
            "installed package files are immutable and cannot be repaired in place."
        ) from exc


def _dependency_stamp_path(es_dir: Path) -> Path:
    return es_dir / "node_modules" / DEPENDENCY_STAMP_NAME


def _read_dependency_stamp(es_dir: Path) -> dict | None:
    try:
        value = json.loads(_dependency_stamp_path(es_dir).read_text())
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    return value if isinstance(value, dict) else None


def _dependency_tree(es_dir: Path) -> dict:
    result = _run_npm(["npm", "ls", "--all", "--json", "--offline"], es_dir)
    try:
        tree = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"npm ls returned malformed JSON in {es_dir}: {exc}"
        ) from exc
    if not isinstance(tree, dict):
        raise RuntimeError(f"npm ls returned a non-object dependency tree in {es_dir}")
    problems = tree.get("problems")
    if problems:
        raise RuntimeError(f"npm dependency tree is invalid in {es_dir}: {problems}")
    return tree


def _source_dependencies_valid(es_dir: Path) -> bool:
    node_modules = es_dir / "node_modules"
    workspace = node_modules / "@moda-labs" / "bobi-events-core"
    esbuild = node_modules / ".bin" / "esbuild"
    if not workspace.exists() or not esbuild.is_file() or not os.access(esbuild, os.X_OK):
        return False
    stamp = _read_dependency_stamp(es_dir)
    if (
        stamp is None
        or type(stamp.get("schema_version")) is not int
        or stamp["schema_version"] != 1
    ):
        return False
    try:
        lock_digest = event_server_artifact.file_sha256(es_dir / "package-lock.json")
    except event_server_artifact.ArtifactValidationError:
        return False
    if stamp.get("lockfile_sha256") != lock_digest:
        return False
    try:
        tree_digest = event_server_artifact.canonical_json_digest(
            _dependency_tree(es_dir)
        )
    except RuntimeError:
        return False
    return stamp.get("installed_tree_sha256") == tree_digest


def _refresh_dependency_stamp(es_dir: Path) -> None:
    stamp = {
        "installed_tree_sha256": event_server_artifact.canonical_json_digest(
            _dependency_tree(es_dir)
        ),
        "lockfile_sha256": event_server_artifact.file_sha256(
            es_dir / "package-lock.json"
        ),
        "schema_version": 1,
    }
    _dependency_stamp_path(es_dir).write_text(
        json.dumps(stamp, indent=2, sort_keys=True) + "\n"
    )


def _install_source_dependencies(es_dir: Path) -> None:
    _run_npm(["npm", "ci", "--no-audit", "--no-fund"], es_dir)
    _refresh_dependency_stamp(es_dir)


def _build_local(es_dir: Path, node_version: str) -> None:
    _run_npm(["npm", "run", "build:local"], es_dir)
    npm_version = _run_npm(["npm", "--version"], es_dir).stdout.strip()
    if not npm_version:
        raise RuntimeError("npm returned an empty version after building the event server")
    try:
        event_server_artifact.generate_artifact_metadata(
            es_dir,
            node_version=node_version,
            npm_version=npm_version,
        )
    except event_server_artifact.ArtifactValidationError as exc:
        raise RuntimeError(f"local event-server artifact audit failed: {exc}") from exc


def _ensure_source_artifact(es_dir: Path, node_version: str) -> None:
    if event_server_artifact.is_artifact_current(es_dir):
        return

    dependencies_were_valid = _source_dependencies_valid(es_dir)
    if not dependencies_were_valid:
        log.info("Installing exact event-server build dependencies...")
        _install_source_dependencies(es_dir)

    log.info("Building local event server...")
    try:
        _build_local(es_dir, node_version)
    except RuntimeError as first_error:
        if not dependencies_were_valid:
            raise
        log.warning(
            "Event-server build failed with a validated dependency tree; "
            "running one exact reinstall before retrying: %s",
            first_error,
        )
        _install_source_dependencies(es_dir)
        try:
            _build_local(es_dir, node_version)
        except RuntimeError as retry_error:
            raise RuntimeError(
                "event-server build failed before and after one exact dependency "
                f"reinstall; first failure: {first_error}; retry failure: {retry_error}"
            ) from retry_error


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


def _run_npm(
    args: list[str],
    es_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """Run an npm command, surfacing its output on failure.

    npm failures here used to raise a bare CalledProcessError with the
    output captured but never shown — the real cause (e.g. ENOSPC)
    was invisible in manager.log.
    """
    try:
        result = subprocess.run(
            args,
            cwd=str(es_dir),
            capture_output=True,
            env=event_server_artifact.sanitized_node_environment(),
            text=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{args[0]} was not found while running {' '.join(args)} in {es_dir}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{' '.join(args)} timed out after 300s in {es_dir}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        log.error(f"{' '.join(args)} failed (exit {result.returncode}):\n{detail}")
        raise RuntimeError(
            f"{' '.join(args)} failed (exit {result.returncode}): "
            f"{detail or 'no output'}"
        )
    return result


def ensure_running(port: int, webhook_secret: str | None = None,
                   slack_signing_secret: str | None = None,
                   linear_webhook_secret: str | None = None,
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
    is_installed = _is_installed_event_server_dir(es_dir)
    if is_installed:
        _validate_packaged_artifact(es_dir)

    node, node_version = resolve_node_runtime()
    if not is_installed:
        _ensure_source_artifact(es_dir, node_version)

    from bobi import paths
    state = paths.state_dir(project_path)
    log_file = state / "event-server.log"
    pid_file = state / "event-server.pid"

    env = dict(os.environ)
    env["BOBI_ES_PORT"] = str(port)
    resolved_webhook_secret = webhook_secret or ""
    resolved_slack_signing_secret = (
        env.get("SLACK_SIGNING_SECRET", "")
        if slack_signing_secret is None else slack_signing_secret
    )
    resolved_linear_webhook_secret = (
        env.get("LINEAR_WEBHOOK_SECRET", "")
        if linear_webhook_secret is None else linear_webhook_secret
    )
    if resolved_webhook_secret:
        env["BOBI_ES_WEBHOOK_SECRET"] = resolved_webhook_secret
    if resolved_slack_signing_secret:
        env["BOBI_ES_SLACK_SIGNING_SECRET"] = resolved_slack_signing_secret
    if resolved_linear_webhook_secret:
        env["BOBI_ES_LINEAR_WEBHOOK_SECRET"] = resolved_linear_webhook_secret
    # WhatsApp inbound verification (#656): the runtime .env carries the
    # unprefixed vars (the connector card captures them); the local server
    # reads only BOBI_ES_*.
    if env.get("WHATSAPP_APP_SECRET"):
        env["BOBI_ES_WHATSAPP_APP_SECRET"] = env["WHATSAPP_APP_SECRET"]
    if env.get("WHATSAPP_VERIFY_TOKEN"):
        env["BOBI_ES_WHATSAPP_VERIFY_TOKEN"] = env["WHATSAPP_VERIFY_TOKEN"]
    # Discord Gateway (#2): the local server holds the persistent inbound
    # WebSocket, so it needs the bot credential at boot. Same unprefixed ->
    # BOBI_ES_* re-map as WhatsApp.
    if env.get("DISCORD_BOT_TOKEN"):
        env["BOBI_ES_DISCORD_BOT_TOKEN"] = env["DISCORD_BOT_TOKEN"]
    if env.get("DISCORD_APPLICATION_ID"):
        env["BOBI_ES_DISCORD_APPLICATION_ID"] = env["DISCORD_APPLICATION_ID"]
    if env.get("DISCORD_MESSAGE_CONTENT"):
        env["BOBI_ES_DISCORD_MESSAGE_CONTENT"] = env["DISCORD_MESSAGE_CONTENT"]
    if bind:
        env["BOBI_ES_BIND"] = bind
    if extra_env:
        env.update(extra_env)
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_PATH", None)
    env["WS_NO_BUFFER_UTIL"] = "1"
    env["WS_NO_UTF_8_VALIDATE"] = "1"

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            [node, str(es_dir / "dist" / "local.js")],
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
    from bobi.events.signing import signed_request

    resp = signed_request(
        base_url, "POST", "/resources/authorize",
        {"service": service, "resource": resource, "credential": credential},
        bubble_id, bubble_key, timeout=10.0,
    )
    return resp.status_code == 200


def _seed_test_resource_grant(base_url: str, service: str, resource: str,
                              bubble_id: str, bubble_key: str) -> bool:
    """Seed a resource grant through the event server's test-only endpoint.

    This is used only by integration tests that run a black-box event server
    without live GitHub/Linear/Slack credentials. The server route is disabled
    unless it was started with a matching test secret.
    """
    from bobi.events.signing import signed_request

    secret = os.environ.get("BOBI_ES_TEST_GRANTS_SECRET", "")
    if not secret:
        return False
    resp = signed_request(
        base_url, "POST", "/__test/resource-grants",
        {"grants": [{"service": service, "resource": resource}]},
        bubble_id, bubble_key, timeout=5.0,
        extra_headers={"x-moda-test-secret": secret},
    )
    return resp.status_code == 200


def authorize_resources(base_url: str, cfg, subscribe: list[str],
                        bubble_id: str, bubble_key: str,
                        *, filter_unauthorized: bool = True,
                        whatsapp_registered: list[str] | None = None,
                        discord_registered: list[str] | None = None) -> list[str]:
    """Obtain a bubble-scoped resource grant for each global ``github:``/``linear:``
    topic in ``subscribe`` so the subsequent ``register`` / ``update_subscriptions``
    passes the server's #488 grant check.

    By default, returns the subset of ``subscribe`` that is safe to register:
    every non-global topic, every ``slack:`` topic (authorized out-of-band by
    :func:`register_slack_workspaces`), and every ``github:``/``linear:`` topic
    we successfully authorized. A topic whose credential is MISSING or REJECTED
    by the upstream is logged LOUDLY and DROPPED, so it never triggers the
    server's hard-reject during fresh registration.

    ``whatsapp_registered`` is the pnid list :func:`register_whatsapp_numbers`
    returned when the caller just ran it (``None`` means no registration was
    attempted); ``discord_registered`` is the application-id list from
    :func:`register_discord_apps`, same contract. A ``whatsapp:<pnid>`` or
    ``discord:<application_id>`` topic the registration did not back is
    treated exactly like a rejected github/linear credential: the grant the
    server checks is written BY that registration, so keeping the topic would
    hard-reject the whole atomic register/PUT (#488) and stall delivery for
    every channel, not just that one.

    When ``filter_unauthorized`` is false, authorization is still attempted, but
    unverified topics are kept. This is used for saved deployments: the server
    may already hold a no-expiry grant from an earlier start, so replacing the
    deployment's subscriptions with a filtered list would silently unsubscribe a
    valid existing deployment. The server remains authoritative and will reject
    the update if the grant is truly absent.
    """
    if not (bubble_id and bubble_key):
        return list(subscribe)  # can't sign — leave the set unchanged

    # Channels whose grant is written by a signed registration this caller may
    # have just run: None means no registration was attempted (keep the topic),
    # a list means only its members are backed.
    registered_by_service = {
        "whatsapp": whatsapp_registered,
        "discord": discord_registered,
    }

    kept: list[str] = []
    unbacked: list[str] = []
    for sub in subscribe:
        service = sub.split(":", 1)[0] if ":" in sub else ""
        if service in ("github", "linear", "slack", "whatsapp", "discord") and ":" in sub:
            resource = sub.split(":", 1)[1]
            try:
                if _seed_test_resource_grant(base_url, service, resource, bubble_id, bubble_key):
                    kept.append(sub)
                    continue
            except Exception as e:
                log.debug("Test resource-grant seed failed for %r: %s", sub, e)
        registered = registered_by_service.get(service)
        if registered is not None and ":" in sub:
            if sub.split(":", 1)[1] not in registered:
                action = "dropping it from" if filter_unauthorized else "keeping it in"
                log.warning(
                    "%s registration did not back %r — %s this session's "
                    "subscriptions (a resource grant is required, #488)",
                    service, sub, action,
                )
                unbacked.append(sub)
                if not filter_unauthorized:
                    kept.append(sub)
                continue
        if service not in _RESOURCE_CRED_KEYS:
            # Non-global, or slack/whatsapp/discord (granted via their
            # registrations).
            kept.append(sub)
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
            unbacked.append(sub)
            if not filter_unauthorized:
                kept.append(sub)
            continue
        try:
            granted = _authorize_one_resource(
                base_url, service, resource, credential, bubble_id, bubble_key,
            )
        except Exception as e:  # transport hiccup — drop, never block startup
            action = "dropping" if filter_unauthorized else "keeping"
            log.warning(
                "Resource authorize failed for %r: %s — %s",
                sub, type(e).__name__, action,
            )
            unbacked.append(sub)
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
            unbacked.append(sub)
            if not filter_unauthorized:
                kept.append(sub)
    if unbacked:
        action = "dropped" if filter_unauthorized else "kept"
        log.warning(
            "Global event subscriptions without resource grants were %s: %s",
            action, sorted(unbacked),
        )
    return kept


def _post_register(base_url: str, name: str, subscriptions: list[str],
                   bubble_id: str = "", bubble_key: str = "") -> dict:
    """POST /deployments. MINT when no bubble_key (server generates a bubble +
    returns its key once, sent unsigned); JOIN when signed with an existing
    bubble's key. Raises BubbleRejected on a 403 join so callers can re-mint.
    """
    from bobi.events.signing import signed_request

    resp = signed_request(
        base_url, "POST", "/deployments",
        {"name": name, "subscriptions": subscriptions},
        bubble_id, bubble_key, timeout=REGISTER_TIMEOUT,
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
    outbound channel sends accept (#487) - so this bubble can send
    through its own Slack bot. The global record (inbound self-reply loop
    prevention) is written either way, so an unsigned call still works.
    """
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
    try:
        app_token = str(cfg.credential("slack", "app_token") or "").strip()
    except Exception:
        app_token = ""
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
        # Socket Mode is a local-runtime capability and app tokens may only
        # cross this boundary on an authenticated registration. Trust the
        # server's declared mode rather than its URL: a standalone local Node
        # server may be reached over a LAN, tailnet, tunnel, or public host.
        if app_token and bubble_id and bubble_key:
            server_health = health(base_url)
            if server_health and server_health.get("mode") == "local":
                record["app_token"] = app_token
        # Signed when we hold a bubble key, so the server writes the
        # bubble-scoped record outbound channel sends require. Unsigned
        # otherwise (still writes the global self-reply record).
        from bobi.events.signing import signed_request
        signed_request(
            base_url, "POST", "/slack/workspaces", record,
            bubble_id, bubble_key, timeout=10.0,
        )
        log.info(
            "Registered Slack workspace %s (app %s) with event server "
            "(self-reply loop prevention)", team_id, app_id or "?",
        )
        return [team_id]
    except Exception as e:
        detail = str(e)
        for secret in (token, signing_secret, app_token):
            if secret:
                detail = detail.replace(secret, "[redacted]")
        log.warning("Slack workspace registration failed for %s: %s", team_id, detail)
        return []


def register_whatsapp_numbers(base_url: str, cfg, bubble_id: str = "",
                              bubble_key: str = "") -> list[str]:
    """Register the agent's WhatsApp number with the event server (#656).

    Signed-only mirror of :func:`register_slack_workspaces`: the server
    verifies the access token against the Meta Graph API, stores the
    bubble-scoped send credential ``/channels/send`` resolves, and writes the
    ``whatsapp:<phone_number_id>`` resource grant that lets this bubble
    subscribe to the number's inbound topic. Without a bubble key there is
    nothing to register (no unsigned/global use case), so this is a no-op.
    Best-effort: logs and continues on any failure so a registration hiccup
    never blocks startup. Returns the phone number ids registered.
    """
    try:
        token = cfg.credential("whatsapp", "access_token")
        pnid = cfg.credential("whatsapp", "phone_number_id")
    except Exception:
        return []
    if not (token and pnid and bubble_id and bubble_key):
        return []
    try:
        from bobi.events.signing import signed_request
        resp = signed_request(
            base_url, "POST", "/whatsapp/numbers",
            {"phone_number_id": pnid, "access_token": token},
            bubble_id, bubble_key, timeout=10.0,
        )
        if resp.status_code != 200:
            log.warning("WhatsApp number registration rejected for %s: HTTP %d",
                        pnid, resp.status_code)
            return []
        log.info("Registered WhatsApp number %s with event server", pnid)
        return [pnid]
    except Exception as e:
        log.warning("WhatsApp number registration failed for %s: %s", pnid, e)
        return []


def register_discord_apps(base_url: str, cfg, bubble_id: str = "",
                          bubble_key: str = "") -> list[str]:
    """Register the agent's Discord app with the event server (#2).

    Signed-only mirror of :func:`register_whatsapp_numbers`: the server
    verifies the bot token against the Discord API, stores the bubble-scoped
    send credential ``/channels/send`` resolves, and writes the
    ``discord:<application_id>`` resource grant that lets this bubble
    subscribe to the app's inbound topic. On the local runtime a successful
    registration also starts the app's Gateway connection (inbound Discord
    messages arrive over a persistent WebSocket, not a webhook). Without a
    bubble key there is nothing to register. Best-effort: logs and continues
    on any failure so a registration hiccup never blocks startup. Returns the
    application ids registered.
    """
    try:
        token = cfg.credential("discord", "bot_token")
        app_id = cfg.credential("discord", "application_id")
    except Exception:
        return []
    if not (token and app_id and bubble_id and bubble_key):
        return []
    try:
        from bobi.events.signing import signed_request
        resp = signed_request(
            base_url, "POST", "/discord/apps",
            {"application_id": app_id, "bot_token": token},
            bubble_id, bubble_key, timeout=10.0,
        )
        if resp.status_code != 200:
            log.warning("Discord app registration rejected for %s: HTTP %d",
                        app_id, resp.status_code)
            return []
        log.info("Registered Discord app %s with event server", app_id)
        return [app_id]
    except Exception as e:
        log.warning("Discord app registration failed for %s: %s", app_id, e)
        return []
