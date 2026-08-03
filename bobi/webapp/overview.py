"""What this agent IS, as opposed to what it has been doing.

The runs table and the status strip answer questions about activity. This
answers the other one a steward asks — *what did I build here?* — and it is the
page's read-only mirror of the team's composition: its roles, what it can
reach, what runs on its own, and which brain drives it.

Read-only is the whole posture. Composition is edited in setup, and this
surface exists so nobody has to open setup (or `agent.yaml`) to remember what a
team is. Every value here is derived from the installed package image, never
from a source directory: the runtime runs the image, so the image is the truth.

Two rules carry over from the rest of this page's reads:

- **Every read takes an explicit root.** One webapp process serves every team
  and binds none of them.
- **A value this machine cannot produce is empty, not invented.** An unreadable
  ``agent.yaml`` costs the fields it would have carried, not the response.
"""

from __future__ import annotations

from pathlib import Path

# Fallbacks for values that are configured by absence: an unset key means "the
# framework default", and the page should show what will actually happen rather
# than an empty cell.
from bobi.spend_governor import DEFAULT_CAP as DEFAULT_SPEND_CAP


def _description(root: Path) -> str:
    """The team's one-line description — the first prose paragraph of agent.md.

    The dashboard card's ``_describe`` truncates for a card; the header shows
    the paragraph whole, so this reuses the parser and not the trim.
    """
    from bobi import paths
    from bobi.webapp.runtime import _first_paragraph

    try:
        return _first_paragraph(
            (paths.package_dir(root) / "agent.md").read_text())
    except OSError:
        return ""


def _roles(root: Path) -> list[dict]:
    from bobi.prompts.resolver import discover_roles

    try:
        roles = discover_roles(project_path=root)
    except Exception:
        return []
    return sorted(
        ({"name": r.get("name", ""),
          "description": r.get("description", "")} for r in roles),
        key=lambda r: r["name"])


def _monitor_counts(root: Path) -> dict:
    """Scheduled automations, resolved exactly the way the scheduler resolves
    them.

    Counted through the registry, not ``agent.yaml``: what runs is the merge of
    framework defaults, the package's ``monitors.yaml`` and the team's own
    entries, so a count taken from any single layer would be wrong.

    And it is ``effective_monitors`` filtered by ``projects_for``, which is the
    pair the scheduler itself uses - because "disabled" means two different
    things in this registry. On a framework default it pauses the monitor; on a
    team's own entry it is an OPT-OUT of an inherited default, which never
    enters the project list at all and instead removes the global from this
    team. Counting ``all_monitors()`` by its ``enabled`` flag would report a
    default this team switched off as scheduled work.
    """
    from bobi.monitors.registry import MonitorRegistry

    try:
        registry = MonitorRegistry.load(project_path=root)
        every = registry.all_monitors()
        live = [m for m in registry.effective_monitors()
                if registry.projects_for(m)]
    except Exception:
        return {"scheduled": 0, "paused": 0}
    return {"scheduled": len(live), "paused": max(0, len(every) - len(live))}


def _workflow_count(root: Path) -> int:
    from bobi import paths

    d = paths.workflows_dir(root)
    if not d.exists():
        return 0
    return len([p for p in d.glob("*.y*ml") if p.is_file()])


def _brain(config) -> dict:
    """Which engine drives this team, with the defaults made explicit.

    ``kind`` falls back to the framework default because an unset brain still
    runs one; ``model`` and ``effort`` stay empty when unset, since "the
    provider's default" is not a name this layer gets to guess.
    """
    from bobi.brain import DEFAULT_BRAIN

    brain = config.brain or {}
    max_turns = brain.get("max_turns")
    return {
        "kind": config.brain_kind or DEFAULT_BRAIN,
        "model": config.brain_model,
        "effort": config.brain_effort,
        "max_turns": int(max_turns) if isinstance(max_turns, int) else 0,
        # A gateway endpoint is a fact about where the tokens go. The URL
        # itself is not shown - it can carry a key - only that one is set.
        "gateway": bool(config.brain_base_url),
    }


def _chat(config) -> dict:
    """Where a human talks to this team, and on which channels.

    The channel list lives on the chat service's own declaration, so an
    agent whose Slack app is scoped to two channels says so.
    """
    chat = config.chat or ""
    if not chat:
        return {"service": "", "channels": []}
    service = next((s for s in config.services if s.name == chat), None)
    return {"service": chat,
            "channels": list(service.channels) if service else []}


def build_overview(root: Path) -> dict:
    """The identity header's read-only view of a team.

    Shape::

        {"name", "description",
         "roles": [{"name", "description"}, ...],
         "chat": {"service", "channels"},
         "services": [{"name", "events", "required"}, ...],
         "automations": {"monitors", "paused_monitors", "workflows"},
         "brain": {"kind", "model", "effort", "max_turns", "gateway"},
         "spend_cap": {"value", "is_default"},
         "entry_role": str}
    """
    from bobi import paths
    from bobi.config import Config

    try:
        config = Config.load(root)
    except Exception:
        # A team whose agent.yaml will not parse still has a name and an
        # agent.md. Returning what is readable beats a 500 on a page whose
        # entire job is telling you the agent is in trouble.
        config = Config()

    monitors = _monitor_counts(root)
    return {
        "name": paths.agent_name_for_root(root),
        "description": _description(root),
        "roles": _roles(root),
        "chat": _chat(config),
        "services": [{"name": s.name, "events": bool(s.events),
                      "required": bool(s.required)} for s in config.services],
        "automations": {
            "monitors": monitors["scheduled"],
            "paused_monitors": monitors["paused"],
            "workflows": _workflow_count(root),
        },
        "brain": _brain(config),
        "spend_cap": {
            "value": config.spend_cap or DEFAULT_SPEND_CAP,
            "is_default": not config.spend_cap,
        },
        "entry_role": config.entry_role,
    }
