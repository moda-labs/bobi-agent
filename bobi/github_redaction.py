"""Redact secrets from GitHub comments posted by agent shell commands."""

from __future__ import annotations

import os
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
        return
    target[REAL_GH_ENV] = real_gh
    target[PYTHON_EXECUTABLE_ENV] = sys.executable

    wrapper = str(shim_dir())
    parts = [part for part in target.get("PATH", "").split(os.pathsep) if part]
    target["PATH"] = os.pathsep.join(
        [wrapper, *(part for part in parts if part != wrapper)]
    )


def _is_comment_command(argv: Sequence[str]) -> bool:
    return any(
        argv[index] in {"issue", "pr"} and argv[index + 1] == "comment"
        for index in range(len(argv) - 1)
    )


def _flag_value(argv: Sequence[str], index: int, flag: str) -> tuple[str, int]:
    arg = argv[index]
    if arg == flag:
        if index + 1 >= len(argv):
            raise ValueError(f"{flag} requires a value")
        return argv[index + 1], 2
    return arg[len(flag) + 1 :], 1


def _redacted_comment(
    argv: Sequence[str], stdin: TextIO,
) -> tuple[list[str], str, int]:
    from bobi.setup.actions import redact_secrets

    clean_args: list[str] = []
    body: str | None = None
    insert_at: int | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        matched = False
        for flag in _BODY_FLAGS:
            if arg == flag or arg.startswith(f"{flag}="):
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
            if arg == flag or arg.startswith(f"{flag}="):
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
        if any(flag in argv for flag in ("--delete-last", "--help", "-h")):
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
