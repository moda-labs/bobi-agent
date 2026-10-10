"""Auto-discover event subscriptions from the environment."""

import logging
from pathlib import Path

import yaml

from bobi.events.adapters import detect

log = logging.getLogger(__name__)


def _normalize_explicit_subscriptions(value) -> list[str]:
    """Return non-empty string subscription keys from a YAML subscribe value."""
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    return [item.strip() for item in candidates
            if isinstance(item, str) and item.strip()]


# Topic namespaces a workspace file may never declare. ``inbox/<name>`` and
# ``reply/<name>`` are per-session private channels: the event server accepts any
# non-empty string at registration and treats ``inbox/`` as an ordinary
# bubble-scoped topic, so a hand-written ``inbox/<worker>`` would be composed and
# PUT on the next reload and would route that worker's private messages to the
# manager. That is the 2026-06-12 cross-session leak class (see
# ``bobi/events/state.py`` and ``tests/integration/test_event_isolation.py``) and
# it needs no resource grant to reach, so the ban lives in the READER rather than
# only in the CLI's argument check.
FORBIDDEN_TOPIC_PREFIXES = ("inbox/", "reply/")

WORKSPACE_SUBSCRIPTIONS_FILE = "subscriptions.yaml"

WORKSPACE_SUBSCRIPTIONS_HEADER = """\
# Event topics this team subscribes to.
# Created by `bobi agent <name> subscriptions add`, seeded from the agent pack.
# Edit with `bobi agent <name> subscriptions add|remove`, which applies the
# change to the running event server without a restart.
# Comments below this header are not preserved across an edit.
# A direct edit of this file is not serialized against a running command and
# can be overwritten by one; it also applies only at the next reload or start.
"""


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader that REJECTS a duplicate mapping key.

    `yaml.safe_load` silently keeps the last one, so

        subscribe:
          - github:o/keep
        subscribe: []

    would parse as an authoritative "subscribe to nothing" and unsubscribe the
    team from everything. That is precisely the fail-closed hazard this
    validator exists for, and it is one more reason the permissive pack parser
    is not reused here.
    """


def _no_duplicate_keys(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


class WorkspaceSubscriptionsError(Exception):
    """``workspace/subscriptions.yaml`` exists but is not a valid document.

    Carries the file *path* and the *reason* so every reader can name the file
    an operator has to fix. Deliberately NOT swallowed into auto-detection:
    under this design ``[]`` is an authoritative "subscribe to nothing", so a
    truncated or mistyped file must fail loud instead of silently collapsing a
    team's topics to whatever git remotes happen to be configured.
    """

    def __init__(self, path: Path, reason: str):
        self.path = Path(path)
        self.reason = reason
        super().__init__(f"{self.path}: {reason}")


def workspace_subscriptions_path(project_path: Path) -> Path:
    """Where a runtime's operator-owned subscription list lives."""
    from bobi import paths

    return paths.workspace_dir(project_path) / WORKSPACE_SUBSCRIPTIONS_FILE


def validate_topic(topic, *, where: str = "topic") -> str:
    """Return *topic* stripped, or raise ``ValueError`` naming the fault.

    The ONE definition of what a declarable topic is, shared by the workspace
    reader and by the CLI's argument check, so the CLI can never write a file
    its own reader rejects and thereby fail every later apply.
    """
    if not isinstance(topic, str):
        raise ValueError(f"{where} is not a string (got {type(topic).__name__})")
    stripped = topic.strip()
    if not stripped:
        raise ValueError(f"{where} is empty")
    for prefix in FORBIDDEN_TOPIC_PREFIXES:
        if stripped.startswith(prefix):
            raise ValueError(
                f"{where} {stripped!r} is in the reserved {prefix!r} namespace; "
                "per-session inbox and reply topics are derived automatically "
                "and declaring one would route another session's private "
                "messages here"
            )
    return stripped


