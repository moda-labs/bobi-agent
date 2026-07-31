"""Deployment identity - orchestrator-neutral by construction (issue #5).

The read-model key is ``{fleet, instance}``, both Bobi-stamped env set by
whatever provisions the instance (the Fly provisioner today, a K8s manifest /
Helm chart tomorrow). Platform facts (``platform``, ``machine``, ``region``,
``node``) are *optional enrichment* resolved best-effort by this adapter - the
ONLY place that knows platform-specific env var names. Missing enrichment is
null, never fatal: the key still holds.

Restart escalation is a non-zero process exit; each orchestrator's own restart
policy escalates (Fly machine restart / K8s CrashLoopBackOff / docker
``--restart``). Nothing here calls a platform API, so the sidecar is not
Fly-locked.
"""

from __future__ import annotations

import os
from pathlib import Path


def _first_env(*names: str) -> str | None:
    """First non-empty env var among ``names``, else None."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return None


def _detect_platform() -> str:
    """Best-effort orchestrator detection from ambient env."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "k8s"
    if os.environ.get("FLY_APP_NAME"):
        return "fly"
    # A container without either signal: plain `docker run` (or an unknown
    # runtime). We cannot prove docker specifically, so report "docker" only
    # when we can see the standard containerization hint, else "unknown".
    if os.path.exists("/.dockerenv"):
        return "docker"
    return "unknown"


def _resolve_instance(project_root: Path | None) -> str:
    """The tenant key. Mirrors the entrypoint's AGENT_NAME resolution order."""
    instance = _first_env("BOBI_INSTANCE", "BOBI_AGENT")
    if instance:
        return instance
    # Fall back to the run-root basename, exactly as docker-entrypoint.sh does
    # when neither BOBI_INSTANCE nor BOBI_AGENT is set.
    try:
        from bobi import paths
        return paths.agent_name_for_root(
            (project_root or paths.bobi_root()).resolve())
    except Exception:
        return "unknown"


def resolve_deployment_identity(project_root: Path | None = None) -> dict:
    """Resolve the stable deployment identity + best-effort platform enrichment.

    Returns a dict shaped for the heartbeat's ``deployment`` block:
    ``{fleet, instance, platform, machine, region, node}``. ``fleet`` and
    ``instance`` are always non-empty (the key); the four enrichment fields are
    ``None`` when unknown. Explicit ``BOBI_*`` overrides win on any platform so
    an operator on an unrecognized runtime can supply identity without a code
    change.
    """
    platform = _detect_platform()

    machine = _first_env("BOBI_MACHINE_ID")
    region = _first_env("BOBI_REGION")
    node = _first_env("BOBI_NODE")

    if platform == "fly":
        machine = machine or _first_env("FLY_MACHINE_ID")
        region = region or _first_env("FLY_REGION")
    elif platform == "k8s":
        # The operator wires these into the pod spec via the downward API.
        machine = machine or _first_env("POD_NAME")
        node = node or _first_env("NODE_NAME")

    return {
        "fleet": _first_env("BOBI_FLEET") or "default",
        "instance": _resolve_instance(project_root),
        "platform": platform,
        "machine": machine,
        "region": region,
        "node": node,
    }
