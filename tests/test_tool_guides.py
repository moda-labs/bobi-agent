"""Docs and agent-facing guides must reference the public bobi contract.

Tool guides, role prompts, slash commands, and docs teach humans and
agents CLI invocations. A guide that documents a nonexistent command
ships agents that try to run it — this drift class reached main twice
(`bobi slack-send`, a fictional `bobi linear` group). The #526 runtime
cutover also made stale path docs dangerous: the old project-local runtime
image is gone, and runtime operations now require a named scope.
"""

import re
import shlex
import tomllib
from pathlib import Path

import pytest

from bobi.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent


def _plugin_commands() -> frozenset[str]:
    """Commands the in-repo bobi-deploy plugin declares via `bobi.commands`.

    The public unit suite runs WITHOUT the plugin installed (its CI lives in
    deploy-package.yml; repo-split phase 1), so `_PluginGroup.get_command`
    cannot resolve these from entry-point metadata. Read the declarations from
    the plugin's own pyproject instead. When bobi_deploy/ leaves the tree at
    cut time this returns empty, and any doc still referencing a private
    command fails the contract check — exactly the drift we want flagged.
    """
    pyproject = REPO_ROOT / "bobi_deploy" / "pyproject.toml"
    if not pyproject.exists():
        return frozenset()
    data = tomllib.loads(pyproject.read_text())
    entry_points = data.get("project", {}).get("entry-points", {})
    return frozenset(entry_points.get("bobi.commands", {}))


_PLUGIN_COMMANDS = _plugin_commands()

# bobi invocations inside fenced code blocks or inline code spans.
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE = re.compile(r"`[^`\n]+`")
_INVOCATION = re.compile(r"\bbobi(?:\s+[^#\n;&|`]*)?")
_COMMAND_PREFIXES = {
    "cd", "env", "exec", "fly", "gosu", "pipx", "python", "python3",
    "sudo", "timeout", "uv", "xargs",
}

_LEGACY_HOME_FRAGMENT = r"\." + "bobi"
_PER_PROJECT_PHRASE = r"per-" + "project"

_STALE_PATHS = [
    (
        re.compile(
            r"(?<!\$BOBI_HOME/)(?<!~/)" + _LEGACY_HOME_FRAGMENT + r"/"
            r"(agent\.yaml|\.env|roles|tools|workflows|monitors|context|sessions|state|kb)\b"
        ),
        "use $BOBI_HOME/agents/<name>/run/{package,state,workspace} paths",
    ),
    (
        re.compile(r"(?<!~/)(?<!/data/)" + _LEGACY_HOME_FRAGMENT + r"/"),
        "unqualified project-local runtime paths are obsolete; use BOBI_HOME/run paths",
    ),
    (
        re.compile(
            r"\bNo\s+global\s+`?~?/?" + _LEGACY_HOME_FRAGMENT + r"/?`?",
            re.IGNORECASE,
        ),
        "BOBI_HOME defaults to ~/.bobi and is configurable by environment variable",
    ),
    (
        re.compile(r"\b" + _PER_PROJECT_PHRASE + r" (config|install|installation)\b", re.IGNORECASE),
        "Bobi Agents are machine-wide slots under BOBI_HOME, not cwd-scoped installs",
    ),
]


def _contract_files() -> list[Path]:
    files = [REPO_ROOT / "bobi" / "prompts" / "base.md"]

    for root in ["docs", "skills", ".claude/commands"]:
        root_path = REPO_ROOT / root
        if root_path.is_dir():
            files += sorted(root_path.rglob("*.md"))

    for filename in ["README.md", "AGENTS.md"]:
        path = REPO_ROOT / filename
        if path.exists():
            files.append(path)

    agents_dir = REPO_ROOT / "agents"
    for pack in sorted(agents_dir.iterdir()):
        if pack.is_dir():
            files += sorted(pack.glob("agent.md"))
            files += sorted(pack.glob("agent.yaml"))
            files += sorted(pack.glob("tools/*.md"))
            files += sorted(pack.glob("roles/*/ROLE.md"))
            files += sorted(pack.glob("workflows/*.yaml"))
            files += sorted(pack.glob("monitors/*.yaml"))
            files += sorted(pack.glob("workspace/**/*.md"))
    return [f for f in files if f.exists()]


def _code_lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in _FENCE.findall(text):
        inner = block.strip("`")
        parts = inner.splitlines()
        lines.extend(parts[1:] if parts and re.match(r"^[a-zA-Z0-9_-]+$", parts[0]) else parts)

    # Strip fences before scanning inline spans so blocks aren't re-matched.
    for span in _INLINE.findall(_FENCE.sub("", text)):
        inner = span.strip("`").strip()
        if inner.startswith("bobi"):
            lines.append(inner)

    return [s.replace("\\\n", " ") for s in lines]


