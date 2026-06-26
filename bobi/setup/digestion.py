"""The digestion brain — one stateful prompt, run stateless per turn.

This is the through-line intelligence (DESIGN.md "the magic is in the
digestion prompt"). Each conversation turn:

  1. the user's message is appended to the transcript;
  2. a context is assembled — the spec so far + rolling summary + the last
     N raw messages — and sent as a single stateless streaming call;
  3. the model streams a warm, Bob-voiced **reply** (relayed to the UI
     token-by-token), then a sentinel, then a JSON **payload** that routes
     the turn into the four spec slots, refreshes the rolling summary, and
     self-scores each slot's readiness;
  4. the payload is applied to `SetupState` (authoritative) and persisted.

The model owns routing + content; the wizard owns structure. Readiness is
soft — it tunes bobi's next follow-up and a calm UI cue, never a gate.

The context assembler is one tunable function (`assemble_context`); the
output contract is one tunable prompt (`DIGESTION_SYSTEM_PROMPT`). You
iterate these, not the screens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from bobi.setup import llm
from bobi.setup.state import SPEC_SLOTS, Readiness, SetupState

# Separates the streamed conversational reply from the trailing JSON payload.
# The server relays everything before it to the UI and buffers the rest.
SPEC_SENTINEL = "===BOBI-SPEC==="

_READINESS_VALUES = {r.value for r in Readiness}


DIGESTION_SYSTEM_PROMPT = f"""\
You are bobi, helping a developer design an autonomous agent **team** by
talking with them. bobi's voice: dry, witty, geeky, warm, and reassuring —
never corporate, never gushing. Plain language, not tech-industry lingo:
never say "ship it", "let's ship", "lock(ed) in", "lock it in", "dial it in",
"supercharge", "leverage", "10x", "let's gooo", or similar startup clichés.
Keep replies short (1–4 sentences). Reflect back a smart, *alive*
understanding of what they want, and ask at most ONE good follow-up that
moves the design forward. Rough input is fine — the team is editable later;
never make the user feel they must get it perfect now.

# How you interview — methodical, one agent at a time

You run a guided interview, not a free-for-all. Work in phases, and tell the
user when you move from one to the next so they always know where you are:

1. **Goal** — first settle one sentence: what the team does and the outcome.
2. **Roles, ONE AT A TIME** — once the goal is clear, work out the roster of
   roles, then interview them **one role at a time, in order**. Finish a role
   before starting the next, and say so out loud — e.g. "Got the Triage Lead.
   That's role 1 of 3 — let's do the Engineer next." For EACH role, get clarity
   on all four of these before moving on:
     - **responsibility** — what this role actually does day to day.
     - **good_looks_like** — what a good job looks like for this role (the bar
       for success).
     - **systems** — which outside systems/services it needs to access to do
       its job (list them).
     - **triggers** — what makes this role act (a schedule, an event, a human
       request).
   A small team is fine — often one role. Don't fabricate roles the user
   hasn't implied.
3. **Automations** — proactive things the team does on its own, unprompted.
4. **Connections** — the outside services the whole team reads from / writes to.
   Once you and the user agree on which services the team needs, **direct them to
   the Connections card in the right-hand panel to actually connect them** —
   that's where OAuth and API keys are entered (NEVER in this chat; if a user
   pastes a key here, tell them it belongs in the panel). Say something like
   "I've added those to the Connections card on the right — go connect each one
   there, then come back and we'll sort out how you chat with the team." Then
   **wait** — do not move on to chat until every connection shows connected (or
   the user confirms none are needed).
5. **Chat** — how they'll talk to the team day to day. Bring this up only once
   connections are settled, framing it as the last step.

Give the user a sense of progress as you go (which phase, how many roles left).
Ask ONE good question at a time. Never dump a giant questionnaire.

# The slots you are filling

- **goal**: one sentence — what the team does and the outcome it produces.
- **roles**: the roles, each with the four dimensions above. Mark a role
  "complete" only once all four dimensions are genuinely settled; otherwise
  "in_progress".
- **autonomous**: things the team should do on its own, unprompted (proactive
  checks / scheduled or triggered behavior). Each carries a leash: "notify"
  (tell the user), "ask" (propose, wait for approval), or "act" (do it,
  report); the role that runs it; and what the agent is told to do (command).
  An empty list is a valid, deliberate answer — but only once the user has
  weighed in (set autonomous_confirmed).
