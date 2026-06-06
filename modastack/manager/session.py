"""Manager session — thin wrapper around Session.

The manager is just a Session configured with the manager prompt,
Slack callback, and strict MCP config. All communication goes
through the inbox.
"""

from __future__ import annotations

import logging
from pathlib import Path

from modastack.session import Session
from modastack.sdk import _sessions_dir, load_session_id

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MANAGER_BASE_PATH = _PROMPTS_DIR / "manager_base.md"


class ManagerSession:
    """A manager session bound to one project — wraps a Session."""

    def __init__(self, project_path: Path, session_name: str | None = None):
        self.project_path = project_path
        self.session_name = session_name or f"moda-mgr-{project_path.name}"
        self.cwd = str(project_path)
        self._session: Session | None = None
        self._response_callback = None

    def _load_manager_prompt(self) -> str:
        core = MANAGER_BASE_PATH.read_text()

        builtin_role = MANAGER_BASE_PATH.parent / "manager_engineering.md"
        if builtin_role.exists():
            core += "\n\n" + builtin_role.read_text()

        repo_mgr = self.project_path / ".modastack" / "manager.md"
        if repo_mgr.exists():
            core += f"\n\n## {self.project_path.name} policies\n\n" + repo_mgr.read_text()

        return core

    def _list_workflows(self) -> str:
        try:
            from modastack.workflow.triggers import WORKFLOWS_DIR
            from modastack.workflow.schema import load_workflow

            sources = [WORKFLOWS_DIR]
            repo_wf = self.project_path / ".modastack" / "workflows"
            if repo_wf.exists():
                sources.append(repo_wf)

            seen: set[str] = set()
            lines: list[str] = []
            for d in reversed(sources):
                for f in sorted(d.glob("*.yaml")):
                    if f.stem in seen:
                        continue
                    seen.add(f.stem)
                    try:
                        wf = load_workflow(f)
                        lines.append(f"- {wf.name}: trigger={wf.trigger.event}, {len(wf.nodes)} nodes")
                    except Exception:
                        continue
            return "\n".join(lines) if lines else "No workflows found."
        except Exception:
            return ""

    def _build_startup_prompt(self) -> str:
        prompt = self._load_manager_prompt()
        workflows = self._list_workflows()
        return (
            f"You are the Modastack manager for {self.project_path.name}. "
            f"You manage this project only — no other projects. "
            f"All agents you launch run in this project. "
            f"Act directly using your tools.\n\n{prompt}\n\n"
            f"## Available workflows\n\n{workflows}"
        )

    def start_or_resume(self) -> bool:
        if self.is_alive():
            log.info(f"Manager session '{self.session_name}' already running")
            return True

        if self._session:
            self._session.stop()

        prompt = self._build_startup_prompt()

        self._session = Session(
            name=self.session_name,
            cwd=self.cwd,
            system_prompt={"type": "preset", "preset": "claude_code"},
            on_response=self._response_callback,
            extra_options={"strict_mcp_config": True},
            role="manager",
        )

        ok = self._session.start(startup_prompt=prompt)
        if ok:
            log.info(f"Manager session '{self.session_name}' ready (port={self._session.port})")
        return ok

    def inject_capture(
        self, text: str, timeout: int = 300, wait_for_ready: int = 0
    ) -> tuple[bool, str]:
        from modastack.inbox import deliver

        effective_timeout = max(timeout, wait_for_ready) if wait_for_ready else timeout
        return deliver(
            self.session_name, text, sender="internal",
            wait=True, timeout=effective_timeout,
        )

    def inject(self, text: str, timeout: int = 300, wait_for_ready: int = 0) -> bool:
        from modastack.inbox import deliver

        ok, _ = deliver(self.session_name, text, sender="internal", wait=False)
        return ok

    def last_inject_error(self) -> str:
        return ""

    def detect_state(self) -> str:
        if self._session:
            return self._session.detect_state()
        return "stopped"

    def set_response_callback(self, fn) -> None:
        self._response_callback = fn
        if self._session:
            self._session._on_response = fn

    def wait_until_ready(self, timeout: int = 60) -> bool:
        if self._session:
            return self._session.wait_until_ready(timeout)
        return False

    def is_alive(self) -> bool:
        if self._session:
            return self._session.is_alive()
        return False

    def read_last_response(self) -> str | None:
        if self._session:
            return self._session._last_response or None
        return None

    def capture(self, lines: int = 50) -> str:
        if self._session:
            return self._session._last_response or "(no response yet)"
        return "(no session)"

    def get_session_id(self) -> str:
        if self._session:
            return self._session.get_session_id()
        return load_session_id(self.session_name)


# ---------------------------------------------------------------------------
# Backward-compat module-level API
# ---------------------------------------------------------------------------

_default_session: ManagerSession | None = None


def set_default_session(session: ManagerSession) -> None:
    global _default_session
    _default_session = session


def get_default_session() -> ManagerSession | None:
    return _default_session


def start_or_resume(cwd: str = None) -> bool:
    if _default_session is None:
        log.warning("No default session configured")
        return False
    return _default_session.start_or_resume()


def inject_capture(
    text: str, timeout: int = 300, wait_for_ready: int = 0
) -> tuple[bool, str]:
    if _default_session is None:
        return False, ""
    return _default_session.inject_capture(text, timeout=timeout, wait_for_ready=wait_for_ready)


def inject(text: str, timeout: int = 300, wait_for_ready: int = 0) -> bool:
    if _default_session is None:
        return False
    return _default_session.inject(text, timeout=timeout, wait_for_ready=wait_for_ready)


def last_inject_error() -> str:
    if _default_session is None:
        return "no session"
    return _default_session.last_inject_error()


def detect_state() -> str:
    if _default_session is None:
        return "stopped"
    return _default_session.detect_state()


def set_response_callback(fn) -> None:
    if _default_session is not None:
        _default_session.set_response_callback(fn)


def wait_until_ready(timeout: int = 60) -> bool:
    if _default_session is None:
        return False
    return _default_session.wait_until_ready(timeout)


def is_alive() -> bool:
    if _default_session is None:
        return False
    return _default_session.is_alive()


def read_last_response() -> str | None:
    if _default_session is None:
        return None
    return _default_session.read_last_response()


def capture(lines: int = 50) -> str:
    if _default_session is None:
        return "(no session)"
    return _default_session.capture(lines)


def get_session_id() -> str:
    if _default_session is None:
        return ""
    return _default_session.get_session_id()
