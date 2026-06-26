"""Harness status — is the AI agent that runs bobi reachable and authed?

The setup wizard's own digestion brain runs on the Claude Code CLI (see
``bobi/setup/llm.py``), and so does every agent in a team once it's
installed (``bobi/sdk.py``). So before the user gets anywhere, the
**harness** — the agent + its auth — has to be in place:

- the ``claude`` CLI present on PATH, and
- a usable credential: either ``ANTHROPIC_API_KEY`` in the environment
  (api_key mode) or subscription OAuth credentials on disk
  (``~/.claude/.credentials.json``, written by ``claude auth login``).

This module is the single, pure read of that state. The web UI surfaces it on
the welcome screen (which agent runs your harness, and whether you're logged
in) and ``/api/message`` uses it as a backstop so an unauthenticated wizard
fails with one clear instruction instead of a cryptic stream error.

Today the only harness is Claude Code; the shape here (``agent`` field, generic
"not authenticated" copy) leaves room for additional providers later without
the UI having to change.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass

AGENT_NAME = "Claude Code"
LOGIN_COMMAND = "claude auth login"

# Claude Code stores its subscription OAuth credentials in the macOS login
# keychain under this service (not in ~/.claude/.credentials.json, which is the
# container/volume layout). Checking the keychain is what keeps the local check
# from false-negativing for the common Mac dev — a false "not logged in" on a
# working machine is worse than no check at all.
_MACOS_KEYCHAIN_SERVICE = "Claude Code-credentials"


@dataclass(frozen=True)
class HarnessStatus:
    """Whether the agent harness is ready to run, and on what."""

    agent: str            # the harness agent, e.g. "Claude Code"
    model: str            # resolved model label, or "default" when unset
    cli_present: bool     # the `claude` CLI is on PATH
    authenticated: bool   # cli present AND a usable credential exists
    auth_mode: str | None  # "api_key" | "subscription" | None
    login_command: str    # what to run to authenticate

    def to_dict(self) -> dict:
        return asdict(self)


def _macos_keychain_has_claude() -> bool:
    """True iff the macOS login keychain holds Claude Code's OAuth credential.

    Uses ``security find-generic-password`` *without* ``-w`` — finding the item
    returns 0/non-zero and never prompts for an unlock (only printing the secret
    would). Any failure (non-macOS, no `security`, timeout) is a clean False.
    """
    if platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _MACOS_KEYCHAIN_SERVICE],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 — absence of the tool/entry is just "no"
        return False


def _oauth_credentials_present() -> bool:
    """True iff `claude auth login` has stored subscription OAuth credentials —
    on disk (container/Linux) or in the macOS keychain (the Mac dev default)."""
    # Reuse the one canonical creds-path resolver so the local check and the
    # container subscription-bootstrap can never drift.
    from bobi import auth_bootstrap

    return auth_bootstrap.credentials_exist() or _macos_keychain_has_claude()


def harness_status(model: str | None = None) -> HarnessStatus:
    """Read the current harness state. Cheap to poll for the welcome-screen
    Re-check button (the macOS keychain probe spawns ``security``, ~10-50ms).

    ``model`` is the model the wizard/runtime is configured to use (the setup
    server's ``--model``); ``None`` means the CLI picks its default.

    ``authenticated`` means **a credential is present**, not that it's valid: a
    stale/expired keychain entry or a garbage ``ANTHROPIC_API_KEY`` both read as
    authenticated. Validating would cost an API round-trip, so we accept this —
    a bad credential degrades to the digestion call failing with a clear error,
    not a security hole.
    """
    cli_present = shutil.which("claude") is not None

    # ANTHROPIC_API_KEY silently outranks subscription OAuth creds (it bills the
    # API), so it's the authoritative mode whenever it's set — mirror the
    # container precedence (auth_bootstrap §6.1) here.
    if os.environ.get("ANTHROPIC_API_KEY"):
        auth_mode: str | None = "api_key"
    elif _oauth_credentials_present():
        auth_mode = "subscription"
    else:
        auth_mode = None

    return HarnessStatus(
        agent=AGENT_NAME,
        model=model or "default",
        cli_present=cli_present,
        authenticated=cli_present and auth_mode is not None,
        auth_mode=auth_mode,
        login_command=LOGIN_COMMAND,
    )