def workspace_subscriptions(project_path: Path) -> list[str] | None:
    """Return the operator-declared topic list, or ``None`` when absent.

    ``None`` means no file: every reader then falls through to the pack
    ``subscribe:`` and auto-detection exactly as before this layer existed.
    ``[]`` means the operator declares nothing and is returned as such.

    Anything else that is not a valid document raises
    :class:`WorkspaceSubscriptionsError`. This validator is deliberately NOT
    the permissive pack parser: ``_normalize_explicit_subscriptions`` maps six
    distinct wrong inputs to ``[]``, which is safe behind a read-only
    ``package/`` and unsafe for a writable ``workspace/`` file where ``[]`` is
    authoritative.
    """
    path = workspace_subscriptions_path(project_path)
    try:
        raw_text = path.read_text()
    except FileNotFoundError:
        return None
    except IsADirectoryError as exc:
        raise WorkspaceSubscriptionsError(path, "is a directory, not a file") from exc

    try:
        doc = yaml.load(raw_text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise WorkspaceSubscriptionsError(path, f"is not valid YAML: {exc}") from exc

    if doc is None:
        raise WorkspaceSubscriptionsError(
            path, "is empty; write `subscribe: []` to declare no topics")
    if not isinstance(doc, dict):
        raise WorkspaceSubscriptionsError(
            path,
            f"document root is a {type(doc).__name__}, not a mapping with a "
            "`subscribe:` key")

    unknown = [k for k in doc if k != "subscribe"]
    if unknown:
        raise WorkspaceSubscriptionsError(
            path,
            f"has unknown top-level key(s) {sorted(map(str, unknown))}; "
            "only `subscribe:` is allowed")
    if "subscribe" not in doc:
        raise WorkspaceSubscriptionsError(path, "has no `subscribe:` key")

    value = doc["subscribe"]
    if not isinstance(value, list):
        raise WorkspaceSubscriptionsError(
            path,
            f"`subscribe:` is a {type(value).__name__}, not a list of topics")

    topics: list[str] = []
    for index, item in enumerate(value):
        try:
            topic = validate_topic(item, where=f"`subscribe:` item {index}")
        except ValueError as exc:
            raise WorkspaceSubscriptionsError(path, str(exc)) from exc
        if topic in topics:
            raise WorkspaceSubscriptionsError(
                path, f"declares duplicate topic {topic!r}")
        topics.append(topic)
    return topics


def sanitize_seed_topics(topics) -> tuple[list[str], list[str]]:
    """Return (usable topics, dropped topics) for a list the operator did not type.

    The pack `subscribe:` parser is permissive: it tolerates a duplicate and an
    `inbox/`/`reply/` topic, both of which this file's STRICT reader rejects. The
    first `subscriptions add` seeds from that list, so without this the CLI could
    write a document its own reader fails on - and `remove` could not repair it,
    because the argument check refuses to name a forbidden topic.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for raw in topics:
        try:
            topic = validate_topic(raw, where="seeded topic")
        except ValueError:
            dropped.append(str(raw))
            continue
        if topic in kept:
            dropped.append(topic)
            continue
        kept.append(topic)
    return kept, dropped


def render_workspace_subscriptions(topics: list[str]) -> str:
    """Serialize *topics* as the workspace file's full text.

    A fixed header constant plus ``yaml.safe_dump``: only PyYAML is available
    and ``safe_dump`` discards comments, which the header says out loud rather
    than working around with line splicing.
    """
    return WORKSPACE_SUBSCRIPTIONS_HEADER + yaml.safe_dump(
        {"subscribe": list(topics)}, default_flow_style=False, sort_keys=False)


def explicit_subscriptions(project_path: Path) -> list[str]:
    """Return agent.yaml's explicit `subscribe:` keys, env-interpolated.

    The ONE parser for the `subscribe:` surface (D078). `discover_subscriptions`
    reads it as its highest-precedence source and `bobi.ingress` reads it to
    decide which topics a reachability warning must cover; when each had its own
    copy, a schema change could make the warning disagree with what the session
    actually subscribes to.

    Errors are NOT swallowed here — the two callers want different things from a
    malformed agent.yaml, so each decides for itself.

    ``workspace/subscriptions.yaml`` is consulted FIRST and wins when present
    (#952): the pack list is only the seed. Putting the workspace layer here
    rather than in ``discover_subscriptions`` is what keeps the D078 one-parser
    invariant: ``bobi.ingress``'s reachability warning and ``bobi doctor``
    follow the operator's list for free instead of naming the pack's.
    """
    from bobi import paths
    from bobi.config import _interpolate_env, project_env

    declared = workspace_subscriptions(project_path)
    if declared is not None:
        return declared

    agent_yaml = paths.agent_yaml_path(project_path)
    if not agent_yaml.exists():
        return []
    raw = yaml.safe_load(agent_yaml.read_text()) or {}
    if not isinstance(raw, dict):
        return []
    return _normalize_explicit_subscriptions(
        _interpolate_env(raw.get("subscribe", []), project_env(project_path))
    )


def discover_subscriptions(project_path: Path) -> list[str]:
    """Build subscription keys by auto-detecting event sources.

    Resolution order:
    1. workspace/subscriptions.yaml subscribe list (operator-owned, #952)
    2. agent.yaml subscribe list (explicit override / the seed)
    3. agent.yaml services with events: true (adapters auto-detect keys)
    4. Fallback to project directory name
    """
    # Read BEFORE and OUTSIDE the try below. Inside it, an invalid workspace
    # file would be swallowed into auto-detection, which for this team
    # collapses four GitHub topics to one and promotes a channel-scoped Slack
    # subscription to workspace-wide. Outside it, the apply fails loud with the
    # file path, while an invalid PACK agent.yaml keeps its historical
    # fall-through (pinned by tests/test_ingress.py).
    declared = workspace_subscriptions(project_path)
    if declared is not None:
        return declared

    try:
        explicit = explicit_subscriptions(project_path)
        if explicit:
            return explicit
    except WorkspaceSubscriptionsError:
        # Never swallowed. The file is read twice - once above, and again inside
        # `explicit_subscriptions`, which this call site must keep using per the
        # D078 one-parser pin - so a hand edit landing between the two reads
        # would otherwise reach the fall-through below.
        raise
    except Exception:
        # An unreadable agent.yaml falls through to auto-detection rather than
        # failing the whole discovery — the pre-consolidation behavior here.
        pass

    from bobi.config import Config
    cfg = Config.load(project_path)
    if cfg.event_services:
        subs = []
        for svc in cfg.event_services:
            keys = detect(svc.name, project_path, cfg)
            subs.extend(keys)
        if subs:
            return subs

    return [project_path.name]


# Sub-agent lifecycle topics the persistent entry point must hear so a detached
# agent's completion/failure is delivered back to the launcher instead of being
# emitted into the void (MDS-65 RC#1). _emit_session_finished already POSTs these
# carrying requested_by; nothing subscribed to them before.
LIFECYCLE_EVENTS = ("agent/session.completed", "agent/session.failed")


def lifecycle_subscription_keys() -> list[str]:
    """Topics the entry point subscribes to so sub-agent completions reach it.

    Mirrors ``monitor_subscription_keys``: returns BOTH the bare delivered type
    (``session.completed``) and the source-qualified topic
    (``agent/session.completed``). Current servers route a posted event onto both
    forms; older servers (pre-#235 topic contract) deliver only the bare type, so
    subscribing to both keeps delivery working across server versions.
    ``deliver()`` dedupes across matched topics, so the double subscription never
    double-delivers.
    """
    keys: list[str] = []
    for event in LIFECYCLE_EVENTS:
        delivered_topic = event.split("/", 1)[1] if "/" in event else event
        for key in (delivered_topic, event):
            if key not in keys:
                keys.append(key)
    return keys


def monitor_subscription_keys(monitor_events: list[str]) -> list[str]:
    """Topics the manager must subscribe to so monitor findings get delivered.

    The scheduler publishes every monitor finding through
    ``events.publish.post_event(monitor.event, ...)``, which splits the event
    on the first ``/`` and POSTs the *type* to ``/events/<type>`` with the
    source in the body. Current event servers route that onto BOTH the bare
    type (``support.email``) and the source-qualified topic
    (``monitor/support.email``) — see ``createTopicEvent``.

    Both forms are returned anyway: older deployed servers (pre-#235 topic
    contract) deliver only on the bare type, so subscribing to both keeps the
    manager working across server versions. ``deliver()`` dedupes deployments
    across matched topics, so a double subscription never double-delivers.
    """
    keys: list[str] = []
    for event in monitor_events:
        if not event:
            continue
        delivered_topic = event.split("/", 1)[1] if "/" in event else event
        for key in (delivered_topic, event):
            if key not in keys:
                keys.append(key)
    return keys


def compose_session_subscriptions(
    project_path: Path, session: str, extra=()
) -> list[str]:
    """Build the full topic list a session subscribes to.

    The ONE composition site (#952). Previously split across
    ``bobi/service.py`` (workspace/pack + monitor + lifecycle layers, composed
    before the session existed) and ``bobi/session.py`` (the ``inbox/<self>``
    prepend). Composing inside the session is what lets a running manager
    re-derive its own set on a reload instead of replaying a boot-time snapshot.

    Every session gets ``inbox/<session>`` plus its own *extra* topics:
    ``bobi agent <name> spawn --subscribe`` is a supported way to launch a
    persistent worker on an external topic. The declared, monitor and lifecycle
    layers are the MANAGER's alone: composing them for a worker would push the
    manager's ``github:``/``slack:``/``linear:`` topics onto that worker's own
    deployment, which is the 2026-06-12 incident where project leads received
    the user's Slack DMs to the director.
    """
    keys: list[str] = [f"inbox/{session}"]

    def _extend(candidates) -> None:
        for key in candidates:
            if key not in keys:
                keys.append(key)

    from bobi.service import manager_session_name

    is_manager = session == manager_session_name(project_path)
    # Layer order is the pre-#952 order exactly: declared, extras, monitors,
    # lifecycle. The server treats `replace` as a set, but keeping the order
    # makes the move behaviour-preserving by construction.
    if is_manager:
        _extend(discover_subscriptions(project_path))
    _extend(extra or ())
    if not is_manager:
        return keys

    from bobi.monitors.registry import MonitorRegistry

    monitor_events = [
        m.event
        for m in MonitorRegistry.load(project_path=project_path).effective_monitors()
    ]
    _extend(monitor_subscription_keys(monitor_events))
    _extend(lifecycle_subscription_keys())
    return keys
