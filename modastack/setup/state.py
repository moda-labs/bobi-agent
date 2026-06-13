"""Setup state machine: stages, gating rules, and persistence.

The session's system prompt describes the stages; this module is what
actually enforces them. Tool handlers call `can_advance` / `require_stage`
and refuse with an actionable reason instead of trusting the prompt.

State is checkpointed to .modastack/state/setup.json after every tool
call so an interrupted setup resumes with `modastack setup --resume`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

from modastack import paths

STATE_FILENAME = "setup.json"

# The seven interview questions, by canonical key. The first four must
# have substantive answers before the interview can close; the last
# three must be recorded but may be "none".
INTERVIEW_KEYS = [
    "purpose",          # 1. what is this agent going to do?
    "roles",            # 2. distinct roles and the job each performs
    "services",         # 3. services the team reads from / writes to
    "chat",             # 4. interaction channel (slack | telegram | none)
    "schedules",        # 5. recurring jobs ("none" allowed)
    "event_triggers",   # 6. events to react to ("none" allowed)
    "gates",            # 7. human-in-the-loop gates ("none" allowed)
]
REQUIRED_INTERVIEW_KEYS = INTERVIEW_KEYS[:4]


class Stage(str, Enum):
    CHOOSE = "choose"
    INTERVIEW = "interview"
    SERVICES = "services"
    DISCOVERY = "discovery"
    GENERATE = "generate"
    INSTALL = "install"
    DONE = "done"


STAGE_ORDER = list(Stage)


@dataclass
class SetupState:
    stage: Stage = Stage.CHOOSE
    branch: str = ""                 # "use-as-is" | "build"
    team_name: str = ""
    answers: dict = field(default_factory=dict)
    credentials_saved: list = field(default_factory=list)
    monitors_recorded: list = field(default_factory=list)
    discovery_skipped_reason: str = ""
    validated: bool = False
    validated_hash: str = ""         # source tree hash at validation time
    installed: bool = False
    finished: bool = False
    session_id: str = ""
    stage_summaries: dict = field(default_factory=dict)

    # --- gating ---------------------------------------------------------

    def require_stage(self, *stages: Stage) -> str | None:
        """None when the current stage is one of `stages`, else a refusal."""
        if self.stage in stages:
            return None
        allowed = " or ".join(f"'{s.value}'" for s in stages)
        return (
            f"this tool is only available in stage {allowed}; "
            f"setup is currently in '{self.stage.value}'"
        )

    def can_advance(self, to: Stage) -> str | None:
        """None when the transition is legal, else the refusal reason."""
        cur = self.stage
        if to == cur:
            return f"already in stage '{cur.value}'"

        # The one sanctioned jump: a picked team goes straight to install.
        if cur == Stage.CHOOSE and to == Stage.INSTALL:
            if self.branch != "use-as-is":
                return "skipping to install requires branch 'use-as-is' (call select_team first)"
            if not self.team_name:
                return "no team selected (call select_team first)"
            return None

        # Discovery is skippable, but only deliberately.
        if cur == Stage.SERVICES and to == Stage.GENERATE:
            if not self.discovery_skipped_reason:
                return (
                    "discovery has not happened — either advance to 'discovery' "
                    "and explore venn tools, or call skip_discovery with a reason"
                )
            return None

        if STAGE_ORDER.index(to) != STAGE_ORDER.index(cur) + 1:
            return (
                f"cannot move from '{cur.value}' to '{to.value}' — stages "
                f"advance in order: {' → '.join(s.value for s in STAGE_ORDER)}"
            )

        # Leaving discovery requires having actually discovered something
        # or a deliberate skip — otherwise passing through the stage
        # without a single probe would hollow out the skip gate above.
        if cur == Stage.DISCOVERY and to == Stage.GENERATE:
            if not self.monitors_recorded and not self.discovery_skipped_reason:
                return (
                    "discovery produced nothing — record at least one "
                    "monitor (record_monitor) or call skip_discovery with "
                    "a reason"
                )

        if to == Stage.INTERVIEW:
            if self.branch != "build":
                return "the interview is for branch 'build' (call select_team first)"
            if not self.team_name:
                return "no pack name chosen (call select_team first)"
        elif to == Stage.SERVICES:
            missing = [k for k in REQUIRED_INTERVIEW_KEYS
                       if not str(self.answers.get(k, "")).strip()]
            unrecorded = [k for k in INTERVIEW_KEYS if k not in self.answers]
            if missing or unrecorded:
                gaps = sorted(set(missing) | set(unrecorded))
                return (
                    "the interview is incomplete — record answers for: "
                    + ", ".join(gaps)
                    + " (schedules/event_triggers/gates may be 'none', but say so)"
                )
        elif to == Stage.INSTALL:
            if not self.validated:
                return "the team source has not passed validate_team yet"
        elif to == Stage.DONE:
            if not self.installed:
                return "the team is not installed yet (call install_team)"
        return None

    # --- persistence ----------------------------------------------------

    def save(self, project_path: Path) -> None:
        data = asdict(self)
        data["stage"] = self.stage.value
        path = paths.state_dir(project_path) / STATE_FILENAME
        path.write_text(json.dumps(data, indent=1))

    @classmethod
    def load(cls, project_path: Path) -> "SetupState | None":
        path = paths.state_path(project_path) / STATE_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            data["stage"] = Stage(data.get("stage", Stage.CHOOSE.value))
        except (ValueError, KeyError, json.JSONDecodeError):
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def clear(cls, project_path: Path) -> None:
        (paths.state_path(project_path) / STATE_FILENAME).unlink(missing_ok=True)


def source_tree_hash(pack_dir: Path) -> str:
    """Content hash of a team source tree — validation freshness check."""
    h = hashlib.sha256()
    if not pack_dir.is_dir():
        return ""
    for f in sorted(pack_dir.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            h.update(f.relative_to(pack_dir).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()
