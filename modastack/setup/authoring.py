"""Build — the pour. Author a scratch pack's files from the spec.

Route-then-author: the conversation has filled the four-slot spec; now the
**wizard computes the structure** (which files exist, the entry point, the
services block, the monitor records) and the **LLM authors the prose**
(agent.md and each ROLE.md). The file list and entry_point are never
LLM-decided — that's the lock. Deterministic files are written verbatim;
prose files stream token-by-token to disk (the "pour") and are normalized
once at the end.

`author_pack` is an async generator of pour events for the UI:
  {"type": "file_start", "path": ...}
  {"type": "delta", "path": ..., "text": ...}
  {"type": "file_end", "path": ...}
It writes the pack source to `agents/<team_name>/` as it goes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from modastack.setup import llm, services
from modastack.setup.state import SetupState


def cadence_to_interval(cadence: str) -> str:
    """A monitor `interval:` from a free-form cadence — the cadence verbatim
    if it parses as an interval (e.g. '15m', '1d'), else a sane default. An
    event-shaped cadence ('when a PR opens') falls back to the default."""
    from modastack.monitors.schema import parse_interval
    try:
        parse_interval(cadence)
        return cadence.strip()
    except (ValueError, TypeError):
        return "15m"

# Credential key names for native services in agent.yaml (matches the
# reference eng-team pack); anything else falls back to a generic "token".
_CRED_KEY = {"slack": "bot_token", "linear": "api_key"}


def slug(text: str) -> str:
    """A lowercase, dash-separated slug safe for a directory / pack name."""
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return s[:64]


# --- spec normalization --------------------------------------------------

def normalized_roles(state: SetupState) -> list[dict]:
    """The spec's roles, guaranteed at least one. A goal-only team gets a
    single 'assistant' role responsible for the whole goal."""
    out = []
    for r in state.spec.roles:
        name = (r.get("name") if isinstance(r, dict) else str(r)) or ""
        if not name.strip():
            continue
        out.append({"name": name.strip(),
                    "responsibility": (r.get("responsibility", "")
                                       if isinstance(r, dict) else "")})
    if not out:
        out.append({"name": "assistant",
                    "responsibility": state.spec.goal or "Carry out the team's work."})
    return out


def compute_entry_point(state: SetupState) -> str:
    """The entry-point role slug — the first role. Deterministic, never LLM."""
    return slug(normalized_roles(state)[0]["name"]) or "assistant"


def derive_team_name(state: SetupState) -> str:
    """A valid pack slug when none was set — from the goal, else a default."""
    if state.team_name:
        return state.team_name
    words = " ".join((state.spec.goal or "").split()[:4])
    return slug(words) or "agent-team"


# --- deterministic file bodies -------------------------------------------

def build_service_records(state: SetupState, catalog=None) -> list[dict]:
    """The agent.yaml `services:` block from the spec. The chat channel you
    talk to the team through is itself a service it must connect (Slack); CLI
    needs nothing. (Telegram: framework support pending.) Custom services
    (neither native nor on Venn) carry their own API-key credential."""
    service_names = [(s.get("name") if isinstance(s, dict) else str(s))
                     for s in state.spec.services]
    if state.chat == "slack":
        service_names.append("slack")
    svcs: list[dict] = []
    seen: set[str] = set()
    for name in service_names:
        if not (name or "").strip():
            continue
        conn = services.resolve(name, venn_catalog=catalog)
        if conn.key in seen:
            continue
        seen.add(conn.key)
        rec: dict = {"name": conn.key}
        if conn.kind == "native":
            rec["events"] = True
            if conn.credential_var:
                key = _CRED_KEY.get(conn.key, "token")
                rec["credentials"] = {key: f"${{{conn.credential_var}}}"}
        elif conn.kind == "custom" and conn.credential_var:
            rec["credentials"] = {"api_key": f"${{{conn.credential_var}}}"}
        svcs.append(rec)
    return svcs


def has_venn_services(state: SetupState, catalog=None) -> bool:
    """Whether the team uses any Venn-backed service — those reach the world
    through the shared VENN_API_KEY, so agent.yaml must declare it."""
    names = [(s.get("name") if isinstance(s, dict) else str(s))
             for s in state.spec.services]
    return any((n or "").strip()
               and services.resolve(n, venn_catalog=catalog).kind == "venn"
               for n in names)


def build_agent_cfg(state: SetupState, catalog=None) -> dict:
    cfg: dict = {
        "agent": derive_team_name(state),
        "version": "0.1.0",
        "entry_point": compute_entry_point(state),
    }
    svcs = build_service_records(state, catalog)
    if svcs:
        cfg["services"] = svcs
    # Venn-backed services authenticate with the one shared key — declare it so
    # `modastack start` resolves it from the environment / .env (else preflight
    # reports "venn — no API key" despite the key being set).
    if has_venn_services(state, catalog):
        cfg["venn_api_key"] = "${VENN_API_KEY}"
    if state.chat and state.chat != "cli":
        cfg["chat"] = state.chat
    return cfg


def build_agent_yaml(state: SetupState, catalog=None) -> str:
    return yaml.dump(build_agent_cfg(state, catalog), sort_keys=False)


def merge_agent_yaml(existing_text: str, state: SetupState, catalog=None) -> str:
    """Non-lossy agent.yaml update for open/modify: overlay the keys setup
    manages (entry_point, chat) onto the existing config and UNION the
    services by name — never drop a service or a hand-written key the pack
    already carries (custom workflows refs, context, richer credentials)."""
    try:
        cfg = yaml.safe_load(existing_text) or {}
    except yaml.YAMLError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    managed = build_agent_cfg(state, catalog)
    # `agent` (the team name) and `entry_point` are setup-managed — overwrite so
    # a rename actually takes; `version` and any hand-written keys are preserved.
    cfg["agent"] = managed["agent"]
    cfg.setdefault("version", managed.get("version", "0.1.0"))
    cfg["entry_point"] = managed["entry_point"]
    if managed.get("venn_api_key"):
        cfg.setdefault("venn_api_key", managed["venn_api_key"])
    # Union services by name: keep every existing entry untouched, append only
    # services the pack doesn't already declare.
    existing_svcs = cfg.get("services") if isinstance(cfg.get("services"), list) else []
    have = {(s.get("name") if isinstance(s, dict) else str(s)) for s in existing_svcs}
    merged = list(existing_svcs)
    for rec in managed.get("services", []):
        if rec["name"] not in have:
            merged.append(rec)
    if merged:
        cfg["services"] = merged
    if state.chat and state.chat != "cli":
        cfg["chat"] = state.chat
    return yaml.dump(cfg, sort_keys=False)


def build_adhoc_yaml() -> str:
    """The open-ended task handler every pack must ship (validate requires it)."""
    return yaml.dump({
        "name": "adhoc",
        "trigger": "Any ad-hoc task or request not covered by another workflow.",
        "description": "Open-ended task handler.",
        "steps": [{"name": "task", "prompt": "${{input.task}}"}],
    }, sort_keys=False)


def build_monitors_yaml(state: SetupState) -> str:
    """Description-only monitors from the autonomous behaviors — robust at
    install time (no venn CLI dependency); the agent interprets them."""
    mons: list[dict] = []
    for i, b in enumerate(state.spec.autonomous):
        desc = (b.get("description") if isinstance(b, dict) else str(b)) or ""
        if not desc.strip():
            continue
        rec = {
            "name": slug(desc)[:40] or f"behavior-{i + 1}",
            "description": desc.strip(),
            "interval": cadence_to_interval(
                b.get("cadence", "") if isinstance(b, dict) else ""),
        }
        leash = b.get("leash") if isinstance(b, dict) else ""
        if leash == "notify":
            rec["notify"] = True
        mons.append(rec)
    return yaml.dump({"monitors": mons}, sort_keys=False)


def merge_monitors_yaml(existing_text: str, state: SetupState) -> str:
    """Union the spec's monitors into an existing monitors file by name —
    keep every hand-written monitor, add only the ones the pack lacks."""
    try:
        cur = yaml.safe_load(existing_text) or {}
    except yaml.YAMLError:
        cur = {}
    existing = cur.get("monitors") if isinstance(cur.get("monitors"), list) else []
    have = {m.get("name") for m in existing if isinstance(m, dict)}
    fresh = yaml.safe_load(build_monitors_yaml(state)).get("monitors", [])
    merged = list(existing) + [m for m in fresh if m.get("name") not in have]
    return yaml.dump({"monitors": merged}, sort_keys=False)


def has_monitors(state: SetupState) -> bool:
    return any((b.get("description") if isinstance(b, dict) else str(b))
               for b in state.spec.autonomous)


# --- LLM authoring prompts -----------------------------------------------

# These distill the pack-format conventions from skills/create-agent.md (the
# canonical reference) into per-file, one-shot authoring instructions. Keep
# them in sync with that doc — it's the source of truth for pack format.
AUTHORING_SYSTEM_PROMPT = """\
You author ONE file in a modastack agent-team package — a portable bundle of
prompts the LLM agents read and OBEY at runtime. Output ONLY the raw file
contents: no markdown code fences, no preamble, no sign-off, and no TODOs or
placeholders — the file must be complete and immediately usable.

