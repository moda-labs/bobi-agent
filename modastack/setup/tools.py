"""In-process tools for the setup session.

Every handler stage-checks against the shared SetupState before acting
and returns a structured refusal instead of trusting the prompt. The
deterministic machinery (registry, install, validation, preflight) is
the same code the rest of the CLI uses — setup adds no parallel
implementations.

Secrets never enter the session transcript: save_credential prompts the
user directly on the terminal (masked) and writes .modastack/.env; only
a masked echo returns to the model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import click
import yaml

from modastack import paths
from modastack.setup.state import (
    INTERVIEW_KEYS,
    SetupState,
    Stage,
    source_tree_hash,
)
from modastack.setup.venn_cli import run_venn

# Token shapes that must never appear as literals in a generated pack.
SECRET_SHAPES = re.compile(
    r"xox[a-z]-|lin_api_|ghp_|github_pat_|sk-ant-|venn_[a-z0-9]{8,}"
)

PACK_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": f"REFUSED: {text}"}],
            "is_error": True}


def _default_secret_prompt(var_name: str, service: str, instructions: str) -> str:
    """Masked terminal prompt — the value never reaches the model."""
    click.echo()
    if instructions:
        click.secho(f"  {service}: {instructions}", fg="cyan")
    return click.prompt(f"  {var_name}", hide_input=True, default="",
                        show_default=False)


def _mask(value: str) -> str:
    if len(value) > 12:
        return f"{value[:4]}…{value[-4:]}"
    return "•••"


def _env_path(project: Path) -> Path:
    return paths.modastack_dir(project) / ".env"


def _read_env(project: Path) -> dict[str, str]:
    from modastack.config import parse_env_file
    return parse_env_file(_env_path(project))


def _write_env(project: Path, values: dict[str, str]) -> None:
    from modastack.config import write_env_file
    write_env_file(_env_path(project), values)


def _venn_key(project: Path) -> str:
    # Same precedence as runtime resolution (config.load_dotenv): an
    # exported environment variable wins over .env, so setup verifies the
    # key `modastack start` will actually use.
    import os
    return os.environ.get("VENN_API_KEY") or _read_env(project).get("VENN_API_KEY", "")


def _team_source_dir(project: Path, state: SetupState) -> Path:
    return project / "agents" / state.team_name


def installed_team_name(project: Path) -> str | None:
    """Name of the team installed in .modastack/, or None."""
    agent_yaml = paths.agent_yaml_path(project)
    if not agent_yaml.exists():
        return None
    try:
        return (yaml.safe_load(agent_yaml.read_text()) or {}).get(
            "agent", "an agent team")
    except yaml.YAMLError:
        return "an agent team"


def _resolve_or_fetch(name: str, project: Path) -> Path | None:
    """Resolve a team locally, falling back to a registry fetch."""
    from modastack.cli import _resolve_agent_pack
    pack_dir = _resolve_agent_pack(name, project)
    if pack_dir:
        return pack_dir
    from modastack.registry import fetch
    fetch(project, name)
    return _resolve_agent_pack(name, project)


def make_setup_tools(state: SetupState, project: Path,
                     prompt_fn: Callable[[str, str, str], str] | None = None,
                     ) -> list:
    """Build the tool list. All handlers close over `state` and `project`."""
    from claude_agent_sdk import tool

    prompt_secret = prompt_fn or _default_secret_prompt

    def _checkpoint() -> None:
        state.save(project)

    # --- choose ----------------------------------------------------------

    @tool("list_teams",
          "List agent teams available to install: local teams in this "
          "project's agents/ directory and teams from remote registries.",
          {})
    async def list_teams(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.CHOOSE)
        if refusal:
            return _err(refusal)

        import asyncio

        teams: list[dict] = []
        # Same locations modastack install resolves from: project agents/
        # plus the registry cache in .modastack/agents/.
        for local_dir, source in [(project / "agents", "local"),
                                  (paths.agents_dir(project), "cached")]:
            if not local_dir.is_dir():
                continue
            for d in sorted(local_dir.iterdir()):
                if d.is_dir() and (d / "agent.yaml").exists() \
                        and not any(t["name"] == d.name for t in teams):
                    pitch = ""
                    agent_md = d / "agent.md"
                    if agent_md.exists():
                        for line in agent_md.read_text().splitlines():
                            line = line.strip()
                            if line and not line.startswith("#"):
                                pitch = line
                                break
                    teams.append({"name": d.name, "source": source, "pitch": pitch})
        try:
            from modastack.registry import list_remote
            for pack in await asyncio.to_thread(list_remote, project):
                if not any(t["name"] == pack["name"] for t in teams):
                    teams.append({"name": pack["name"], "source": "registry",
                                  "pitch": pack.get("description", "")})
        except Exception as e:
            teams.append({"note": f"registry unavailable: {e}"})
        return _ok(json.dumps(teams, indent=1))

    @tool("get_team_details",
          "Show one team's agent.md, agent.yaml, and role list.",
          {"name": str})
    async def get_team_details(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.CHOOSE)
        if refusal:
            return _err(refusal)

        import asyncio
        try:
            pack_dir = await asyncio.to_thread(_resolve_or_fetch,
                                               args["name"], project)
        except Exception as e:
            return _err(f"team '{args['name']}' not found locally or remotely: {e}")
        if not pack_dir:
            return _err(f"team '{args['name']}' not found locally or remotely")

        parts = []
        for f in ["agent.md", "agent.yaml"]:
            p = pack_dir / f
            if p.exists():
                parts.append(f"--- {f} ---\n{p.read_text()}")
        roles = sorted(d.name for d in (pack_dir / "roles").iterdir()
                       if d.is_dir()) if (pack_dir / "roles").is_dir() else []
        parts.append(f"--- roles ---\n{', '.join(roles) or '(none)'}")
        return _ok("\n\n".join(parts))

    @tool("select_team",
          "Commit the branch decision. branch='use-as-is' with an existing "
          "team name installs it directly; branch='build' with a new pack "
          "slug starts the build-your-own interview.",
          {"name": str, "branch": str})
    async def select_team(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.CHOOSE)
        if refusal:
            return _err(refusal)

        branch, name = args["branch"], args["name"]
        if branch not in ("use-as-is", "build"):
            return _err("branch must be 'use-as-is' or 'build'")
        if branch == "use-as-is":
            import asyncio
            try:
                resolved = await asyncio.to_thread(_resolve_or_fetch, name, project)
            except Exception as e:
                return _err(f"team '{name}' not found locally or remotely: {e}")
            if not resolved:
                return _err(f"team '{name}' not found locally or remotely")
        else:
            if not PACK_SLUG.match(name):
                return _err("pack name must be a lowercase slug (letters, "
                            "digits, dashes), e.g. 'sales-outreach'")
            if (project / "agents" / name).exists():
                return _err(f"agents/{name}/ already exists — pick another "
                            "name or remove the directory first")
        state.branch, state.team_name = branch, name
        _checkpoint()
        return _ok(f"selected '{name}' (branch: {branch})")

    # --- interview -------------------------------------------------------

    @tool("record_answer",
          "Commit one interview conclusion. Valid questions: "
          + ", ".join(INTERVIEW_KEYS)
          + ". Free-form discussion happens in conversation; this records "
            "the agreed answer. schedules/event_triggers/gates may be 'none'.",
          {"question": str, "answer": str})
    async def record_answer(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.INTERVIEW)
        if refusal:
            return _err(refusal)
        q = args["question"]
        if q not in INTERVIEW_KEYS:
            return _err(f"unknown question '{q}' — valid keys: "
                        + ", ".join(INTERVIEW_KEYS))
        state.answers[q] = args["answer"]
        _checkpoint()
        remaining = [k for k in INTERVIEW_KEYS if k not in state.answers]
        return _ok(f"recorded '{q}'"
                   + (f" — still unrecorded: {', '.join(remaining)}"
                      if remaining else " — all seven questions recorded"))

    # --- services --------------------------------------------------------

    @tool("save_credential",
          "Securely collect one credential. Prompts the user directly on "
          "their terminal (masked input) and writes .modastack/.env — the "
          "value never enters this conversation. Provide instructions "
          "telling the user where to find the value.",
          {"var_name": str, "service": str, "instructions": str})
    async def save_credential(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.SERVICES, Stage.INSTALL)
        if refusal:
            return _err(refusal)
        var = args["var_name"].strip()
        if not re.match(r"^[A-Z][A-Z0-9_]*$", var):
            return _err("var_name must be an UPPER_SNAKE_CASE env var name")
        if var.startswith("MODASTACK_"):
            return _err("MODASTACK_* variables configure the framework "
                        "itself and are not credentials — they are not "
                        "collected through setup")

        import asyncio
        import os
        value = await asyncio.to_thread(
            prompt_secret, var, args.get("service", ""),
            args.get("instructions", ""))
        if not value:
            return _ok(json.dumps({"saved": False, "skipped": True,
                                   "var": var}))
        env = _read_env(project)
        env[var] = value
        _write_env(project, env)
        # Refresh this process too: config.load_dotenv never overwrites an
        # existing os.environ entry, so a corrected credential would
        # otherwise stay stale for the rest of the setup session.
        os.environ[var] = value
        if var not in state.credentials_saved:
            state.credentials_saved.append(var)
        _checkpoint()
        return _ok(json.dumps({"saved": True, "var": var,
                               "masked": _mask(value)}))

    @tool("check_venn",
          "Check which of the given service names are connected in the "
          "user's Venn account (VENN_API_KEY must be saved first).",
          {"services": list})
    async def check_venn(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.SERVICES, Stage.DISCOVERY)
        if refusal:
            return _err(refusal)
        key = _venn_key(project)
        if not key:
            return _err("no VENN_API_KEY saved — collect it with "
                        "save_credential first")
        import asyncio
        from modastack.venn import check_services
        check = await asyncio.to_thread(check_services, key, list(args["services"]))
        return _ok(json.dumps({"connected": check.connected,
                               "missing": check.missing}))

    # --- discovery -------------------------------------------------------

    @tool("venn_cli",
          "Run the venn CLI for monitor discovery (read-only). Pass the "
          "argument string exactly as it would appear after 'venn' in a "
          "monitor command line, e.g. \"tools search 'list emails'\" or "
          "\"tools execute -s work-gmail -t list_messages -a '{}'\". The "
          "API key is injected automatically.",
          {"args": str})
    async def venn_cli(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.DISCOVERY)
        if refusal:
            return _err(refusal)
        key = _venn_key(project)
        if not key:
            return _err("no VENN_API_KEY saved — collect it with "
                        "save_credential first")
        import asyncio
        result = await asyncio.to_thread(run_venn, args["args"], key)
        if result.refused:
            return _err(result.refused)
        status = "exit 0" if result.ok else "non-zero exit"
        return _ok(f"({status})\n{result.output}")

    @tool("record_monitor",
          "Commit one monitor for the generated pack: either a live-tested "
          "command (preferred — output must be a diffable list with stable "
          "ids) or a description for an agent-interpreted monitor.",
          {"name": str, "command": str, "description": str,
           "interval": str, "event": str})
    async def record_monitor(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.DISCOVERY)
        if refusal:
            return _err(refusal)
        record = {k: v for k, v in args.items() if str(v).strip()}
        if not record.get("command") and not record.get("description"):
            return _err("a monitor needs a command (tested via venn_cli) "
                        "or a description")
        try:
            from modastack.monitors.schema import Monitor, parse_interval
            Monitor.from_dict(dict(record))
            # from_dict stores the interval verbatim; parse it now so a bad
            # value fails here instead of silently at scheduler runtime.
            parse_interval(record.get("interval", "15m"))
        except (ValueError, KeyError) as e:
            return _err(f"invalid monitor record: {e}")
        state.monitors_recorded = [m for m in state.monitors_recorded
                                   if m.get("name") != record["name"]]
        state.monitors_recorded.append(record)
        _checkpoint()
        return _ok(f"recorded monitor '{record['name']}' "
                   f"({len(state.monitors_recorded)} total)")

    @tool("skip_discovery",
          "Skip the monitor discovery stage, with a reason (e.g. the team "
          "has no Venn event sources, or the venn CLI is unavailable).",
          {"reason": str})
    async def skip_discovery(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.SERVICES, Stage.DISCOVERY)
        if refusal:
            return _err(refusal)
        reason = args["reason"].strip()
        if not reason:
            return _err("a reason is required")
        state.discovery_skipped_reason = reason
        _checkpoint()
        return _ok(f"discovery marked skippable: {reason}")

    # --- generate --------------------------------------------------------

    @tool("validate_team",
          "Structurally validate the generated team source at "
          "agents/<name>/ using the framework's real parsers. Fix every "
          "finding and re-run until it passes — advancing to install "
          "requires a passing validation.",
          {})
    async def validate_team(args: dict[str, Any]) -> dict:
        # Also legal from install: a source edit after advancing must be
        # re-validatable without moving backwards through the stage gate.
        refusal = state.require_stage(Stage.GENERATE, Stage.INSTALL)
        if refusal:
            return _err(refusal)
        pack_dir = _team_source_dir(project, state)
        findings = _validate_pack(pack_dir, state, project)
        failures = [f for f in findings if not f[0]]
        report = "\n".join(f"  {'✓' if ok else '✗'} {detail}"
                           for ok, detail in findings)
        if failures:
            state.validated = False
            state.validated_hash = ""
            _checkpoint()
            return _ok(f"validation FAILED ({len(failures)} finding(s)):\n{report}")
        state.validated = True
        state.validated_hash = source_tree_hash(pack_dir)
        _checkpoint()
        return _ok(f"validation passed:\n{report}")

    # --- install ---------------------------------------------------------

    @tool("install_team",
          "Install the selected/generated team into .modastack/ (the frozen "
          "runtime image). Returns any credential vars still missing from "
          ".env — collect each with save_credential.",
          {})
    async def install_team(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.INSTALL)
        if refusal:
            return _err(refusal)

        from modastack.cli import (_install_pack, _resolve_agent_pack,
                                   _write_install_gitignore)
        pack_dir = _resolve_agent_pack(state.team_name, project)
        if not pack_dir:
            return _err(f"team source for '{state.team_name}' not found")

        if state.branch == "build":
            current = source_tree_hash(pack_dir)
            if not state.validated or current != state.validated_hash:
                state.validated = False
                _checkpoint()
                return _err("the team source changed since validate_team "
                            "last passed — run validate_team again before "
                            "installing")

        dot_moda = paths.modastack_dir(project)
        local_source = (pack_dir.is_relative_to(project)
                        and not pack_dir.is_relative_to(dot_moda))
        _install_pack(pack_dir, project, local_source)
        _write_install_gitignore(project, local_source)

        state.installed = True
        _checkpoint()

        import os
        from modastack.config import find_required_env_vars
        env = _read_env(project)
        missing = [v for v in find_required_env_vars(project)
                   if v not in env and v not in os.environ]
        return _ok(json.dumps({
            "installed": state.team_name,
            "image": str(dot_moda),
            "missing_credentials": missing,
        }))

    @tool("run_preflight",
          "Run the same preflight checks `modastack start` runs: entry "
          "point, credentials, venn connections, MCP probes.",
          {})
    async def run_preflight(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.INSTALL, Stage.DONE)
        if refusal:
            return _err(refusal)
        # In a worker thread: validate_config's MCP probe calls
        # asyncio.run(), which raises inside this handler's running loop,
        # and its network checks would otherwise block the REPL.
        import asyncio
        from modastack.validate import validate_config
        result = await asyncio.to_thread(validate_config, project)
        verdict = "PASSED" if result.ok else "FAILED"
        return _ok(f"preflight {verdict}:\n{result.format()}")

    # --- flow control ----------------------------------------------------

    @tool("advance_stage",
          "Move setup to the next stage. Refuses when the current stage's "
          "requirements are unmet — the error says exactly what is missing. "
          "Stages: " + " → ".join(s.value for s in Stage),
          {"to": str, "summary": str})
    async def advance_stage(args: dict[str, Any]) -> dict:
        try:
            to = Stage(args["to"])
        except ValueError:
            return _err(f"unknown stage '{args['to']}' — stages: "
                        + ", ".join(s.value for s in Stage))
        reason = state.can_advance(to)
        if reason:
            return _err(f"cannot enter '{to.value}': {reason}")
        state.stage = to
        if args.get("summary"):
            state.stage_summaries[to.value] = args["summary"]
        _checkpoint()
        click.secho(f"\n── {to.value} ──\n", fg="cyan", dim=True)
        return _ok(f"now in stage '{to.value}'")

    @tool("finish_setup",
          "End the setup session (only from stage 'done'). The closing "
          "message is shown to the user.",
          {"message": str})
    async def finish_setup(args: dict[str, Any]) -> dict:
        refusal = state.require_stage(Stage.DONE)
        if refusal:
            return _err(refusal)
        state.finished = True
        _checkpoint()
        return _ok("setup complete — the session will close after this turn")

    return [list_teams, get_team_details, select_team, record_answer,
            save_credential, check_venn, venn_cli, record_monitor,
            skip_discovery, validate_team, install_team, run_preflight,
            advance_stage, finish_setup]


def create_setup_server(state: SetupState, project: Path,
                        prompt_fn: Callable[[str, str, str], str] | None = None):
    from claude_agent_sdk import create_sdk_mcp_server
    return create_sdk_mcp_server(
        name="setup", tools=make_setup_tools(state, project, prompt_fn))


# --- pack validation ------------------------------------------------------

def _validate_pack(pack_dir: Path, state: SetupState,
                   project: Path) -> list[tuple[bool, str]]:
    """Structural validation of a team source tree. Returns (ok, detail)."""
    findings: list[tuple[bool, str]] = []

    if not pack_dir.is_dir():
        return [(False, f"{pack_dir} does not exist")]

    # agent.yaml
    cfg: dict = {}
    agent_yaml = pack_dir / "agent.yaml"
    if not agent_yaml.exists():
        findings.append((False, "agent.yaml is missing"))
    else:
        try:
            cfg = yaml.safe_load(agent_yaml.read_text()) or {}
            findings.append((True, "agent.yaml parses"))
        except yaml.YAMLError as e:
            findings.append((False, f"agent.yaml does not parse: {e}"))

    entry = cfg.get("entry_point", "")
    if not entry:
        findings.append((False, "agent.yaml has no entry_point"))
    else:
        role_md = pack_dir / "roles" / entry / "ROLE.md"
        findings.append((role_md.exists(),
                         f"entry point role '{entry}' has roles/{entry}/ROLE.md"
                         if role_md.exists() else
                         f"entry_point '{entry}' has no roles/{entry}/ROLE.md"))

    roles_dir = pack_dir / "roles"
    if roles_dir.is_dir():
        for d in sorted(roles_dir.iterdir()):
            if d.is_dir() and not (d / "ROLE.md").exists():
                findings.append((False, f"roles/{d.name}/ has no ROLE.md"))

    # agent.md
    findings.append(((pack_dir / "agent.md").exists(),
                     "agent.md present" if (pack_dir / "agent.md").exists()
                     else "agent.md is missing"))

    # workflows
    wf_dir = pack_dir / "workflows"
    wf_files = sorted(wf_dir.glob("*.yaml")) if wf_dir.is_dir() else []
    if not (wf_dir / "adhoc.yaml").exists():
        findings.append((False, "workflows/adhoc.yaml is missing (always "
                                "include the open-ended task handler)"))
    from modastack.workflow.schema import load_workflow
    for wf in wf_files:
        try:
            load_workflow(wf)
            findings.append((True, f"workflows/{wf.name} parses"))
        except Exception as e:
            findings.append((False, f"workflows/{wf.name}: {e}"))

    # monitors
    mon_file = pack_dir / "monitors" / "defaults.yaml"
    if mon_file.exists():
        from modastack.monitors.schema import Monitor, parse_interval
        try:
            raw = yaml.safe_load(mon_file.read_text()) or {}
            for rec in raw.get("monitors") or []:
                Monitor.from_dict(dict(rec))
                # parse intervals eagerly — the scheduler only evaluates
                # them lazily, where a bad value means the monitor
                # silently never fires.
                parse_interval(rec.get("interval", "15m"))
            findings.append((True, "monitors/defaults.yaml parses"))
        except Exception as e:
            findings.append((False, f"monitors/defaults.yaml: {e}"))
    elif state.monitors_recorded:
        findings.append((False,
                         f"{len(state.monitors_recorded)} monitor(s) were "
                         "recorded during discovery but monitors/defaults.yaml "
                         "is missing"))

    # literal secrets: known token shapes, plus the exact values the user
    # saved during this setup — exact matching catches every service the
    # shape list has never heard of.
    saved_values = {v for v in _read_env(project).values() if len(v) >= 8}
    for f in sorted(pack_dir.rglob("*")):
        if f.is_file() and f.suffix in (".yaml", ".md"):
            text = f.read_text()
            if SECRET_SHAPES.search(text) or any(v in text for v in saved_values):
                findings.append((False,
                                 f"{f.relative_to(pack_dir)} contains what "
                                 "looks like a literal secret — use ${VAR} "
                                 "references"))
    return findings
