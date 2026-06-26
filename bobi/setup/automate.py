"""The Automate suggester — a dedicated generative one-shot.

Distinct from the digestion brain: this doesn't route conversation, it
*ideates*. Given the team's intent (the spec), it proposes concrete,
genuinely-useful, non-spammy proactive behaviors — each with a sane cadence
and a leash (notify / ask / act) — so the user gets the "it thought of
things I didn't" beat. The user toggles, edits, adds, or skips; nothing is
applied until they commit (Automate is an opt-in trust decision).

One stateless call, JSON out. The source is injectable for hermetic tests.
"""

from __future__ import annotations

import json

from bobi.setup import llm
from bobi.setup.state import SetupState

_LEASHES = {"notify", "ask", "act"}


AUTOMATE_SYSTEM_PROMPT = """\
You design proactive behaviors for an autonomous agent team. Given the
team's goal, roles, and connected services, propose 2–4 things the team
could usefully do ON ITS OWN, unprompted — scheduled checks, periodic
digests, watches on an outside source, nudges. Each must be:
- genuinely useful given THIS team's goal (no filler, no spam),
- concrete and specific (name what it checks and what it does),
- given a sane cadence (an interval like "1d"/"15m" or an event), and
- given a leash: "notify" (tell the user, they act), "ask" (propose, wait
  for approval), or "act" (do it, report). Pick the leash that matches the
  risk — destructive or high-stakes actions get "ask" or "notify".

Output ONLY a JSON array (no prose, no code fence):
[{"description": "...", "leash": "notify|ask|act", "cadence": "...",
  "rationale": "one line on why it's worth doing"}]
If nothing is genuinely worth doing unprompted, output [].
"""


def _spec_brief(state: SetupState) -> str:
    spec = state.spec
    roles = "; ".join(
        f"{r.get('name')}: {r.get('responsibility', '')}"
        for r in spec.roles if isinstance(r, dict)) or "(none specified)"
    svcs = ", ".join(s.get("name") if isinstance(s, dict) else str(s)
                     for s in spec.services) or "(none)"
    return (f"Goal: {spec.goal}\nRoles: {roles}\nServices: {svcs}\n\n"
            "Propose the proactive behaviors.")


def _parse_suggestions(text: str) -> list[dict]:
    """Pull the first JSON array out of the model's output, tolerantly, and
    normalize each suggestion. Bad output → no suggestions (never raises)."""
    start = text.find("[")
    if start == -1:
        return []
    try:
        arr, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        desc = (item.get("description") or "").strip()
        if not desc:
            continue
        leash = item.get("leash") if item.get("leash") in _LEASHES else "notify"
        out.append({
            "description": desc,
            "leash": leash,
            "cadence": (item.get("cadence") or "").strip(),
            "rationale": (item.get("rationale") or "").strip(),
        })
    return out


async def suggest(state: SetupState, *, model: str | None = None,
                  cwd: str | None = None, stream_fn=None) -> list[dict]:
    """Run the suggester once and return a list of proposed behaviors."""
    text = await llm.complete(AUTOMATE_SYSTEM_PROMPT, _spec_brief(state),
                              model=model, cwd=cwd, stream_fn=stream_fn)
    return _parse_suggestions(text)
