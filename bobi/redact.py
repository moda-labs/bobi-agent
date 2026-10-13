"""Shared secret redaction for setup and opt-in policy egress."""

from array import array
from bisect import bisect_right
import re

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
      | (?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}  # anthropic (before generic sk-)
      | (?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}      # openai-style, not "task-..."
      | venn_[A-Za-z0-9]{8,}                # venn
      | AKIA[0-9A-Z]{16}                    # aws access key id
      | AIza[0-9A-Za-z_-]{20,}              # google api key
      | eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+  # jwt
      | (?i:bearer)\s+[A-Za-z0-9._-]{8,}    # bearer token (space form)
      | (?i:authorization:\s*basic)\s+[A-Za-z0-9+/=]{8,}  # basic auth header
      | [A-Za-z0-9_-]{40,}                  # generic long opaque token
    )""",
    re.VERBOSE | re.DOTALL,
)

# `password: hunter2`, `DB_PASSWORD=x`, `{"password": "x"}`, `GH_TOKEN=x`,
# `SECRET_KEY=x`, `DB_PASS=x` — redact the value, which shape detection alone
# (short secrets) would miss. Requires a ':' or '=' so prose like "keep the
# credential safe" is left alone; bare `TOKEN`, `KEY` and `PASS` count only as
# the upper-case tail of an env-style name (`GH_TOKEN`, `DB_PASS`), so code
# like `primary_key=True` or `next_page_token: str` and prose like "key: 5" stay.
# The left boundary excludes `_` so a match starts only at the start of a name,
# which keeps the scan linear on long `a_a_a...` runs.
_SECRET_KEY = r"""(?<![A-Za-z0-9_])(
        (?i:(?:--)?_*(?:[A-Za-z0-9]+_)*(?:(?:pg|mysql)?password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?token
          |auth[_-]?token|refresh[_-]?token|client[_-]?secret|bot[_-]?token
          |secret[_-]?key|private[_-]?key))
      | _*(?:[A-Z0-9]+_)+(?:TOKEN|KEY|PASS)
    )(?![A-Za-z0-9])"""
# A quoted value may hold an escaped (`\'`) or SQL-doubled (`''`) quote and
# ends at the line; a triple-quoted one may span lines.
_SECRET_KV = re.compile(
    _SECRET_KEY + r"""
    (?i:(["']?\s*[:=]\s*|(?<=--password)\x20+(?=[^\s-])))
    (?:(?P<quote>"{3}|'{3})(?P<long>(?:[^"'\\]|\\.|(?!(?P=quote))["'])*)(?P=quote)
      | (?P<short>["'])(?P<quoted>(?:[^"'\\\n]|\\.|(?P=short){2}|(?!(?P=short))["'])*)(?P=short)
      | (?P<bare>\S+))""",
    re.VERBOSE,
)
# A bare JSON literal after a quoted key (`{"password": null}`) is structure,
# not a credential.
_JSON_LITERAL = frozenset({"null", "true", "false"})

# `scheme://user:password@host` — the userinfo of a connection string. The
# bounded scheme and its left boundary keep the scan linear on dotted text.
_URL_USERINFO = re.compile(r"(?i)(?<![a-z0-9+.\-])([a-z][a-z0-9+.-]{0,31}://)[^/\s:@]+:[^/\s@]+@")

# Env-name segments that mark a credential, for scrubbing its exact value.
_SECRET_ENV_SEGMENTS = frozenset({
    "KEY", "APIKEY", "TOKEN", "SECRET", "PASS", "PASSWORD", "PASSWD", "PWD",
    "CREDENTIAL", "CREDENTIALS", "AUTH", "COOKIE", "PRIVATE",
})
# Process bookkeeping whose names look like credentials but whose values do not.
_NOT_SECRET_ENV = frozenset({"PWD", "OLDPWD", "XAUTHORITY"})
_ORDINARY_VALUE = re.compile(r"(?i)(?:true|false|yes|no|on|off|none|null|/.*|~/.*|[a-z]:[\\/].*)")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
# A numeric value is a credential only under a password-like name (a PIN), not
# under `AUTH_TIMEOUT` or `COOKIE_MAX_AGE`.
_NUMERIC_SECRET_SEGMENTS = frozenset({"PASS", "PASSWORD", "PASSWD", "PWD", "SECRET", "PIN"})


def _kv_value_span(m: re.Match[str]) -> tuple[int, int] | None:
    """The span of the credential value in a key/value match, or None when the
    match is structure (a JSON literal, an empty or already-redacted value)."""
    if m.group("quote"):
        start, end = m.span("long")
    elif m.group("short"):
        start, end = m.span("quoted")
    else:
        start, end = m.span("bare")
        if m.group("bare").startswith(REDACTION_PLACEHOLDER):
            return None
        if m.group(2)[:1] in "\"'":
            # After a quoted key the value ends at the JSON/dict delimiter.
            delimiter = re.search(r"[,}\]](?=[,}\]\"']|$)", m.group("bare"))
            if delimiter:
                end = start + delimiter.start()
            if m.string[start:end] in _JSON_LITERAL:
                return None
    value = m.string[start:end]
    if not value or value == REDACTION_PLACEHOLDER:
        return None
    return start, end


def _userinfo_span(m: re.Match[str]) -> tuple[int, int]:
    return m.end(1), m.end() - 1


# Applied in order, each to the text the earlier ones produced.
_RULES = (
    (_SECRET_TOKEN, lambda m: m.span()),
    (_SECRET_KV, _kv_value_span),
    (_URL_USERINFO, _userinfo_span),
)


def secret_spans(text: str) -> tuple[list[tuple[int, int]], int]:
    """The spans of *text* that :func:`redact_secrets` replaces, and how many
    redactions that makes. Each rule runs on the text the earlier rules left;
    its spans are mapped back onto *text*, so a span that covers an earlier
    redaction covers the original characters behind it."""
    spans: list[tuple[int, int]] = []
    count = 0
    for pattern, span_of in _RULES:
        working, to_original = _apply(text, spans)
        found = [span for span in map(span_of, pattern.finditer(working)) if span]
        count += len(found)
        spans += [to_original(a, b) for a, b in found]
    return _merge(spans), count


def _merge(spans) -> list[tuple[int, int]]:
    """Sort *spans* and join the ones that overlap (not the ones that only touch)."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _apply(text: str, spans):
    """*text* with each merged span replaced by the placeholder, and a function
    mapping a span of the result back to the span of *text* it stands for.

    The map keeps one entry per kept or replaced segment, in compact arrays, so
    its size follows the number of redactions, not the length of *text*."""
    parts: list[str] = []
    # Per segment: its start in the result, its original start and end, and
    # whether it is a placeholder (which stands for its whole original span).
    offsets, starts, ends = array("q"), array("q"), array("q")
    placeholders = bytearray()
    position = written = 0

    def segment(start: int, end: int, placeholder: bool) -> None:
        offsets.append(written)
        starts.append(start)
        ends.append(end)
        placeholders.append(placeholder)

    for start, end in _merge(spans):
        if start > position:
            segment(position, start, False)
            parts.append(text[position:start])
            written += start - position
        segment(start, end, True)
        parts.append(REDACTION_PLACEHOLDER)
        written += len(REDACTION_PLACEHOLDER)
        position = end
    segment(position, len(text), False)
    parts.append(text[position:])

    def to_original(a: int, b: int) -> tuple[int, int]:
        first = bisect_right(offsets, a) - 1
        last = bisect_right(offsets, b - 1) - 1
        orig_start = starts[first] if placeholders[first] else starts[first] + a - offsets[first]
        orig_end = ends[last] if placeholders[last] else starts[last] + b - offsets[last]
        return orig_start, orig_end

    return "".join(parts), to_original