- **services**: the outside services the team reads from or writes to (e.g.
  github, slack, email, a CRM). Name each one.
- **chat**: how they'll talk to the team — "cli" (the command line, nothing to
  set up — the sensible default), "slack" (message the bot in a channel), or
  "telegram" (coming soon). Bring this up once the team's shape is clear.

Route what the user says into these slots. When a slot changes, emit its
**full new value** (not a diff) — for roles and autonomous, always return the
COMPLETE list with every item, updated. Don't invent services or roles the
user hasn't implied. Keep the user's own framing.

After your conversational reply, output a line containing exactly
{SPEC_SENTINEL} and then a single JSON object — nothing after it. Shape:

{{
  "deltas": {{
    "name": "short-kebab-team-name (a 2-4 word slug for the team, drawn from the goal — only include the FIRST time the goal becomes clear, so the team gets a name automatically)",
    "goal": "string (only if it changed)",
    "roles": [{{"name": "...", "responsibility": "...",
                "good_looks_like": "...", "systems": ["..."],
                "triggers": "...", "status": "in_progress|complete"}}],
    "autonomous": [{{"description": "...", "leash": "notify|ask|act",
                     "cadence": "e.g. 1d, 15m, or an event",
                     "role": "which role runs it", "command": "what it's told to do"}}],
    "services": [{{"name": "..."}}],
    "chat": "cli|slack|telegram (only if they indicated how they'll talk to it)"
  }},
  "autonomous_confirmed": true,
  "phase": "goal | role:<role-name> | automations | connections | wrap",
  "summary": "refreshed 2–4 sentence running summary of the whole design",
  "readiness": {{"goal": "empty|thin|enough", "roles": "...",
                 "autonomous": "...", "services": "..."}}
}}

Only include keys in "deltas" for slots that changed this turn. Always include
"phase", "summary", and "readiness". "phase" is where the interview currently
is (use "role:<name>" while interviewing a specific role). Score a slot
"enough" only once genuinely settled — goal: one clear sentence with an
outcome; roles: at least one role AND every role marked "complete" (all four
dimensions filled); autonomous: explicitly confirmed (an empty list counts);
services: each implied service named, OR the user confirmed none are needed.
The reply and the JSON are both required, in that order.
"""


@dataclass
class DigestionResult:
    reply: str
    deltas: dict = field(default_factory=dict)
    summary: str = ""
    readiness: dict = field(default_factory=dict)
    autonomous_confirmed: bool | None = None
    phase: str = ""


# --- context assembly (tunable) ------------------------------------------

def assemble_context(state: SetupState, last_n: int = 12) -> str:
    """Build the single user prompt for one digestion turn: the spec so
    far + rolling summary + the last N raw messages."""
    spec = state.spec
    snapshot = {
        "goal": spec.goal,
        "roles": spec.roles,
        "autonomous": spec.autonomous,
        "autonomous_confirmed": spec.autonomous_confirmed,
        "services": spec.services,
        "readiness": {s: spec.readiness_for(s).value for s in SPEC_SLOTS},
        "phase": state.phase,
    }
    parts = ["SPEC SO FAR:", json.dumps(snapshot, indent=2)]
    if state.summary:
        parts += ["", "SUMMARY SO FAR:", state.summary]
    parts += ["", "RECENT MESSAGES:"]
    for m in state.messages[-last_n:]:
        parts.append(f"{m.get('role', 'user')}: {m.get('content', '')}")
    parts += ["", "Reply to the latest user message, then emit the spec block."]
    return "\n".join(parts)


# --- output parsing ------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    """Parse the first JSON object in `text`, tolerating trailing prose."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_digestion(full_text: str) -> DigestionResult:
    """Split a completed digestion response into reply + structured payload.

    Degrades gracefully: with no sentinel or bad JSON, the whole text is the
    reply and nothing is routed (the conversation never crashes on a
    malformed turn)."""
    idx = full_text.find(SPEC_SENTINEL)
    if idx == -1:
        return DigestionResult(reply=full_text.strip())
    reply = full_text[:idx].strip()
    payload = _extract_json(full_text[idx + len(SPEC_SENTINEL):])
    if not payload:
        return DigestionResult(reply=reply)
    phase = payload.get("phase")
    return DigestionResult(
        reply=reply,
        deltas=payload.get("deltas") or {},
        summary=payload.get("summary") or "",
        readiness=payload.get("readiness") or {},
        autonomous_confirmed=payload.get("autonomous_confirmed"),
        phase=str(phase) if isinstance(phase, str) else "",
    )


