"""Resource attributes stamped on every agent-authored emission.

This is the reason the feature belongs in bobi rather than in a prompt
snippet: an agent can supply the observation, but it cannot reliably supply
the fleet identity that makes the observation sliceable. Mapped onto OTel
semantic conventions wherever one exists.
"""

from __future__ import annotations

from pathlib import Path

from bobi.otel.config import RESOURCE_ATTRIBUTES_VAR, SERVICE_NAME_VAR

DEFAULT_SERVICE_NAME = "bobi"

# Identity field -> semconv (or bobi-namespaced) attribute key. All four are
# best-effort enrichment and are omitted when the resolver returns None.
_ENRICHMENT_KEYS = {
    "machine": "host.id",
    "region": "cloud.region",
    "node": "k8s.node.name",
}


def _parse_resource_attributes(raw: str) -> dict[str, str]:
    """Parse ``OTEL_RESOURCE_ATTRIBUTES`` (``k=v,k=v``)."""
    attrs: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            attrs[key] = value.strip()
    return attrs


def _team_name(root: Path) -> str:
    """The installed package's ``agent:`` key, or "" when absent."""
    from bobi import config as _config

    try:
        return (_config.Config.load(root).agent or "").strip()
    except Exception:
        return ""


def resource_attributes(root: Path, agent: str) -> dict[str, str]:
    """Build the resource attribute set for this runtime.

    ``OTEL_RESOURCE_ATTRIBUTES`` is merged in first so bobi's own identity
    attributes win on conflict - the labels the feature exists to make
    trustworthy are not overridable from the environment the agent can reach.

    ``service.instance.id`` and ``bobi.agent`` are usually the same string
    (``_resolve_instance`` falls back through ``BOBI_INSTANCE`` -> ``BOBI_AGENT``
    -> run-root basename). That is expected: the former is the semconv key
    external tooling looks for, the latter is bobi's own dimension.
    """
    from bobi import config as _config
    from bobi.__version__ import __version__
    from bobi.identity import resolve_deployment_identity

    env = _config.project_env(root)
    attrs: dict[str, str] = {}

    raw_extra = (env.get(RESOURCE_ATTRIBUTES_VAR) or "").strip()
    if raw_extra:
        attrs.update(_parse_resource_attributes(raw_extra))

    identity = resolve_deployment_identity(root)

    attrs["service.name"] = (env.get(SERVICE_NAME_VAR) or "").strip() or DEFAULT_SERVICE_NAME
    attrs["service.version"] = __version__
    attrs["service.instance.id"] = identity["instance"]
    attrs["bobi.fleet"] = identity["fleet"]
    attrs["bobi.platform"] = identity["platform"]
    attrs["bobi.agent"] = agent

    team = _team_name(root)
    if team:
        attrs["bobi.team"] = team

    for field_name, key in _ENRICHMENT_KEYS.items():
        value = identity.get(field_name)
        if value:
            attrs[key] = value

    return attrs