Write for the agents who will execute this file, not for a human reader:
- Second person, concrete, operational. Prefer explicit rules and decision
  tables over prose.
- Reference the `modastack` CLI commands an agent would actually run.
- Obey the team's stated goal and the user's framing. NEVER invent roles,
  services, or behaviors the user did not ask for.
- Do NOT copy engineering-specific content into a non-engineering team.
- NEVER write a literal secret or token — reference credentials as ${ENV_VAR}.
"""


# Open/modify mode edits an existing file rather than authoring from scratch.
# The lock: preserve everything the user already wrote; change ONLY what the
# current design now requires. This is what makes modify non-lossy.
EDITING_SYSTEM_PROMPT = """\
You revise ONE existing file in a modastack agent-team package to match an
updated team design. You are EDITING, not rewriting.

Rules:
- Preserve the author's existing structure, wording, depth, and any content
  the design does not speak to. Make the SMALLEST change that brings the file
  in line with the design.
- If the file already reflects the design, return it UNCHANGED, verbatim.
- Output ONLY the raw, complete file contents — no code fences, no preamble,
  no commentary, no TODOs or placeholders.
- NEVER write a literal secret or token — reference credentials as ${ENV_VAR}.
"""


def edit_prompt(state: SetupState, what: str, current: str) -> str:
    """An edit instruction: here is the file now, here is the team design —
    bring it in line, minimally."""
    return (f"{_spec_brief(state)}\n\n"
            f"You are revising {what}. Here is the file's CURRENT contents:\n"
            f"-----\n{current}\n-----\n\n"
            "Update it so it reflects the team design above. Change only what "
            "the design now requires; keep everything else exactly as written. "
            "If it already matches, return it unchanged.")


def _spec_brief(state: SetupState) -> str:
    spec = state.spec
    roles = "; ".join(f"{r['name']}: {r['responsibility']}"
                      for r in normalized_roles(state))
    svcs = ", ".join((s.get("name") if isinstance(s, dict) else str(s))
                     for s in spec.services) or "none"
    auto = "; ".join((b.get("description") if isinstance(b, dict) else str(b))
                     for b in spec.autonomous) or "none"
    return (f"Team goal: {spec.goal}\nRoles: {roles}\n"
            f"Services: {svcs}\nProactive behaviors: {auto}")


def agent_md_prompt(state: SetupState) -> str:
    return (f"{_spec_brief(state)}\n\n"
            "Write agent.md — the shared base prompt EVERY role on this team "
            "inherits. Use exactly this structure:\n"
            "# <Team name>\n"
            "One paragraph: what this team does and the outcome it produces.\n"
            "## Roles\n"
            "- **<role>** — what it does   (one bullet per role)\n"
            "## Operating principles\n"
            "The rules every role follows — how they coordinate, when to "
            "escalate, how work hands off.\n"
            "Keep it tight: this is a shared base prompt, not an essay.")


# A custom service (not native, not on Venn) gets its own API guide so the
# agent knows how to call it. This is the #4 "write a posthog.md" path.
TOOLS_AUTHORING_SYSTEM_PROMPT = """\
You author ONE service guide (tools/<service>.md) in a modastack agent-team
package — operational instructions the LLM agents read to call an external
service's API at runtime. Output ONLY the raw markdown file contents: no
wrapping code fence, no preamble, no sign-off.

