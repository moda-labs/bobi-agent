"""Shared secret redaction for setup and opt-in policy egress."""

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
# which keeps the scan linear on long `a_a_a...` runs. The key name alone is
# public so a credential setting whose value is a key name
# (`API_KEY_HEADER=X-API-Key`) can be scrubbed after this rule has seen it.
_SECRET_KEY = r"""(?<![A-Za-z0-9_])(
        (?i:(?:--)?_*(?:[A-Za-z0-9]+_)*(?:(?:pg|mysql)?password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?token
          |auth[_-]?token|refresh[_-]?token|client[_-]?secret|bot[_-]?token
          |secret[_-]?key|private[_-]?key))
      | _*(?:[A-Z0-9]+_)+(?:TOKEN|KEY|PASS)
    )(?![A-Za-z0-9])"""
SECRET_KEY_NAME = re.compile(_SECRET_KEY, re.VERBOSE)
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
        quote = m.group("quote") or m.group("short") or ""
        value, rest = (m.group("long") or m.group("quoted"), "") if quote else (m.group("bare"), "")
        if not quote and (m.group("bare") or "").startswith(REDACTION_PLACEHOLDER):
            return m.group(0)
        if not quote and m.group(2)[:1] in "\"'":
            # After a quoted key the value ends at the JSON/dict delimiter.
            end = re.search(r"[,}\]](?=[,}\]\"']|$)", value)
            if end:
                value, rest = value[:end.start()], value[end.start():]
            if value in _JSON_LITERAL:
                return m.group(0)
        if not value or value == REDACTION_PLACEHOLDER:
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}{quote}{REDACTION_PLACEHOLDER}{quote}{rest}"

    text = _SECRET_KV.sub(_kv, text)

    def _userinfo(m):
        nonlocal count
        count += 1
        return f"{m.group(1)}{REDACTION_PLACEHOLDER}@"

    text = _URL_USERINFO.sub(_userinfo, text)
    return text, count


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
