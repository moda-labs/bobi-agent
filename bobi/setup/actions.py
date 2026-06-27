"""Deterministic setup actions — the engine bodies, no stage gating.

These are the pure(ish) functions the setup machinery is built from:
registry resolution, credential capture, pack validation, install, and
preflight. They mutate `SetupState` and checkpoint it, but they do NOT
enforce which stage you're in — stage gating is a caller concern (the
`@tool` wrappers in `tools.py` today, the web server tomorrow).

Business-rule refusals (a bad var name, a missing team, a stale
validation) raise `ActionError`; the caller decides how to surface it.
The same deterministic machinery the rest of the CLI uses (registry,
install, validation, preflight) is reused here — setup adds no parallel
implementations.

Secrets never enter any model transcript: `save_credential` prompts the
user directly (masked) via the injected `prompt_fn` and writes
`run/.env`; only a masked echo is returned.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

import click
import yaml

from bobi import paths
from bobi.setup.state import SetupState, source_tree_hash

# Token shapes that must never appear as literals in a generated pack.
SECRET_SHAPES = re.compile(
    r"xox[a-z]-|lin_api_|ghp_|github_pat_|sk-ant-|venn_[a-z0-9]{8,}"
)

PACK_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

# Full-token secret patterns for REDACTING freeform chat input before it
# reaches the LLM, the rolling summary, or the persisted transcript.
# (SECRET_SHAPES above is prefix-only, for scanning generated files; this is
# whole-token so we can strip the value, not just flag a prefix.) Credentials
# belong in Connect (→ run/.env), never in the conversation — so this errs
# toward redaction; a redacted long hash in a design chat costs nothing.
REDACTION_PLACEHOLDER = "[redacted]"

_SECRET_TOKEN = re.compile(
    r"""(
        -----BEGIN[A-Z ]*PRIVATE\ KEY-----.*?-----END[A-Z ]*PRIVATE\ KEY-----
      | xox[abprs]-[A-Za-z0-9-]{10,}        # slack
      | lin_api_[A-Za-z0-9]{10,}            # linear
      | ghp_[A-Za-z0-9]{20,}                # github PAT
      | github_pat_[A-Za-z0-9_]{20,}
      | sk-ant-[A-Za-z0-9_-]{20,}           # anthropic (before generic sk-)
      | sk-[A-Za-z0-9]{20,}                 # openai-style
      | venn_[A-Za-z0-9]{8,}                # venn
      | AKIA[0-9A-Z]{16}                    # aws access key id
      | AIza[0-9A-Za-z_-]{20,}              # google api key
      | eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+  # jwt
      | (?i:bearer)\s+[A-Za-z0-9._-]{8,}    # bearer token (space form)
      | [A-Za-z0-9_-]{40,}                  # generic long opaque token
    )""",
    re.VERBOSE | re.DOTALL,
)

# `password: hunter2`, `API_KEY=shortish` — redact the value, which shape
# detection alone (short secrets) would miss. Requires a ':' or '=' so prose
# like "keep the credential safe" is left alone.
_SECRET_KV = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?token"
    r"|auth[_-]?token|client[_-]?secret|bot[_-]?token)\b(\s*[:=]\s*)(\S+)"
)


def redact_secrets(text: str) -> tuple[str, int]:
    """Strip secret-shaped substrings from `text`, returning
    (redacted_text, count). Idempotent — re-running on redacted text is a
    no-op (the placeholder has no secret shape)."""
    count = 0

    def _tok(_m):
        nonlocal count
        count += 1
        return REDACTION_PLACEHOLDER

    text = _SECRET_TOKEN.sub(_tok, text)

    def _kv(m):
        nonlocal count
        if m.group(3) == REDACTION_PLACEHOLDER:
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}{REDACTION_PLACEHOLDER}"

    text = _SECRET_KV.sub(_kv, text)
    return text, count


class ActionError(Exception):
    """A deterministic business-rule refusal (not stage gating)."""


# --- terminal / masking helpers ------------------------------------------

def default_secret_prompt(var_name: str, service: str, instructions: str) -> str:
    """Masked terminal prompt — the value never reaches the model."""
    click.echo()
    if instructions:
        click.secho(f"  {service}: {instructions}", fg="cyan")
    return click.prompt(f"  {var_name}", hide_input=True, default="",
                        show_default=False)


def mask(value: str) -> str:
    if len(value) > 12:
        return f"{value[:4]}…{value[-4:]}"
    return "•••"


# --- env helpers ----------------------------------------------------------

def env_path(project: Path) -> Path:
    return paths.env_path(project)


def read_env(project: Path) -> dict[str, str]:
    from bobi.config import parse_env_file
    return parse_env_file(env_path(project))


def write_env(project: Path, values: dict[str, str]) -> None:
    from bobi.config import write_env_file
    write_env_file(env_path(project), values)


def venn_key(project: Path) -> str:
    # Same precedence as runtime resolution (config.load_dotenv): an
    # exported environment variable wins over .env, so setup verifies the
    # key `bobi agent <name> start` will actually use.
    return os.environ.get("VENN_API_KEY") or read_env(project).get("VENN_API_KEY", "")


# --- team / source resolution --------------------------------------------

def team_source_dir(project: Path, state: SetupState) -> Path:
    """Where the team source lives.

    ``source_dir`` is an exact source directory. Relative paths are anchored at
    ``BOBI_HOME`` because setup is machine-scoped, not cwd-scoped. When unset,
    the canonical source is ``<BOBI_HOME>/agents/<name>/src``.
    """
    if state.source_dir:
        p = Path(state.source_dir)
        return p if p.is_absolute() else paths.home_dir() / p
    return paths.agent_source_dir(state.team_name)


def installed_team_name(project: Path) -> str | None:
    """Name of the team installed in run/package/, or None."""
    agent_yaml = paths.agent_yaml_path(project)
    if not agent_yaml.exists():
        return None
    try:
        return (yaml.safe_load(agent_yaml.read_text()) or {}).get(
            "agent", "an agent team")
    except yaml.YAMLError:
        return "an agent team"


def resolve_or_fetch(name: str, project: Path) -> Path | None:
    """Resolve a team locally, then fetch it from a registry if needed."""
    from bobi.cli import _resolve_agent_pack
    pack_dir = _resolve_agent_pack(name, project)
    if pack_dir:
        return pack_dir
    from bobi.registry import fetch
    fetch(project, name)
    return _resolve_agent_pack(name, project)


# --- credential capture ---------------------------------------------------

def save_credential(state: SetupState, project: Path, var_name: str,
                    service: str, instructions: str,
                    prompt_fn: Callable[[str, str, str], str]) -> dict:
    """Collect one credential via `prompt_fn`, write it to run/.env, refresh
    the process environment, and record it on the state. The value never
    leaves this function — only a masked echo is returned.

    Returns {"saved": False, "skipped": True, "var": ...} on empty input,
    else {"saved": True, "var": ..., "masked": ...}.
    Raises ActionError on an invalid or framework-reserved var name.
    """
    var = var_name.strip()
    if not re.match(r"^[A-Z][A-Z0-9_]*$", var):
        raise ActionError("var_name must be an UPPER_SNAKE_CASE env var name")
    if var.startswith("BOBI_"):
        raise ActionError("BOBI_* variables configure the framework "
                          "itself and are not credentials — they are not "
                          "collected through setup")

    value = prompt_fn(var, service or "", instructions or "")
    if not value:
        return {"saved": False, "skipped": True, "var": var}
    # A newline in the value would write extra physical lines into .env — on
    # reparse that truncates the secret or injects a spurious VAR=…. Reject it
    # (no legitimate token contains a newline).
    if "\n" in value or "\r" in value:
        raise ActionError("credential value cannot contain newlines")

    env = read_env(project)
    env[var] = value
    write_env(project, env)
    # Refresh this process too: config.load_dotenv never overwrites an
    # existing os.environ entry, so a corrected credential would otherwise
    # stay stale for the rest of the setup session.
    os.environ[var] = value
    if var not in state.credentials_saved:
        state.credentials_saved.append(var)
    state.save(project)
    return {"saved": True, "var": var, "masked": mask(value)}


# --- pack validation ------------------------------------------------------

def validate_pack(pack_dir: Path, state: SetupState,
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
    from bobi.workflow.schema import load_workflow
    for wf in wf_files:
        try:
            load_workflow(wf)
            findings.append((True, f"workflows/{wf.name} parses"))
        except Exception as e:
            findings.append((False, f"workflows/{wf.name}: {e}"))

    # monitors
    mon_file = pack_dir / "monitors" / "defaults.yaml"
    if mon_file.exists():
        from bobi.monitors.schema import Monitor, parse_interval
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
    elif state.spec.autonomous:
        findings.append((False,
                         f"{len(state.spec.autonomous)} proactive behavior(s) "
                         "were specified but monitors/defaults.yaml is missing"))

    # literal secrets: known token shapes, plus the exact values the user
    # saved during this setup — exact matching catches every service the
    # shape list has never heard of.
    saved_values = {v for v in read_env(project).values() if len(v) >= 8}
    for f in sorted(pack_dir.rglob("*")):
        if f.is_file() and f.suffix in (".yaml", ".md"):
            text = f.read_text()
            if SECRET_SHAPES.search(text) or any(v in text for v in saved_values):
                findings.append((False,
                                 f"{f.relative_to(pack_dir)} contains what "
                                 "looks like a literal secret — use ${VAR} "
                                 "references"))
    return findings


def validate_team(state: SetupState, project: Path) -> dict:
    """Validate the generated team source, freezing the validated hash on
    success and clearing it on failure. Returns
    {"passed": bool, "report": str, "failure_count": int}.
    """
    pack_dir = team_source_dir(project, state)
    findings = validate_pack(pack_dir, state, project)
    failures = [f for f in findings if not f[0]]
    report = "\n".join(f"  {'✓' if ok else '✗'} {detail}"
                       for ok, detail in findings)
    if failures:
        state.validated = False
        state.validated_hash = ""
        state.save(project)
        return {"passed": False, "report": report,
                "failure_count": len(failures)}
    state.validated = True
    state.validated_hash = source_tree_hash(pack_dir)
    state.save(project)
    return {"passed": True, "report": report, "failure_count": 0}


# --- install / preflight --------------------------------------------------

def install_team(state: SetupState, project: Path) -> dict:
    """Install the selected/generated team into run/package/ (the frozen
    runtime image). Returns {"installed", "image", "missing_credentials"}.
    Raises ActionError if the source is missing or its validation is stale.
    """
    from bobi.cli import (_install_pack, _resolve_agent_pack,
                               _write_install_gitignore)
    # Prefer the chosen source location; fall back to registry resolution.
    pack_dir = team_source_dir(project, state)
    if not pack_dir.is_dir():
        pack_dir = _resolve_agent_pack(state.team_name, project)
    if not pack_dir:
        raise ActionError(f"team source for '{state.team_name}' not found")

    if state.mode == "create":
        current = source_tree_hash(pack_dir)
        if not state.validated or current != state.validated_hash:
            state.validated = False
            state.save(project)
            raise ActionError("the team source changed since validate_team "
                              "last passed — run validate_team again before "
                              "installing")

    package = paths.package_dir(project)
    local_source = not pack_dir.is_relative_to(paths.agent_cache_dir())
    _install_pack(pack_dir, project, local_source)
    _write_install_gitignore(project, local_source)

    state.installed = True
    state.save(project)

    from bobi.config import find_required_env_vars
    env = read_env(project)
    missing = [v for v in find_required_env_vars(project)
               if v not in env and v not in os.environ]
    return {
        "installed": state.team_name,
        "image": str(package),
        "missing_credentials": missing,
    }


def run_preflight(project: Path):
    """Run the same preflight checks `bobi agent <name> start` runs. Returns the
    validate_config result (has `.ok` and `.format()`).

    NOTE: validate_config's MCP probe calls asyncio.run(), which raises
    inside a running event loop, and its network checks block — call this
    from a worker thread (asyncio.to_thread) when in async code.
    """
    from bobi.validate import validate_config
    return validate_config(project)