Write for the agent that will make the calls:
- One or two lines on what the service is and what this team uses it for.
- The concrete API surface the agent needs: base URL, the auth header (using
  the ${ENV_VAR} the team stores its key in — NEVER a literal key), and the
  specific endpoints/operations relevant to the team's goal, with short
  example requests.
- Note rate limits, pagination, or common pitfalls when they matter.
- Keep it tight and operational — every line should help the agent make a
  correct call.
"""


def tools_prompt(state: SetupState, conn) -> str:
    var = conn.credential_var or _env_var_fallback(conn.name)
    return (f"{_spec_brief(state)}\n\n"
            f"Write tools/{slug(conn.key)}.md — the usage guide for "
            f"**{conn.name}**, which this team reaches through its own API "
            f"(Venn does not cover it). The team stores its API key in the env "
            f"var {var}; reference it as ${{{var}}}, never a literal key. Focus "
            f"on the parts of {conn.name}'s API the team needs for its goal.")


def _env_var_fallback(name: str) -> str:
    import re
    s = re.sub(r"[^A-Z0-9]+", "_", (name or "").strip().upper()).strip("_")
    return f"{s or 'SERVICE'}_API_KEY"


def custom_services(state: SetupState, catalog=None) -> list:
    """The spec's services that are custom (not native, not on Venn) — each
    needs an authored tools guide. Deduped by connector key."""
    out, seen = [], set()
    for s in state.spec.services:
        name = s.get("name") if isinstance(s, dict) else str(s)
        if not (name or "").strip():
            continue
        conn = services.resolve(name, venn_catalog=catalog)
        if conn.kind == "custom" and conn.key not in seen:
            seen.add(conn.key)
            out.append(conn)
    return out


def role_md_prompt(state: SetupState, role: dict) -> str:
    return (f"{_spec_brief(state)}\n\n"
            f"Write roles/{slug(role['name'])}/ROLE.md for the "
            f"'{role['name']}' role. Responsibility: "
            f"{role['responsibility'] or 'support the team goal'}.\n"
            "- Open with identity: \"You are the <role> ... You <do what>.\"\n"
            "- Define scope: what this role does and does NOT do; what it "
            "delegates.\n"
            "- Be operational: concrete steps, decision tables, the exact "
            "service/CLI actions it takes, and how it hands off.\n"
            "- Show what good output looks like.\n"
            "Length follows complexity: keep a simple role under ~100 lines; "
            "go longer only if the job genuinely needs it. Every line should be "
            "an instruction the agent will use — no filler.")


# --- manifest ------------------------------------------------------------

@dataclass
class FileSpec:
    path: str                      # relative to the pack root
    content: str | None = None     # deterministic — written verbatim
    system: str | None = None      # LLM authoring system prompt
    user: str | None = None        # LLM authoring user prompt
    with_base: bool = False        # inject the authored agent.md for coherence

    @property
    def deterministic(self) -> bool:
        return self.content is not None


def compute_manifest(state: SetupState, catalog=None) -> list[FileSpec]:
    """The full ordered file list for a scratch pack. Structure is the
    wizard's; prose files carry authoring prompts. `catalog` is the Venn
    service catalog used to decide which services are custom (and so need an
    authored tools guide)."""
    files: list[FileSpec] = [
        FileSpec("agent.yaml", content=build_agent_yaml(state, catalog)),
        FileSpec("agent.md", system=AUTHORING_SYSTEM_PROMPT,
                 user=agent_md_prompt(state)),
    ]
    for role in normalized_roles(state):
        files.append(FileSpec(
            f"roles/{slug(role['name'])}/ROLE.md",
            system=AUTHORING_SYSTEM_PROMPT, user=role_md_prompt(state, role),
            with_base=True))
    files.append(FileSpec("workflows/adhoc.yaml", content=build_adhoc_yaml()))
    if has_monitors(state):
        files.append(FileSpec("monitors/defaults.yaml",
                              content=build_monitors_yaml(state)))
    # A guide for each custom (non-native, non-Venn) service the team uses.
    for conn in custom_services(state, catalog):
        files.append(FileSpec(
            f"tools/{slug(conn.key)}.md",
            system=TOOLS_AUTHORING_SYSTEM_PROMPT,
            user=tools_prompt(state, conn)))
    return files


# --- the pour ------------------------------------------------------------

_FENCE = re.compile(r"\A```[^\n]*\n(.*?)\n```\s*\Z", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Undo a model that wrapped the whole file in a ``` code fence."""
    m = _FENCE.match(text.strip())
    return (m.group(1) if m else text).strip() + "\n"


