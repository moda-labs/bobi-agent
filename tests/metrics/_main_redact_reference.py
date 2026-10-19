"""Reference redaction: the merge base's ``bobi.setup.actions.redact_secrets``.

Everything between the two ``---- verbatim`` markers is copied unchanged from
``bobi/setup/actions.py`` at the merge base with main (``git show
b9bc83bc:bobi/setup/actions.py``, the redaction block). It is the floor the
shared redactor must never fall below. ``removed_spans`` re-runs the same two
regex passes while tracking which characters of the input they replace, and
the tests check it reproduces the verbatim output exactly.
"""

from bisect import bisect_right
import re

# ---- verbatim from the merge base
# Full-token secret patterns for REDACTING freeform chat input before it
# reaches the LLM, the rolling summary, or the persisted transcript.
# (SECRET_SHAPES above is prefix-only, for scanning generated files; this is
# whole-token so we can strip the value, not just flag a prefix.) Credentials
# belong in Connect (→ run/.env), never in the conversation — so this errs
# toward redaction; a redacted long hash in a design chat costs nothing.
REDACTION_PLACEHOLDER = "[redacted]"

_SECRET_TOKEN = re.compile(
    r"""(
        -----BEGIN[A-Z ]*PRIVATE\ KEY-----.*?-----END[A-Z ]*PRIVATE\ KEY-----
      | xox[abprs]-[A-Za-z0-9-]{10,}        # slack bot/user tokens
      | xapp-[A-Za-z0-9-]{10,}              # slack app-level tokens
      | lin_api_[A-Za-z0-9]{10,}            # linear
      | ghp_[A-Za-z0-9]{20,}                # github PAT
      | github_pat_[A-Za-z0-9_]{20,}
      | sk-ant-[A-Za-z0-9_-]{20,}           # anthropic (before generic sk-)
      | sk-[A-Za-z0-9]{20,}                 # openai-style
      | venn_[A-Za-z0-9]{8,}                # venn
      | AKIA[0-9A-Z]{16}                    # aws access key id
      | AIza[0-9A-Za-z_-]{20,}              # google api key
      | eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+  # jwt
      | (?i:bearer)\s+[A-Za-z0-9._-]{8,}    # bearer token (space form)
      | [A-Za-z0-9_-]{40,}                  # generic long opaque token
    )""",
    re.VERBOSE | re.DOTALL,
)

# `password: hunter2`, `API_KEY=shortish` — redact the value, which shape
# detection alone (short secrets) would miss. Requires a ':' or '=' so prose
# like "keep the credential safe" is left alone.
_SECRET_KV = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?token"
    r"|auth[_-]?token|refresh[_-]?token|client[_-]?secret|bot[_-]?token)"
    r"\b(\s*[:=]\s*)(\S+)"
)


def redact_secrets(text: str) -> tuple[str, int]:
    """Strip secret-shaped substrings from `text`, returning
    (redacted_text, count). Idempotent — re-running on redacted text is a
    no-op (the placeholder has no secret shape)."""
    count = 0

    def _tok(_m):
        nonlocal count
        count += 1
        return REDACTION_PLACEHOLDER

    text = _SECRET_TOKEN.sub(_tok, text)

    def _kv(m):
        nonlocal count
        if m.group(3) == REDACTION_PLACEHOLDER:
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}{REDACTION_PLACEHOLDER}"

    text = _SECRET_KV.sub(_kv, text)
    return text, count

# ---- end verbatim


def _replace(text: str, spans):
    """*text* with each span (sorted, disjoint) replaced by the placeholder,
    and a function mapping an output span to the input span it stands for."""
    parts: list[str] = []
    # Per output segment: its output start, input start, and whether it is a
    # placeholder (which stands for its whole input span).
    segments: list[tuple[int, int, int, bool]] = []
    position = written = 0
    for start, end in spans:
        if start > position:
            segments.append((written, position, start, False))
            parts.append(text[position:start])
            written += start - position
        segments.append((written, start, end, True))
        parts.append(REDACTION_PLACEHOLDER)
        written += len(REDACTION_PLACEHOLDER)
        position = end
    segments.append((written, position, len(text), False))
    parts.append(text[position:])
    offsets = [segment[0] for segment in segments]

    def to_input(a: int, b: int) -> tuple[int, int]:
        first = segments[bisect_right(offsets, a) - 1]
        last = segments[bisect_right(offsets, b - 1) - 1]
        lo = first[1] if first[3] else first[1] + a - first[0]
        hi = last[2] if last[3] else last[1] + b - last[0]
        return lo, hi

    return "".join(parts), to_input


def removed_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """The merge base's output for *text* and the input spans it replaced."""
    token_spans = [m.span() for m in _SECRET_TOKEN.finditer(text)]
    after_tokens, to_input = _replace(text, token_spans)
    kv_spans = [m.span(3) for m in _SECRET_KV.finditer(after_tokens)
                if m.group(3) != REDACTION_PLACEHOLDER]
    output, _ = _replace(after_tokens, kv_spans)
    return output, token_spans + [to_input(a, b) for a, b in kv_spans]


def occurrence_spans(text: str, values) -> list[tuple[int, int]]:
    """Every occurrence of every value in *text*, overlapping ones joined."""
    found = []
    for value in values:
        start = text.find(value)
        while start != -1:
            found.append((start, start + len(value)))
            start = text.find(value, start + 1)
    return merge_spans(found)


def merge_spans(spans) -> list[tuple[int, int]]:
    """*spans* sorted, overlapping ones joined (touching ones kept apart)."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def scrub_then_removed_spans(text: str, values, removed=removed_spans) -> list[tuple[int, int]]:
    """The input spans that scrubbing every occurrence of *values* and then
    running a redaction (*removed*, the merge base's by default) replaces."""
    occurrences = occurrence_spans(text, values)
    scrubbed, to_input = _replace(text, occurrences)
    return occurrences + [to_input(a, b) for a, b in removed(scrubbed)[1]]
