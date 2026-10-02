"""Redact secrets from GitHub comments posted by agent shell commands."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import MutableMapping, Sequence, TextIO

REAL_GH_ENV = "BOBI_REAL_GH"
PYTHON_EXECUTABLE_ENV = "BOBI_PYTHON_EXECUTABLE"

_BODY_FLAGS = ("--body", "-b")
_BODY_FILE_FLAGS = ("--body-file", "-F")


def shim_dir() -> Path:
    return Path(__file__).with_name("bin")


def _without_shim(path: str) -> str:
    wrapper = shim_dir().resolve()
    kept = []
    for part in path.split(os.pathsep):
        if not part:
            kept.append(part)
            continue
        try:
            if Path(part).resolve() == wrapper:
                continue
        except OSError:
            pass
        kept.append(part)
    return os.pathsep.join(kept)


def _real_gh(env: MutableMapping[str, str]) -> str:
    configured = env.get(REAL_GH_ENV, "")
    if (
        configured
        and Path(configured).is_file()
        and os.access(configured, os.X_OK)
    ):
        return configured
    return shutil.which("gh", path=_without_shim(env.get("PATH", ""))) or ""


def install_github_comment_redaction(
    env: MutableMapping[str, str] | None = None,
) -> None:
    """Put Bobi's ``gh`` shim first in the agent tool PATH."""
    target = os.environ if env is None else env
    real_gh = _real_gh(target)
    if not real_gh:
        target.pop(REAL_GH_ENV, None)
        target.pop(PYTHON_EXECUTABLE_ENV, None)
        if "PATH" in target:
            target["PATH"] = _without_shim(target["PATH"])
        return
    target[REAL_GH_ENV] = real_gh
    target[PYTHON_EXECUTABLE_ENV] = sys.executable

    wrapper = str(shim_dir())
    parts = [part for part in target.get("PATH", "").split(os.pathsep) if part]
    target["PATH"] = os.pathsep.join(
        [wrapper, *(part for part in parts if part != wrapper)]
    )


async def _protect_github_shell(input_data: dict, tool_use_id: str | None, context: dict) -> dict:
    if input_data.get("tool_name") != "Bash":
        return {}
    tool_input = input_data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return {"continue": False, "stopReason": "bobi: Bash command must be inspectable"}
    prefix = f'export PATH={shlex.quote(str(shim_dir()))}:"$PATH"; '
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": {**tool_input, "command": prefix + command},
    }}


def github_comment_hooks(hooks: dict | None) -> dict | None:
    if not os.environ.get(REAL_GH_ENV):
        return hooks
    from claude_agent_sdk import HookMatcher

    protected = dict(hooks or {})
    protected["PreToolUse"] = [
        *protected.get("PreToolUse", []),
        HookMatcher(matcher="Bash", hooks=[_protect_github_shell]),
    ]
    return protected


def _is_comment_command(argv: Sequence[str]) -> bool:
    commands: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in (*_BODY_FLAGS, *_BODY_FILE_FLAGS, "-R", "--repo"):
            index += 2
            continue
        if not arg.startswith("-"):
            commands.append(arg)
            if len(commands) == 2:
                return commands[0] in {"issue", "pr"} and commands[1] == "comment"
        index += 1
    return False


def _flag_value(argv: Sequence[str], index: int, flag: str) -> tuple[str, int]:
    arg = argv[index]
    if arg == flag:
        if index + 1 >= len(argv):
            raise ValueError(f"{flag} requires a value")
        return argv[index + 1], 2
    return arg[len(flag) :].removeprefix("="), 1


def _redacted_comment(
    argv: Sequence[str], stdin: TextIO,
) -> tuple[list[str], str, int]:
    from bobi.setup.actions import redact_secrets

    clean_args: list[str] = []
    body: str | None = None
    insert_at: int | None = None
    read_only = {"--help": False, "--delete-last": False}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            clean_args.extend(argv[index:])
            break
        if arg in ("-R", "--repo"):
            if index + 1 >= len(argv):
                raise ValueError(f"{arg} requires a value")
            clean_args.extend(argv[index:index + 2])
            index += 2
            continue
        boolean_flag, separator, value = arg.partition("=")
        if boolean_flag == "-h":
            boolean_flag = "--help"
        if boolean_flag in read_only:
            if separator and value not in {"1", "t", "T", "true", "TRUE", "True",
                                           "0", "f", "F", "false", "FALSE", "False"}:
                raise ValueError(f"{boolean_flag} requires a boolean")
            read_only[boolean_flag] = not separator or value in {
                "1", "t", "T", "true", "TRUE", "True",
            }
        matched = False
        for flag in _BODY_FLAGS:
            if arg == flag or arg.startswith(f"{flag}=") or (
                len(flag) == 2 and arg.startswith(flag)
            ):
                if body is not None:
                    raise ValueError("GitHub comment body was provided more than once")
                value, consumed = _flag_value(argv, index, flag)
                body = value
                insert_at = len(clean_args)
                index += consumed
                matched = True
                break
        if matched:
            continue
        for flag in _BODY_FILE_FLAGS:
            if arg == flag or arg.startswith(f"{flag}=") or (
                len(flag) == 2 and arg.startswith(flag)
            ):
                if body is not None:
                    raise ValueError("GitHub comment body was provided more than once")
                value, consumed = _flag_value(argv, index, flag)
                body = stdin.read() if value == "-" else Path(value).read_text()
                insert_at = len(clean_args)
                index += consumed
                matched = True
                break
        if matched:
            continue
        clean_args.append(arg)
        index += 1

    if body is None:
        if any(read_only.values()):
            return list(argv), "", 0
        raise ValueError(
            "GitHub comments must use --body or --body-file so Bobi can redact secrets"
        )

    redacted, count = redact_secrets(body)
    position = len(clean_args) if insert_at is None else insert_at
    clean_args[position:position] = ["--body-file", "-"]
    return clean_args, redacted, count


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    real_gh = _real_gh(os.environ)
    if not real_gh:
        print("bobi: could not find the real gh executable", file=sys.stderr)
        return 127

    stdin_text: str | None = None
    if _is_comment_command(args):
        try:
            args, stdin_text, count = _redacted_comment(args, sys.stdin)
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"bobi: {exc}", file=sys.stderr)
            return 2
        if count:
            print(
                f"bobi: redacted {count} secret value(s) from GitHub comment",
                file=sys.stderr,
            )

    completed = subprocess.run(
        [real_gh, *args],
        input=stdin_text,
        text=stdin_text is not None,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