def _split_invocation(invocation: str) -> list[str]:
    invocation = invocation.strip().strip("`").strip()
    invocation = re.sub(r"^\$\s*", "", invocation)
    invocation = invocation.rstrip(".,)")
    try:
        return shlex.split(invocation)
    except ValueError:
        return invocation.split()


def test_linear_setup_documents_worker_webhook_secret():
    text = (REPO_ROOT / "skills" / "linear-setup.md").read_text()
    assert "<event-server-url>/webhooks/linear" in text
    assert "LINEAR_WEBHOOK_SECRET" in text
    assert "wrangler secret put LINEAR_WEBHOOK_SECRET" in text


def _extract_bobi_tokens(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    stripped = re.sub(r"^\$\s*", "", stripped)

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    bobi_indexes = [i for i, token in enumerate(tokens) if token == "bobi"]
    if not bobi_indexes:
        return None

    index: int | None = None
    for candidate in bobi_indexes:
        if candidate == 0:
            index = candidate
            break
        next_token = tokens[candidate + 1] if candidate + 1 < len(tokens) else ""
        if next_token.startswith("-") or next_token in cli_main.commands:
            index = candidate
            break
    if index is None:
        return None

    prefix = tokens[:index]
    if prefix:
        first = prefix[0]
        first_is_env = "=" in first and not first.startswith("-")
        if first not in _COMMAND_PREFIXES and not first_is_env:
            return None

    trailing: list[str] = []
    for token in tokens[index:]:
        if token in {"&&", "||", "|", ";"}:
            break
        trailing.append(token)
    return tuple(trailing) if trailing else None


def _referenced_invocations(text: str) -> set[tuple[str, ...]]:
    invocations: set[tuple[str, ...]] = set()
    for line in _code_lines(text):
        tokens = _extract_bobi_tokens(line)
        if tokens:
            invocations.add(tokens)
    return invocations


def _command_error(tokens: tuple[str, ...]) -> str | None:
    if len(tokens) == 1:
        return None

    command = tokens[1]
    if command.startswith("-"):
        return None
    # Resolve through the group, not `.commands`: plugin commands (the
    # `bobi.commands` entry points, e.g. bobi-deploy's deploy/destroy) are
    # served lazily by _PluginGroup.get_command and never live in the dict.
    # When the plugin is not installed (the public unit suite), fall back to
    # its on-disk entry-point declarations.
    if cli_main.get_command(None, command) is None and command not in _PLUGIN_COMMANDS:
        return f"`bobi {command}` is not a public top-level command"

    if command == "agents":
        return _subcommand_error(cli_main.commands["agents"], tokens[2:], "bobi agents")

    if command == "agent":
        if len(tokens) == 2:
            return None
        agent_name = tokens[2]
        if agent_name.startswith("-"):
            return None
        return _subcommand_error(cli_main.commands["agent"], tokens[3:], "bobi agent <name>")

    return None


def _subcommand_error(group, tokens: tuple[str, ...], prefix: str) -> str | None:
    if not tokens:
        return None
    subcommand = tokens[0]
    if subcommand.startswith("-"):
        return None
    if not hasattr(group, "commands") or subcommand not in group.commands:
        return f"`{prefix} {subcommand}` is not a public command"

    child = group.commands[subcommand]
    if hasattr(child, "commands") and len(tokens) > 1:
        next_token = tokens[1]
        if not next_token.startswith("-") and next_token in child.commands:
            return None
        if not next_token.startswith("-") and subcommand in {"subagents", "workflows", "monitors", "roles", "transcript", "kb", "event-server"}:
            return f"`{prefix} {subcommand} {next_token}` is not a public command"
    return None


@pytest.mark.parametrize("path", _contract_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_bobi_contract_references_are_current(path):
    text = path.read_text()
    errors: list[str] = []

    for tokens in sorted(_referenced_invocations(text)):
        error = _command_error(tokens)
        if error:
            errors.append(f"{' '.join(tokens)}: {error}")

    for pattern, guidance in _STALE_PATHS:
        for match in pattern.finditer(text):
            errors.append(f"{match.group(0)!r}: {guidance}")

    assert not errors, (
        f"{path.relative_to(REPO_ROOT)} has stale bobi contract reference(s):\n"
        + "\n".join(f"  - {error}" for error in errors)
    )
