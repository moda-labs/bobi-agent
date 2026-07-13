"""Default Claude hook policy for Bobi-owned install paths."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

WRITE_GUARD_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"
_PROTECTED_DIR_NAMES = {".venv", "venv", "node_modules", ".tox", ".nox",
                        "__pycache__"}
_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_MUTATING_COMMANDS = {
    "cp", "mv", "rm", "touch", "mkdir", "chmod", "chown", "tee",
    "install", "rsync", "pip", "uv",
}
_SHELL_SEPARATORS = {"&&", "||", ";", "|"}


def protected_roots(cwd: str | Path | None = None) -> list[Path]:
    """Return framework/package-managed roots agents must not patch directly."""
    roots: list[Path] = []
    cwd_path = _resolve_path(Path(cwd)) if cwd else None

    try:
        import bobi
        package_root = _resolve_path(Path(bobi.__file__).parent)
        if not _is_editable_source_checkout(package_root, cwd_path):
            roots.append(package_root)
            site_root = _nearest_site_packages(package_root)
            if site_root is not None:
                roots.append(site_root)
    except Exception:
        pass

    try:
        from importlib.metadata import distribution
        dist = distribution("bobi")
        dist_root = _resolve_path(Path(dist.locate_file("")))
        if not _is_editable_source_checkout(dist_root, cwd_path):
            roots.append(dist_root)
    except Exception:
        pass

    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        roots.append(_resolve_path(Path(sys.prefix)))

    if cwd_path is not None:
        for parent in (cwd_path, *cwd_path.parents):
            for name in _PROTECTED_DIR_NAMES:
                roots.append(_resolve_path(parent / name))
            if parent.name in _PROTECTED_DIR_NAMES:
                roots.append(_resolve_path(parent))

    return _dedupe_roots(roots)


def is_protected_agent_write(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    cwd: str | Path | None = None,
) -> bool:
    """Whether this Claude tool use would mutate a protected install path."""
    tool_input = tool_input or {}
    if tool_name in _PATH_KEYS:
        raw = tool_input.get(_PATH_KEYS[tool_name])
        return _path_is_protected(raw, cwd)
    if tool_name == "Bash":
        return _bash_writes_protected_path(str(tool_input.get("command") or ""), cwd)
    return False


def make_default_pre_tool_use_hooks(
    cwd: str | Path | None,
    existing: dict | None = None,
) -> dict:
    """Prepend Bobi's default write guard to caller-provided Claude hooks."""
    from claude_agent_sdk import HookMatcher

    hooks = {
        event: list(matchers)
        for event, matchers in (existing or {}).items()
    }

    async def _guard(input_data, tool_use_id, context):
        tool_name, tool_input = _tool_from_hook_input(input_data)
        if not is_protected_agent_write(tool_name, tool_input, cwd):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Bobi blocks agents from editing its installed framework, "
                    "venv, or package-managed dependency directories. Report "
                    "the attempted change and implement it in the source repo "
                    "via PR instead."
                ),
            }
        }

    guard = HookMatcher(matcher=WRITE_GUARD_MATCHER, hooks=[_guard])
    hooks["PreToolUse"] = [guard] + hooks.get("PreToolUse", [])
    return hooks


