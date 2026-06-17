"""Open mode — load an existing team's source into the editor.

Create and Open converge on the same chat + cards editor. Open **reverse-fills**
the spec from an existing pack so the cards show what's already there; the user
then edits via the conversation, and the source is re-authored at the chosen
location and installed at Finish. Best-effort and defensive — a malformed pack
should degrade to blank cards, never crash setup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from modastack.setup.state import SPEC_SLOTS, SetupState


def is_team(d: Path) -> bool:
    """A directory is a team source if it holds an agent.yaml."""
    return d.is_dir() and (d / "agent.yaml").is_file()


def _team_display_name(d: Path) -> str:
    """The team's declared name (agent.yaml `agent:`), falling back to the
    folder name — so a team in `modastack/` shows its real name, not 'modastack'."""
    try:
        cfg = yaml.safe_load((d / "agent.yaml").read_text()) or {}
        if isinstance(cfg, dict) and cfg.get("agent"):
            return str(cfg["agent"])
    except Exception:
        pass
    return d.name


def _team_description(d: Path) -> str:
    """A one-line description for a team card — the first prose paragraph of the
    team's agent.md (the same text reverse_fill seeds the goal from)."""
    try:
        return _first_paragraph((d / "agent.md").read_text())[:160]
    except Exception:
        return ""


def list_teams_in(scan_dir: Path) -> list[dict]:
    """Editable team sources found in `scan_dir` — every folder with an
    agent.yaml. The directory itself may be a team (the create default writes
    the team straight into its own folder) or a container of team folders, so
    both are checked. Paths are returned absolute, since a team source can live
    anywhere the user points the scan at (the `~/modastack-agents` library by
    default, a project repo, wherever). Best-effort — a missing or unreadable
    directory simply yields nothing."""
    teams: list[dict] = []
    seen: set[str] = set()

    def _add(d: Path) -> None:
        try:
            key = str(d.resolve())
        except OSError:
            return
        if not is_team(d) or key in seen:
            return
        seen.add(key)
        teams.append({"name": _team_display_name(d), "path": key,
                      "description": _team_description(d)})

    if not scan_dir.is_dir():
        return teams
    _add(scan_dir)                        # the folder itself may be a team
    try:
        children = sorted(scan_dir.iterdir())
    except OSError:
        children = []
    for d in children:                    # …or it may contain team folders
        if d.is_dir():
            _add(d)
    return teams


def list_registry_teams(project: Path) -> list[dict]:
    """Agent teams available to download from configured registries, plus any
    already in the project cache. Network-backed and best-effort — a registry
    that's unreachable simply contributes nothing, never an error."""
    from modastack import registry
    teams: dict[str, dict] = {}
    try:
        for p in registry.list_remote(project):
            name = p.get("name")
            if name:
                teams[name] = {"name": name,
                               "description": p.get("description", ""),
                               "registry": p.get("registry", "")}
    except Exception:
        pass
    try:
        for p in registry.list_cached(project):
            name = p.get("name")
            if name and name not in teams:
                teams[name] = {"name": name,
                               "description": p.get("description", ""),
                               "registry": "cached"}
    except Exception:
        pass
    return [teams[k] for k in sorted(teams)]


def fetch_into(project: Path, name: str, dest: Path) -> None:
    """Download a registry team into `dest` (a user-chosen working location).
    Raises RuntimeError if the team can't be found/fetched."""
    from modastack import registry
    cached = registry.fetch(project, name)
    copy_into(cached, dest)


def _first_paragraph(md: str) -> str:
    """The first prose paragraph of a markdown doc (skips headings)."""
    para: list[str] = []
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("#"):
            if para:
                break
            continue
        if not s:
            if para:
                break
            continue
        para.append(s)
    return " ".join(para)


def reverse_fill(state: SetupState, source: Path) -> None:
    """Populate the spec (goal/roles/services/autonomous), team_name and chat
    from an existing pack so the editor cards reflect it. Never raises."""
    try:
        cfg = yaml.safe_load((source / "agent.yaml").read_text()) or {}
    except Exception:
        cfg = {}
    spec = state.spec
    state.team_name = cfg.get("agent") or source.name

    agent_md = source / "agent.md"
    if agent_md.is_file():
        spec.goal = _first_paragraph(agent_md.read_text())[:300]
    if not spec.goal:
        spec.goal = f"Edit the {state.team_name} team."

    roles: list[dict] = []
    roles_dir = source / "roles"
    if roles_dir.is_dir():
        for d in sorted(roles_dir.iterdir()):
            rm = d / "ROLE.md"
            if d.is_dir() and rm.is_file():
                roles.append({"name": d.name,
                              "responsibility": _first_paragraph(rm.read_text())[:140]})
    spec.roles = roles

    svcs: list[dict] = []
    for s in cfg.get("services", []) or []:
        name = s.get("name") if isinstance(s, dict) else str(s)
        if name and name != "slack":   # slack-as-chat is reflected via chat
            svcs.append({"name": name})
    spec.services = svcs

    state.chat = cfg.get("chat") or "cli"

    behaviors: list[dict] = []
    mon_dir = source / "monitors"
    if mon_dir.is_dir():
        for f in sorted(list(mon_dir.glob("*.yaml")) + list(mon_dir.glob("*.yml"))):
            if f.stem == "agent":
                continue
            behaviors.append({"description": f.stem.replace("-", " "),
                              "leash": "notify", "cadence": ""})
    spec.autonomous = behaviors
    spec.autonomous_confirmed = True

    # An existing team is already coherent: mark every slot ready so the cards
    # show as gathered and Finish is reachable right away.
    spec.readiness = {s: "enough" for s in SPEC_SLOTS}
    state.summary = f"Editing the existing team '{state.team_name}'."

    # Open the conversation by telling the user what the team already does,
    # instead of the blank "tell me what you want to build" greeting — they're
    # editing, not starting from scratch.
    state.messages = [{"role": "assistant", "content": _intro_summary(state)}]


def _intro_summary(state: SetupState) -> str:
    """A short, deterministic recap of an existing team to open the edit chat."""
    spec = state.spec
    parts = [f"You're editing {state.team_name}."]
    if spec.goal:
        parts.append(f"What it does: {spec.goal.rstrip('.')}.")
    role_names = [r.get("name") for r in spec.roles
                  if isinstance(r, dict) and r.get("name")]
    if role_names:
        parts.append("Roles: " + ", ".join(role_names) + ".")
    svc_names = [s.get("name") if isinstance(s, dict) else str(s)
                 for s in spec.services]
    svc_names = [n for n in svc_names if n]
    if svc_names:
        parts.append("It connects to " + ", ".join(svc_names) + ".")
    if spec.autonomous:
        n = len(spec.autonomous)
        parts.append(f"It runs {n} automation{'s' if n != 1 else ''} on its own.")
    if state.chat == "slack":
        parts.append("You talk to it in Slack.")
    elif state.chat == "telegram":
        parts.append("You talk to it in Telegram.")
    elif state.chat:
        parts.append("You talk to it from the command line.")
    parts.append("Tell me what you'd like to change — tweak the goal, add a "
                 "role or automation, or wire up another service.")
    return " ".join(parts)


def copy_into(source: Path, dest: Path) -> None:
    """Copy a team source into the working location (no-op if it's the same
    folder). The original is left untouched until Install."""
    source, dest = source.resolve(), dest.resolve()
    if source == dest:
        return
    if source in dest.parents:
        raise ValueError("the working location can't be inside the team's own "
                         "folder — pick a different folder to fork into")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