def apply_deltas(state: SetupState, result: DigestionResult) -> None:
    """Route a parsed digestion result into the authoritative spec. Each
    provided slot carries its full new value (replace, not merge)."""
    from bobi.setup.authoring import slug
    spec = state.spec
    d = result.deltas or {}
    # Auto-name a brand-new team from the goal the moment one is proposed.
    # Set once (when still unnamed) so the name is stable; the panel header is
    # click-to-edit for an explicit rename. Never auto-rename an existing
    # (open/modify) team — its name comes from the pack.
    if (state.mode == "create" and not state.team_name
            and isinstance(d.get("name"), str) and d["name"].strip()):
        state.team_name = slug(d["name"])
    if isinstance(d.get("goal"), str):
        spec.goal = d["goal"].strip()
    if isinstance(d.get("roles"), list):
        spec.roles = d["roles"]
    if isinstance(d.get("services"), list):
        spec.services = d["services"]
    if isinstance(d.get("autonomous"), list):
        spec.autonomous = d["autonomous"]
    if d.get("chat") in ("cli", "slack", "telegram"):
        state.chat = d["chat"]
    if result.autonomous_confirmed is not None:
        spec.autonomous_confirmed = bool(result.autonomous_confirmed)
    for slot, value in (result.readiness or {}).items():
        if slot in SPEC_SLOTS and value in _READINESS_VALUES:
            spec.readiness[slot] = value
    if result.summary:
        state.summary = result.summary
    if result.phase:
        state.phase = result.phase


# --- streaming reply splitter --------------------------------------------

class _ReplySplitter:
    """Emits the pre-sentinel reply incrementally as chunks arrive, holding
    back a short tail so a sentinel split across chunks is never leaked."""

    def __init__(self, sentinel: str) -> None:
        self.sentinel = sentinel
        self.text = ""
        self._emitted = 0
        self._cut: int | None = None

    def feed(self, chunk: str) -> str:
        self.text += chunk
        if self._cut is None:
            i = self.text.find(self.sentinel)
            if i != -1:
                self._cut = i
        if self._cut is not None:
            safe_end = self._cut
        else:
            hold = len(self.sentinel) - 1
            safe_end = max(0, len(self.text) - hold)
        if safe_end > self._emitted:
            out = self.text[self._emitted:safe_end]
            self._emitted = safe_end
            return out
        return ""

    def flush(self) -> str:
        end = self._cut if self._cut is not None else len(self.text)
        if end > self._emitted:
            out = self.text[self._emitted:end]
            self._emitted = end
            return out
        return ""


# --- the turn ------------------------------------------------------------

async def digest_turn(state: SetupState, project, user_message: str, *,
                      model: str | None = None, cwd: str | None = None,
                      stream_fn=None):
    """Run one digestion turn. Async-generator: yields reply text chunks for
    the UI, and as a side effect routes the payload into `state` and
    checkpoints it. Consume to completion before re-reading `state`.

    Defense-in-depth: any secret-shaped substring the user pastes is redacted
    here before it reaches the LLM, the rolling summary, or the persisted
    transcript — credentials belong in Connect, never the conversation."""
    from bobi.setup.actions import redact_secrets
    user_message, _ = redact_secrets(user_message)
    state.messages.append({"role": "user", "content": user_message})
    splitter = _ReplySplitter(SPEC_SENTINEL)
    async for chunk in llm.stream(DIGESTION_SYSTEM_PROMPT,
                                  assemble_context(state),
                                  model=model, cwd=cwd, stream_fn=stream_fn):
        out = splitter.feed(chunk)
        if out:
            yield out
    tail = splitter.flush()
    if tail:
        yield tail

    result = parse_digestion(splitter.text)
    apply_deltas(state, result)
    state.messages.append({"role": "assistant",
                           "content": result.reply or splitter.text.strip()})
    state.save(project)
