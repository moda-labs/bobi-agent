"""Shared classification of transient API errors.

One definition of "what counts as transient" for every code path that has to
decide whether an API error is worth retrying (the persistent manager session's
turn loop, ``session.py``) or merely worth recording honestly (the sub-agent
spawn/executor path, ``subagent.py``). Before this module each path carried its
own copy of the status set and the sniff heuristic; they could drift. #444
introduced the canonical set as a ``Session`` method coupled to instance state —
this lifts the pure parts here so the spawn/workflow path can reuse them without
importing the persistent-session class.
"""

from __future__ import annotations

# In-band retry budget for transient turn-level API errors (e.g. 529 Overloaded,
# rate limits). A transient error is scoped to a single turn — the SDK client
# stays connected — so the persistent session re-issues the same query with
# capped exponential backoff before giving up. Retries are bounded so a genuinely
# failing turn surfaces its error to the caller instead of looping forever. The
# spawn/executor path does NOT retry (transient survival/retry is owned by the
# persistent session — #444); it consults only the classifier below.
TURN_RETRY_BASE = 2.0
TURN_RETRY_MAX_ATTEMPTS = 2

# HTTP statuses worth retrying: overload, rate limit, gateway/timeout 5xx.
# Anything else (4xx like 400/401/403) is a real error — recover but don't retry.
TRANSIENT_API_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

# Text fingerprints used only when no concrete status was surfaced by the SDK.
_TRANSIENT_TEXT = (
    "overloaded", "rate limit", "rate_limit", "529",
    "503", "502", "504", "timed out", "timeout",
)


def is_transient_api_error(status: int | None, text: str = "") -> bool:
    """Whether an API error is transient (worth retrying / not a real failure).

    Status-first: a known transient status is transient, and a concrete
    non-transient status (e.g. 400) is *not* — we trust the status over the
    text. Only when no status was surfaced do we fall back to sniffing the
    response text for overload/rate-limit/timeout phrasing. Pure — holds no
    session state — so any path can call it.
    """
    if status in TRANSIENT_API_STATUSES:
        return True
    if status is not None:
        return False  # a concrete non-transient status (e.g. 400) — don't retry
    t = (text or "").lower()
    return any(s in t for s in _TRANSIENT_TEXT)
