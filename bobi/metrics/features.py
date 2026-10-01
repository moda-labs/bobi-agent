"""Bounded, opt-in feature construction without transcript or filesystem reads."""

from pathlib import PurePosixPath, PureWindowsPath
import re

from bobi.redact import redact_secrets

_ABSOLUTE_PATH = re.compile(
    r'''(?P<quote>["'`])(?P<quoted>(?:[A-Za-z]:[\\/]|\\\\|/)[^\r\n]*?)(?P=quote)'''
    r'''|(?<![\w:/])(?:[A-Za-z]:[\\/]|\\\\|/)[^\s"'<>`]+'''
)

def prepare_task(prompt: str, *, repo_path: str, max_bytes: int) -> tuple[str, int]:
    if max_bytes < 1:
        raise ValueError("prompt byte limit must be positive")
    memory = re.search(r"(?m)^## Long-Term Memory\s*$", prompt)
    if memory:
        prompt = prompt[:memory.start()]
    prompt, redactions = redact_secrets(prompt)
    windows_root = PureWindowsPath(repo_path)
    root = (windows_root if windows_root.is_absolute()
            else PurePosixPath(repo_path) if repo_path.startswith("/") else None)

    def replace_path(match: re.Match[str]) -> str:
        nonlocal redactions
        value = match.group("quoted") or match.group()
        quote = match.group("quote") or ""
        path = PureWindowsPath(value) if PureWindowsPath(value).is_absolute() else PurePosixPath(value)
        if root and ".." not in path.parts:
            try:
                return quote + path.relative_to(root).as_posix() + quote
            except ValueError:
                pass
        redactions += 1
        return quote + "[path]" + quote

    prompt = _ABSOLUTE_PATH.sub(replace_path, prompt)
    encoded = prompt.encode("utf-8")
    if len(encoded) > max_bytes:
        separator = b"\n...\n" if max_bytes >= 5 else b""
        remaining = max_bytes - len(separator)
        head = (remaining + 1) // 2
        tail = remaining // 2
        prompt = (encoded[:head].decode("utf-8", errors="ignore")
                  + separator.decode()
                  + (encoded[-tail:].decode("utf-8", errors="ignore") if tail else ""))
    return prompt, redactions

def build_features(*, prompt: str, repo_path: str, entry_point: str, role: str,
                   brain: str, phase: str = "", workflow_name: str = "",
                   step_name: str = "", prompt_egress: str,
                   max_prompt_bytes: int = 8192) -> tuple[dict[str, object], int]:
    if prompt_egress not in {"none", "redacted"}:
        raise ValueError("explicit prompt egress is required")
    features: dict[str, object] = {"prompt_bytes": len(prompt.encode("utf-8"))}
    for name, value in {
        "entry_point": entry_point, "role": role, "brain": brain, "phase": phase,
        "workflow_name": workflow_name, "step_name": step_name,
    }.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{0,128}", value):
            raise ValueError("invalid routing feature identifier")
        features[name] = value
    if prompt_egress == "none":
        return features, 0
    task, redactions = prepare_task(prompt, repo_path=repo_path, max_bytes=max_prompt_bytes)
    features["task"] = task
    return features, redactions
