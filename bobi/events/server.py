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

from bobi import launch_stamp
from bobi.events import artifact as event_server_artifact
from bobi.fsutil import atomic_write_text

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

# The LOCAL server is built from `event-server/src/local.ts` with esbuild. It
# needs the workspace root's own dependencies and the shared `core` package -
# and nothing from the `worker` workspace, which exists to be deployed to
# Cloudflare and is never imported by this bundle.
#
# npm installs every workspace by default, so without this scope a local
# bootstrap drags in the Worker's whole toolchain (`agents`, the MCP SDK,
# wrangler, babel) - roughly four times the tree, none of it reachable from
# `local.ts`. That surplus is not merely wasted: it is what put `agents`' peer
# conflicts and optional binaries into the very dependency tree this module
# inspects to decide whether the install is healthy.
#
# Install scope and inspect scope MUST stay identical. `npm ls` reports a
# workspace that was deliberately not installed as `missing:`, which is fatal
# by design - so an unscoped read of a scoped install would fail every time.
_LOCAL_WORKSPACE_SCOPE = ("--include-workspace-root", "--workspace", "core")


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


# `npm ls` problem classes that do NOT block a source build.
#
# DEFAULT-DENY: every problem npm reports is fatal unless it is listed here.
# The inverse (list the fatal ones) was tried first and was wrong - it let
# `peer dep missing: ...` through, because that string does not start with
# `missing:`, so a genuinely absent dependency read as harmless. Any wording
# npm adds in future would slip through the same hole. Enumerating the
# harmless class instead means an unrecognized problem fails loudly and gets
# classified deliberately.
#
# `extraneous` is harmless by construction: it means "installed but not
# reachable from the lockfile graph" - a SUPERSET of what the build needs,
# which nothing can trip over. It is also not stable across npm versions:
# npm 10.8.2 (bundled with Node 20, which CI runs) reports the Worker
# workspace's OPTIONAL dev dependencies - `@emnapi/runtime`, `tslib` - as
# extraneous from the very lockfile npm 11 calls clean. Treating that as fatal
# made the embedded LOCAL server unlaunchable on Node 20 over packages it
# never loads.
_NON_FATAL_TREE_PROBLEM_PREFIXES = ("extraneous:",)


def _fatal_tree_problems(problems: object) -> list[str]:
    """The `npm ls` problems that must block a source build (default-deny)."""
    if not problems:
        return []
    if isinstance(problems, str):
        problems = [problems]
    if not isinstance(problems, (list, tuple)):
        # An unrecognized shape is not something to reason about - fail loudly
        # rather than silently treating an unknown report as "no problems".
        return [f"unrecognized npm ls problems payload: {problems!r}"]
    return [
        p for p in problems
        if not isinstance(p, str)
        or not p.strip().lower().startswith(_NON_FATAL_TREE_PROBLEM_PREFIXES)
    ]


def _dependency_tree(es_dir: Path) -> dict:
    # Scoped to match the install exactly (see _LOCAL_WORKSPACE_SCOPE).
    #
    # `allow_failure` because `npm ls` exits 1 whenever it reports ANY problem,
    # including the harmless classes enumerated above. Treating that exit as
    # fatal made `_fatal_tree_problems` unreachable - the extraneous allowance
    # could never actually run, because the raise happened first. The JSON
    # payload is the authority: `problems` is what gets classified,
    # default-deny, exactly as intended. A genuinely broken tree still fails,
    # one layer later and with a better message.
    result = _run_npm(
        ["npm", "ls", "--all", "--json", "--offline", *_LOCAL_WORKSPACE_SCOPE],
        es_dir,
        allow_failure=True,
    )
    try:
        tree = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"npm ls returned malformed JSON in {es_dir}: {exc}"
        ) from exc
    if not isinstance(tree, dict):
        raise RuntimeError(f"npm ls returned a non-object dependency tree in {es_dir}")
    problems = _fatal_tree_problems(tree.get("problems"))
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
    # Create the stamp file BEFORE measuring the tree, not after.
    #
    # npm keeps a hidden lockfile at `node_modules/.package-lock.json` and
    # decides whether it is still authoritative by comparing the `node_modules`
    # directory's mtime. Creating a file in that directory changes the mtime,
    # so npm stops trusting the hidden lockfile and starts reporting the tree
    # it actually finds on disk - at which point optional dependencies that
    # were never installed (esbuild's and vitest's other-platform binaries)
    # switch from full metadata to empty objects, and the digest changes.
    #
    # Writing the stamp last therefore recorded a digest that the very act of
    # writing it invalidated: the stamp could never match on a fresh install,
    # so every artifact rebuild paid a full reinstall it did not need. This is
    # long-standing (reproduced on main) and was simply never noticed, because
    # the penalty is invisible correctness-wise - just slow.
    #
    # Touching the file first spends that one-time perturbation up front. The
    # measurement then happens in the settled state, and rewriting the file's
    # CONTENTS below does not disturb it again: only creating, removing or
    # renaming an entry changes a directory's mtime.
    stamp_path = _dependency_stamp_path(es_dir)
    stamp_path.touch()
    stamp = {
        "installed_tree_sha256": event_server_artifact.canonical_json_digest(
            _dependency_tree(es_dir)
        ),
        "lockfile_sha256": event_server_artifact.file_sha256(
            es_dir / "package-lock.json"
        ),
        "schema_version": 1,
    }
    stamp_path.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n")