def _tool_from_hook_input(input_data: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(input_data, dict):
        return "", {}
    tool_name = (
        input_data.get("tool_name")
        or input_data.get("toolName")
        or input_data.get("name")
        or ""
    )
    tool_input = (
        input_data.get("tool_input")
        or input_data.get("toolInput")
        or input_data.get("input")
        or {}
    )
    return str(tool_name), tool_input if isinstance(tool_input, dict) else {}


def _bash_writes_protected_path(command: str, cwd: str | Path | None) -> bool:
    if not command.strip():
        return False
    tokens = _shell_tokens(command)
    command_cwd = _resolve_candidate(".", cwd)
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SHELL_SEPARATORS:
            if _segment_writes_protected_path(segment, command_cwd):
                return True
            next_cwd = _segment_cd_target(segment, command_cwd)
            if next_cwd is not None:
                command_cwd = next_cwd
            segment = []
        else:
            segment.append(token)
    return False


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _segment_writes_protected_path(
    segment: list[str],
    cwd: Path | None,
) -> bool:
    if not segment:
        return False
    for target in _segment_redirection_targets(segment):
        if _path_is_protected(target, cwd):
            return True
    command_idx = _command_index(segment)
    if command_idx is None:
        return False
    command_token = segment[command_idx]
    command = Path(command_token).name
    args = segment[command_idx + 1:]
    if command in {"pip", "uv"} and _path_is_protected(command_token, cwd):
        return True
    if command in {"python", "python3"} and _runs_pip_module(args):
        return _path_is_protected(command_token, cwd) or _any_operand_protected(args, cwd)
    if command in {"sed", "perl"} and _has_in_place_flag(args):
        return _any_operand_protected(args, cwd)
    if command not in _MUTATING_COMMANDS:
        return False
    operands = _command_operands(args)
    if command == "cp":
        return bool(operands) and _path_is_protected(operands[-1], cwd)
    return any(_path_is_protected(operand, cwd) for operand in operands)


def _segment_cd_target(segment: list[str], cwd: Path | None) -> Path | None:
    command_idx = _command_index(segment)
    if command_idx is None or Path(segment[command_idx]).name != "cd":
        return None
    if command_idx + 1 >= len(segment):
        return None
    return _resolve_candidate(segment[command_idx + 1], cwd)


def _command_index(segment: list[str]) -> int | None:
    idx = 0
    if segment and segment[0] == "env":
        idx = 1
        while idx < len(segment) and "=" in segment[idx] and not segment[idx].startswith("="):
            idx += 1
    if idx >= len(segment):
        return None
    return idx


def _segment_redirection_targets(segment: list[str]) -> list[str]:
    targets: list[str] = []
    for idx, token in enumerate(segment[:-1]):
        if token in {">", ">>", ">|", "&>", "&>>"}:
            targets.append(segment[idx + 1])
    return targets


def _command_operands(tokens: list[str]) -> list[str]:
    operands: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in {">", ">>", ">|", "&>", "&>>", "<", "<<"}:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if "=" in token and not token.startswith(("/", ".", "~")):
            continue
        operands.append(token)
    return operands


def _has_in_place_flag(tokens: list[str]) -> bool:
    return any(token == "-i" or (token.startswith("-") and "i" in token)
               for token in tokens)


def _runs_pip_module(tokens: list[str]) -> bool:
    for idx, token in enumerate(tokens[:-1]):
        if token == "-m" and tokens[idx + 1] == "pip":
            return True
    return False


def _any_operand_protected(tokens: list[str], cwd: Path | None) -> bool:
    return any(_path_is_protected(operand, cwd)
               for operand in _command_operands(tokens))


def _path_is_protected(raw_path: Any, cwd: str | Path | None) -> bool:
    if not raw_path:
        return False
    candidate = _resolve_candidate(raw_path, cwd)
    if candidate is None:
        return False
    for root in protected_roots(cwd):
        if _is_relative_to(candidate, root):
            return True
    return False


def _resolve_candidate(raw_path: Any, cwd: str | Path | None) -> Path | None:
    try:
        candidate = Path(str(raw_path)).expanduser()
    except Exception:
        return None
    if not candidate.is_absolute():
        base = Path(cwd) if cwd else Path.cwd()
        candidate = base / candidate
    return _resolve_path(candidate)


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _nearest_site_packages(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if parent.name in {"site-packages", "dist-packages"}:
            return parent
    return None


def _is_editable_source_checkout(package_root: Path, cwd: Path | None) -> bool:
    if cwd is None or not _is_relative_to(package_root, cwd):
        return False
    pyproject = cwd / "pyproject.toml"
    return pyproject.exists() and (cwd / "bobi").exists()


def _dedupe_roots(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out
