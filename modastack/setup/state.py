"""Setup state machine: the 8-stage create spine, gating, and persistence.

The web wizard drives navigation; this module is the source of truth for
*where* setup is and *what it knows so far*. The conversation routes the
user's messages into a four-slot accumulating `Spec`
(goal / roles / autonomous / services) — `SetupState` is authoritative,
the LLM owns routing and content, the wizard owns structure (it computes
the file manifest at Build).

Readiness is **soft**: each slot self-scores empty/thin/enough to guide
modastack's follow-up and a calm UI cue, but it never gates. The only hard
floors are structural — goal must be non-empty to author anything at
Build, a fresh validation to Install, an install to finish.

State is checkpointed to .modastack/state/setup.json after every change
so an interrupted setup resumes with `modastack setup --resume`.

v1 is the **create** spine. Open mode (editing an existing pack) reuses
these same stages and is deferred to M2 — `mode` carries the seam.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from modastack import paths

STATE_FILENAME = "setup.json"


class Stage(str, Enum):
    START = "start"
    DESIGN = "design"
    AUTOMATE = "automate"
    CONNECT = "connect"        # what the team can reach (services it operates on)
    CHAT = "chat"              # how you reach the team (slack / telegram / cli)
    BUILD = "build"
    REVIEW = "review"
    INSTALL = "install"
    DONE = "done"


STAGE_ORDER = list(Stage)


class Readiness(str, Enum):
    EMPTY = "empty"   # nothing said yet
    THIN = "thin"     # touched, but under-specified
    ENOUGH = "enough"  # the brain judged this slot well-formed


# The four accumulating spec slots, by canonical key. The conversation
# routes each turn into one or more of these; Build authors files from them.
SPEC_SLOTS = ("goal", "roles", "autonomous", "services")


@dataclass
class Spec:
    """The accumulating, route-then-author spec. Lists hold plain dicts
    (not nested dataclasses) so the digestion delta format can evolve
    without reshaping persistence; the wizard reads them at Build."""

    goal: str = ""                              # one sentence: what it does + outcome
    # Each role carries the four interview dimensions plus a completeness
    # self-score: {"name", "responsibility" (what it does), "good_looks_like",
    # "systems" (list of systems it accesses), "triggers" (what makes it run),
    # "status": "in_progress"|"complete"}.
    roles: list = field(default_factory=list)
    # [{"description","leash","cadence","role" (which agent runs it),"command"}]
    autonomous: list = field(default_factory=list)
    services: list = field(default_factory=list)    # [{"name","status"}]
    # User-defined custom MCP connections added by name + remote URL (the
    # Claude-style "add a connector" form). name -> {"url", "type", "auth"
    # ("none"|"api_key"|"oauth"), and the env-var names holding any secrets:
    # "secret_var" / "client_id_var" / "client_secret_var"}. Authored verbatim
    # into agent.yaml mcp_servers: and shown as their own rows.
    mcp_servers: dict = field(default_factory=dict)

    # Autonomous is "enough" only once explicitly confirmed — even when the
    # answer is "nothing proactive" (an empty list is a real decision here).
    autonomous_confirmed: bool = False

    # Brain-emitted self-scores per slot (slot -> Readiness value). Absent
    # until the digestion prompt scores it; readiness_for() falls back to a
    # structural guess in the meantime.
    readiness: dict = field(default_factory=dict)

    def readiness_for(self, slot: str) -> Readiness:
        """The slot's readiness — the brain's score if it has one, else a
        structural fallback from whether the slot holds anything."""
        if slot not in SPEC_SLOTS:
            raise ValueError(f"unknown spec slot '{slot}'")
        stored = self.readiness.get(slot)
        if stored:
            return Readiness(stored)
        if slot == "autonomous":
            return Readiness.THIN if self.autonomous_confirmed else Readiness.EMPTY
        value = getattr(self, slot)
        has = bool(value.strip()) if isinstance(value, str) else bool(value)
        return Readiness.THIN if has else Readiness.EMPTY


@dataclass
class SetupState:
    stage: Stage = Stage.START
    mode: str = "create"             # "create" | "open"
    team_name: str = ""
    # Where the team source lives — a real, owned folder (e.g.
    # agent-teams/<name>/), authored/edited here and installed into .modastack/
    # at Finish. Empty falls back to agents/<team_name> (legacy/tests).
    source_dir: str = ""
    chat: str = ""                   # how you talk to the team: "cli"|"slack"|"telegram"
    spec: Spec = field(default_factory=Spec)

    # The brain's current interview focus, so the panel can show where we are:
    # "goal" | "role:<slug>" | "automations" | "connections" | "wrap" (or "").
    phase: str = ""

    # Stateless-one-shot brain memory: a rolling summary refreshed each
    # digestion turn, plus the raw transcript (the context assembler takes
    # the last N for the next call).
    summary: str = ""
    messages: list = field(default_factory=list)

    credentials_saved: list = field(default_factory=list)
    validated: bool = False
    validated_hash: str = ""         # source tree hash at validation time
    installed: bool = False
    finished: bool = False
    session_id: str = ""

    # --- gating ---------------------------------------------------------

    def require_stage(self, *stages: Stage) -> str | None:
        """None when the current stage is one of `stages`, else a refusal."""
        if self.stage in stages:
            return None
        allowed = " or ".join(f"'{s.value}'" for s in stages)
        return (
            f"this action is only available in stage {allowed}; "
            f"setup is currently in '{self.stage.value}'"
        )

    def _hard_floor(self, to: Stage) -> str | None:
        """Structural gates — the only things that actually block. Readiness
        is soft and never appears here."""
        if to == Stage.BUILD and not self.spec.goal.strip():
            return "tell modastack what the team should do — the goal is still empty"
        if to == Stage.INSTALL and not self.validated:
            return "the team source hasn't passed validation yet"
        if to == Stage.DONE and not self.installed:
            return "the team isn't installed yet"
        return None

    def can_advance(self, to: Stage) -> str | None:
        """None when moving to `to` is legal, else the refusal reason.

        Backward moves are always allowed (the wizard is a re-entrant
        editor). Forward moves require every hard floor between here and
        the target to be clear; soft readiness never blocks.
        """
        if to == self.stage:
            return f"already in stage '{to.value}'"
        cur_i, to_i = STAGE_ORDER.index(self.stage), STAGE_ORDER.index(to)
        if to_i < cur_i:
            return None
        for s in STAGE_ORDER[cur_i + 1:to_i + 1]:
            blocker = self._hard_floor(s)
            if blocker:
                return blocker
        return None

    def advance_blocker(self) -> str | None:
        """Why the 'Next' affordance to the following stage is blocked, or
        None. Serialized for the UI so it can explain a gated step."""
        i = STAGE_ORDER.index(self.stage)
        if i + 1 >= len(STAGE_ORDER):
            return None
        return self.can_advance(STAGE_ORDER[i + 1])

    # --- persistence ----------------------------------------------------

    def save(self, project_path: Path) -> None:
        data = asdict(self)            # recurses into Spec → dict
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
            data["stage"] = Stage(data.get("stage", Stage.START.value))
        except (ValueError, KeyError, json.JSONDecodeError):
            return None
        raw_spec = data.get("spec")
        if isinstance(raw_spec, dict):
            spec_fields = set(Spec.__dataclass_fields__)
            data["spec"] = Spec(**{k: v for k, v in raw_spec.items()
                                   if k in spec_fields})
        known = set(cls.__dataclass_fields__)
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
