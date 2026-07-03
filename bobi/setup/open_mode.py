"""Open mode — load an existing team's source into the editor.

Create and Open converge on the same chat + cards editor. Open **reverse-fills**
the spec from an existing pack so the cards show what's already there; the user
then edits via the conversation, and the source is re-authored at the chosen
location and installed at Finish. Best-effort and defensive — a malformed pack
should degrade to blank cards, never crash setup.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from bobi.setup.state import SPEC_SLOTS, SetupState


def is_team(d: Path) -> bool:
    """A directory is a team source if it holds an agent.yaml."""
    return d.is_dir() and (d / "agent.yaml").is_file()


def _team_display_name(d: Path) -> str:
    """The team's declared name (agent.yaml `agent:`), falling back to the
    folder name — so a team in `bobi/` shows its real name, not 'bobi'."""
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
    anywhere the user points the scan at. Best-effort — a missing or unreadable
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
            _add(d / "src")               # canonical Bobi Agent slot shape
    return teams


def _bundled_templates_dir() -> Path | None:
    """The repo's `agents/` dir, when running from a source checkout — a dev
    convenience so `bobi setup` lists local teams without the registry.

    Agent teams are NOT bundled into the wheel (they're versioned registry
    packages, #440/#446), so a real install has no local templates dir and this
    returns None — setup lists teams from the registry instead. Returns the
    `agents/` dir only if it actually holds team folders."""
    import bobi
    pkg = Path(bobi.__file__).resolve().parent
    cand = pkg.parent / "agents"
    try:
        if cand.is_dir() and any((c / "agent.yaml").is_file()
                                 for c in cand.iterdir() if c.is_dir()):
            return cand
    except OSError:
        pass
    return None


def list_bundled_templates() -> list[dict]:
    """Local starter teams from a source checkout's `agents/` dir, offered by the
    setup intro as a dev convenience (empty in a real wheel install — teams come
    from the registry there). Each is tagged official + bundled and carries a
    local `path`, so selecting it copies from disk instead of hitting the
    network."""
    d = _bundled_templates_dir()
    if d is None:
        return []
    out: list[dict] = []
    try:
        children = sorted(d.iterdir())
    except OSError:
        return []
    for sub in children:
        if not sub.is_dir() or not is_team(sub):
            continue
        out.append({"name": sub.name,
                    "description": _team_description(sub),
                    "path": str(sub.resolve()),
                    "official": True,
                    "bundled": True})
    return out


def list_registry_teams(project: Path) -> list[dict]:
    """Agent teams available to start from: the starter templates bundled with
    bobi (always, offline), plus any from configured registries and the
    project cache. Network-backed sources are best-effort — an unreachable
    registry simply contributes nothing, never an error."""
    from bobi import registry
    teams: dict[str, dict] = {}
    # Bundled starter templates first, so the intro always has a few options.
    for t in list_bundled_templates():
        teams[t["name"]] = t
    try:
        for p in registry.list_remote(project):
            name = p.get("name")
            if name and name not in teams:
                repo = p.get("registry", "")
                teams[name] = {"name": name,
                               "description": p.get("description", ""),
                               "registry": repo,
                               # "Official" = shipped from the canonical bobi
                               # registry, as opposed to a user-added one.
                               "official": repo == registry.DEFAULT_REPO}
    except Exception:
        pass
    try:
        for p in registry.list_cached(project):
            name = p.get("name")
            if name and name not in teams:
                teams[name] = {"name": name,
                               "description": p.get("description", ""),
                               "registry": "cached",
                               "official": False}
    except Exception:
        pass
    return [teams[k] for k in sorted(teams)]


def fetch_into(project: Path, name: str, dest: Path) -> None:
    """Materialize a template into `dest` (a user-chosen working location).
    Bundled starter templates copy from local disk (offline); anything else
    downloads from a configured registry. Raises if it can't be found/fetched."""
    for t in list_bundled_templates():
        if t["name"] == name:
            copy_into(Path(t["path"]), dest)
            return
    from bobi import registry
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


_CRED_VAR_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


def _declared_credential_vars(service: object) -> dict[str, str]:
    """{credential_key: VAR} for a service block's `credentials:` ${VAR}
    references (e.g. {"token": "GH_TOKEN"}). Non-reference values (inline
    literals, malformed refs) are skipped."""
    if not isinstance(service, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in (service.get("credentials") or {}).items():
        m = _CRED_VAR_RE.match(str(value).strip())
        if m:
            out[str(key)] = m.group(1)
    return out


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
            entry: dict = {"name": name}
            declared = _declared_credential_vars(s)
            if declared:
                # The pack's ${VAR} names are authoritative for credential
                # capture — the Connect cards must speak them, not the
                # connector catalog's authoring defaults.
                entry["credential_vars"] = declared
            svcs.append(entry)
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