def _install_source_dependencies(es_dir: Path) -> None:
    # Clear the tree ourselves instead of leaving it to `npm ci`. npm 10 on
    # Node 20 fails its own cleanup with `ENOTEMPTY ... rmdir
    # node_modules/<pkg>` when a populated tree is already there, which is the
    # normal case here: this runs to REFRESH an install, not only to create
    # one. Latent until the Worker workspace gained its MCP dependencies and
    # the tree grew roughly fourfold; shutil.rmtree does not share the bug.
    #
    # Only the source path reaches this. An installed bobi validates a
    # prebuilt artifact (`_validate_packaged_artifact`) and never runs npm.
    shutil.rmtree(es_dir / "node_modules", ignore_errors=True)
    _run_npm(["npm", "ci", "--no-audit", "--no-fund", *_LOCAL_WORKSPACE_SCOPE], es_dir,
             timeout=900)
    _refresh_dependency_stamp(es_dir)
    # Say so where someone will actually hit it. This install is scoped, so the
    # Cloudflare Worker workspace is now absent - its typecheck and test suites
    # will fail on missing modules until it is restored. Cheaper to name the
    # one-line remedy here than to let it read as a broken checkout.
    log.info(
        "Installed the local event server's dependencies only (workspace root + "
        "core). The Cloudflare Worker workspace was not installed; run "
        "`npm ci` in %s if you are working on the Worker itself.",
        es_dir,
    )


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


def local_port_from_url(url: str) -> int | None:
    """The local event-server port named by *url*, or None when it is remote."""
    if not url:
        return None
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return None
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def local_port_file(project_path: Path | None) -> Path:
    """Where the local event server remembers the port it came up on.

    Resolves the path only — it does not create the state directory, so a
    reader cannot bring state into being just by asking where it lives.
    Writers go through ``atomic_write_text``, which creates the parent.
    """
    from bobi import paths

    return paths.state_path(project_path) / "event-server.port"


def resolve_local_port(project_path: Path) -> int:
    """Port this runtime's LOCAL event server uses.

    A live runtime's remembered start port wins, then the configured local
    ``event_server_url``, then a remembered port with no live pid, then the
    8080 default. The single definition, so `event-server status` and doctor
    can never disagree about which port to probe (D019).
    """
    from bobi import paths

    pid_file = paths.event_server_pid_path(project_path)
    port_file = local_port_file(project_path)

    def _remembered() -> int | None:
        try:
            return int(port_file.read_text().strip())
        except (OSError, ValueError):
            return None

    if pid_file.exists() and port_file.exists():
        port = _remembered()
        if port is not None:
            return port

    try:
        from bobi.config import Config
        configured = Config.load(project_path).event_server_url
    except Exception:
        configured = ""
    if configured:
        port = local_port_from_url(configured)
        if port is not None:
            return port

    if port_file.exists():
        port = _remembered()
        if port is not None:
            return port
    return 8080


