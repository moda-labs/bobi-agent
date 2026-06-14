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

def build_agent_yaml(state: SetupState) -> str:
    cfg: dict = {
        "agent": derive_team_name(state),
        "version": "0.1.0",
        "entry_point": compute_entry_point(state),
    }
    # The chat channel you talk to the team through is itself a service the
    # team must connect (Slack); CLI needs nothing. Fold it into the service
    # list so its credentials land in agent.yaml. (Telegram: framework support
    # pending — CLI + Slack are the v1 channels.)
    service_names = [(s.get("name") if isinstance(s, dict) else str(s))
                     for s in state.spec.services]
    if state.chat == "slack":
        service_names.append("slack")
    svcs: list[dict] = []
    seen: set[str] = set()
    for name in service_names:
        if not (name or "").strip():
            continue
        conn = services.resolve(name)
        if conn.key in seen:
            continue
        seen.add(conn.key)
        rec: dict = {"name": conn.key}
        if conn.kind == "native":
            rec["events"] = True
            if conn.credential_var:
                key = _CRED_KEY.get(conn.key, "token")
                rec["credentials"] = {key: f"${{{conn.credential_var}}}"}
        svcs.append(rec)
    if svcs:
        cfg["services"] = svcs
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


def compute_manifest(state: SetupState) -> list[FileSpec]:
    """The full ordered file list for a scratch pack. Structure is the
    wizard's; prose files carry authoring prompts."""
    files: list[FileSpec] = [
        FileSpec("agent.yaml", content=build_agent_yaml(state)),
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
    return files


# --- the pour ------------------------------------------------------------

_FENCE = re.compile(r"\A```[^\n]*\n(.*?)\n```\s*\Z", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Undo a model that wrapped the whole file in a ``` code fence."""
    m = _FENCE.match(text.strip())
    return (m.group(1) if m else text).strip() + "\n"


async def author_pack(state: SetupState, project: Path, *,
                      model: str | None = None, stream_fn=None):
    """Author the pack source at agents/<team_name>/, yielding pour events.
    Side effect: writes files and persists `state`."""
    state.team_name = derive_team_name(state)
    pack = project / "agents" / state.team_name
    base_md = ""   # the authored agent.md, threaded into ROLE.md for coherence

    for spec in compute_manifest(state):
        target = pack / spec.path
        target.parent.mkdir(parents=True, exist_ok=True)
        yield {"type": "file_start", "path": spec.path}

        if spec.deterministic:
            target.write_text(spec.content)
            yield {"type": "delta", "path": spec.path, "text": spec.content}
        else:
            user = spec.user
            if spec.with_base and base_md:
                user += ("\n\nThe team already has this shared base prompt "
                         "(agent.md) — align with it, do not contradict or "
                         "repeat it:\n\n" + base_md)
            parts: list[str] = []
            with target.open("w") as f:
                async for chunk in llm.stream(spec.system, user,
                                              model=model, cwd=str(project),
                                              stream_fn=stream_fn):
                    f.write(chunk)
                    f.flush()
                    parts.append(chunk)
                    yield {"type": "delta", "path": spec.path, "text": chunk}
            # Normalize once: strip an accidental wrapping code fence; a
            # model that produced nothing usable gets a stub, never a blank.
            cleaned = _strip_fences("".join(parts))
            if not cleaned.strip():
                cleaned = f"# {spec.path}\n"
            target.write_text(cleaned)
            if spec.path == "agent.md":
                base_md = cleaned

        yield {"type": "file_end", "path": spec.path}

    state.save(project)