def _deterministic_body(spec: "FileSpec", target: Path, state: SetupState,
                        catalog=None) -> str:
    """The bytes for a deterministic file in open/modify mode: merge into the
    existing file (non-lossy) where one exists, else the from-scratch body."""
    if not target.is_file():
        return spec.content
    existing = target.read_text()
    if spec.path == "agent.yaml":
        return merge_agent_yaml(existing, state, catalog)
    if spec.path.startswith("monitors/"):
        return merge_monitors_yaml(existing, state)
    # adhoc.yaml and anything else already present: leave it exactly as written.
    return existing


async def author_pack(state: SetupState, project: Path, *,
                      model: str | None = None, stream_fn=None):
    """Author the pack source at the team's source location, yielding pour
    events. In **create** mode every file is written from scratch; in
    **open/modify** mode existing files are merged/edited in place so nothing
    the user already wrote is lost. Side effect: writes files, persists state."""
    from modastack.setup import services
    from modastack.setup.actions import team_source_dir
    state.team_name = derive_team_name(state)
    pack = team_source_dir(project, state)
    # Persist the concrete location (create resolved <base>/<name>) so the Done
    # screen, /api/files, install, and list_teams_in all agree on it. A source
    # in the home library lives outside the project, so it stays absolute.
    try:
        state.source_dir = pack.relative_to(project).as_posix()
    except ValueError:
        state.source_dir = str(pack)
    editing = state.mode != "create"
    # Classify services against Venn's real catalog (live when a key is present)
    # so custom services get an authored tools guide and the right credentials.
    catalog = services.live_venn_catalog(project)
    base_md = ""   # the authored agent.md, threaded into ROLE.md for coherence

    for spec in compute_manifest(state, catalog):
        target = pack / spec.path
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.is_file()
        yield {"type": "file_start", "path": spec.path}

        if spec.deterministic:
            body = (_deterministic_body(spec, target, state, catalog)
                    if editing else spec.content)
            target.write_text(body)
            yield {"type": "delta", "path": spec.path, "text": body}
        else:
            # Open mode edits a file that already exists; otherwise (create, or
            # a newly added role) author it from scratch.
            original = target.read_text() if existed else ""
            if editing and existed:
                system = EDITING_SYSTEM_PROMPT
                user = edit_prompt(state, spec.path, original)
            else:
                system = spec.system
                user = spec.user
                if spec.with_base and base_md:
                    user += ("\n\nThe team already has this shared base prompt "
                             "(agent.md) — align with it, do not contradict or "
                             "repeat it:\n\n" + base_md)
            parts: list[str] = []
            with target.open("w") as f:
                async for chunk in llm.stream(system, user,
                                              model=model, cwd=str(project),
                                              stream_fn=stream_fn):
                    f.write(chunk)
                    f.flush()
                    parts.append(chunk)
                    yield {"type": "delta", "path": spec.path, "text": chunk}
            # Normalize once: strip an accidental wrapping code fence; a
            # model that produced nothing usable keeps the prior file (open) or
            # gets a stub (create), never a blank.
            cleaned = _strip_fences("".join(parts))
            if not cleaned.strip():
                cleaned = original or f"# {spec.path}\n"
            target.write_text(cleaned)
            if spec.path == "agent.md":
                base_md = cleaned

        yield {"type": "file_end", "path": spec.path}

    state.save(project)