def _run_npm(
    args: list[str],
    es_dir: Path,
    timeout: float = 300,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an npm command, surfacing its output on failure.

    npm failures here used to raise a bare CalledProcessError with the
    output captured but never shown — the real cause (e.g. ENOSPC)
    was invisible in manager.log.

    `timeout` is per-command because they are not comparable: `npm --version`
    answers instantly, while a cold `npm ci` of the whole workspace is minutes
    of network. One ceiling sized for the former silently caps the latter.
    """
    try:
        result = subprocess.run(
            args,
            cwd=str(es_dir),
            capture_output=True,
            env=event_server_artifact.sanitized_node_environment(),
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{args[0]} was not found while running {' '.join(args)} in {es_dir}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{' '.join(args)} timed out after {timeout:g}s in {es_dir}"
        ) from exc
    if result.returncode != 0 and not allow_failure:
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
            atomic_write_text(local_port_file(project_path), str(port))
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
    pid_file = paths.event_server_pid_path(project_path)

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
    # The runtime .env carries these unprefixed (the connector cards capture
    # them); the local server reads only BOBI_ES_*. WhatsApp (#656) needs them
    # for inbound webhook verification; Discord (#2) because the local server
    # holds the persistent inbound Gateway WebSocket and needs the bot
    # credential at boot. Adding the next channel's vars is one line here.
    for var in (
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_VERIFY_TOKEN",
        "DISCORD_BOT_TOKEN",
        "DISCORD_APPLICATION_ID",
        "DISCORD_MESSAGE_CONTENT",
    ):
        if env.get(var):
            env[f"BOBI_ES_{var}"] = env[var]
    if bind:
        env["BOBI_ES_BIND"] = bind
    if extra_env:
        env.update(extra_env)
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_PATH", None)
    env["WS_NO_BUFFER_UTIL"] = "1"
    env["WS_NO_UTF_8_VALIDATE"] = "1"

    bundle = es_dir / "dist" / event_server_artifact.BUNDLE_NAME
    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            [node, str(bundle)],
            stdout=lf, stderr=lf,
            env=env, start_new_session=True,
        )

    atomic_write_text(pid_file, str(proc.pid))
    # Record the bundle these bytes came from: an in-place bobi upgrade
    # overwrites it under the running node, and nothing else would notice (#928).
    launch_stamp.record_launch(project_path, launch_stamp.EVENT_SERVER,
                               proc.pid, artifact=bundle)

    for _ in range(30):
        time.sleep(0.5)
        if health(f"http://localhost:{port}"):
            atomic_write_text(local_port_file(project_path), str(port))
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

    def mark_unbacked(sub: str) -> None:
        """Record a topic with no resource grant.

        The four ways a topic ends up here (credential missing, registration
        did not back it, transport error, server denied) share one rule, and
        it lives here so a fifth cannot get it wrong: an unbacked topic is
        always reported, and is kept in the subscription set only when this
        call is not filtering.
        """
        unbacked.append(sub)
        if not filter_unauthorized:
            kept.append(sub)

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
                mark_unbacked(sub)
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
            mark_unbacked(sub)
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
            mark_unbacked(sub)
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
            mark_unbacked(sub)
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

    from bobi.events.state import (
        load_bubble_state, save_bubble_state, bubble_state_path,
    )

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
    from bobi.slack import SlackAppIdentityError, require_app_identity

    try:
        team_id, bot_id, bot_user_id, app_id = require_app_identity(token)
    except SlackAppIdentityError as e:
        log.warning("Slack workspace registration skipped: %s", e)
        return []
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
        resp = signed_request(
            base_url, "POST", "/slack/workspaces", record,
            bubble_id, bubble_key, timeout=10.0,
        )
        # signed_request does not raise on status, so an unchecked call logged
        # success for a registration the server rejected (D031): the
        # bubble-scoped outbound record (#487) and the slack resource grant
        # were never written, leaving self-reply loop prevention and outbound
        # sends silently broken. The whatsapp and discord siblings below check
        # this the same way.
        if resp.status_code != 200:
            log.warning(
                "Slack workspace registration rejected for %s (app %s): "
                "HTTP %d — self-reply loop prevention and outbound sends are "
                "NOT active for this workspace",
                team_id, app_id or "?", resp.status_code,
            )
            return []
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
