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
      | sk-ant-[A-Za-z0-9_-]{20,}           # anthropic (before generic sk-)
      | sk-[A-Za-z0-9_-]{20,}               # openai-style
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
    r"|auth[_-]?token|client[_-]?secret|bot[_-]?token)\b(\s*[:=]\s*)(\S+)"
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