def redact_secrets(text: str) -> tuple[str, int]:
    """Strip secret-shaped substrings from `text`, returning
    (redacted_text, count). Idempotent — re-running on redacted text is a
    no-op (the placeholder has no secret shape)."""
    spans, count = secret_spans(text)
    return _apply(text, spans)[0], count


def redact_with_values(text: str, values) -> tuple[str, int]:
    """Redact *text* and every exact occurrence of *values* together.

    The union of three span sets, overlapping spans becoming one placeholder:
    what the shape rules redact in the original text (exactly as without
    *values*), every occurrence of a value, and what the shape rules redact
    once the values are scrubbed (`postgres://[redacted]:x@h` is userinfo).
    Nothing that any of these removes survives."""
    occurrences: list[tuple[int, int]] = []
    for value in {value for value in values if value and value not in REDACTION_PLACEHOLDER}:
        start = text.find(value)
        while start != -1:
            occurrences.append((start, start + len(value)))
            start = text.find(value, start + 1)
    spans, count = secret_spans(text)
    scrubbed, to_original = _apply(text, occurrences)
    after_scrub, _ = secret_spans(scrubbed)
    spans += occurrences + [to_original(a, b) for a, b in after_scrub]
    return _apply(text, spans)[0], count + len(occurrences)


def env_secret_values(env) -> tuple[str, ...]:
    """The values of credential-named variables in *env*, longest first, for
    :func:`scrub_values`.

    A name counts when one of its ``_`` segments marks a credential
    (``DB_PASS``, ``STRIPE_WEBHOOK_SECRET``), never ``PWD`` itself or a
    ``*_SOCK`` path. Short values, paths, numbers and booleans are skipped:
    replacing them would erase ordinary text, not a secret."""
    values = set()
    for name, value in env.items():
        upper = str(name).upper()
        segments = set(upper.split("_"))
        if (upper in _NOT_SECRET_ENV or upper.endswith("_SOCK") or not isinstance(value, str)
                or not _SECRET_ENV_SEGMENTS & segments):
            continue
        if (len(value) < 6 or _ORDINARY_VALUE.fullmatch(value) or value in REDACTION_PLACEHOLDER
                or (_NUMBER.fullmatch(value) and not _NUMERIC_SECRET_SEGMENTS & segments)):
            continue
        values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def scrub_values(text: str, values) -> tuple[str, int]:
    """Replace every exact occurrence of *values* in *text*; returns (text, count)."""
    count = 0
    for value in values:
        if value and value not in REDACTION_PLACEHOLDER and value in text:
            count += text.count(value)
            text = text.replace(value, REDACTION_PLACEHOLDER)
    return text, count
