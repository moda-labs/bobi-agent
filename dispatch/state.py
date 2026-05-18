"""Track active agent sessions. Minimal — Linear is the source of truth."""

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from .config import STATE_PATH


@dataclass
class AgentSession:
    """An active or recently completed agent session."""

    issue_id: str
    repo_path: str
    title: str
    worktree: str
    started_at: float
    last_activity_at: float
    last_phase: str = ""
    attempts: int = 1
    linear_issue_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSession":
        # Backwards compat: ignore old 'pid' field
        d = {k: v for k, v in d.items() if k != "pid"}
        return cls(**d)


class StateStore:
    """Track active sessions and their worktrees.

    Linear is the source of truth for issue state.
    This store tracks which tmux sessions are active.
    """

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self._agents: dict[str, AgentSession] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        agents = raw.get("agents", raw.get("items", {}))
        for k, v in agents.items():
            try:
                self._agents[k] = AgentSession.from_dict(v)
            except (TypeError, KeyError):
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"agents": {k: v.to_dict() for k, v in self._agents.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(self.path)

    def is_tracked(self, issue_id: str) -> bool:
        return issue_id in self._agents

    def track(self, issue_id: str, repo_path: str, title: str,
              worktree: str, linear_issue_id: str | None = None) -> None:
        prev = self._agents.get(issue_id)
        attempts = (prev.attempts + 1) if prev else 1
        now = time.time()

        self._agents[issue_id] = AgentSession(
            issue_id=issue_id,
            repo_path=repo_path,
            title=title,
            worktree=worktree,
            started_at=now,
            last_activity_at=now,
            attempts=attempts,
            linear_issue_id=linear_issue_id,
        )
        self._save()

    def touch(self, issue_id: str) -> None:
        if issue_id in self._agents:
            self._agents[issue_id].last_activity_at = time.time()
            self._save()

    def set_phase(self, issue_id: str, phase: str) -> None:
        if issue_id in self._agents:
            self._agents[issue_id].last_phase = phase
            self._save()

    def remove(self, issue_id: str) -> None:
        if issue_id in self._agents:
            del self._agents[issue_id]
            self._save()

    def get(self, issue_id: str) -> AgentSession | None:
        return self._agents.get(issue_id)

    def all_agents(self) -> list[AgentSession]:
        return list(self._agents.values())

    def agents_for_repo(self, repo_path: str) -> list[AgentSession]:
        return [a for a in self._agents.values() if a.repo_path == repo_path]
