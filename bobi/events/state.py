"""Event-server transport session state — JSON files under run/state/.

- Per-session deployment records (deployment_id + api_key) and the path of
  each session's event cursor (the cursor JSON itself is read and written by
  the session machinery in bobi/subagent.py).
- The instance-wide bubble credential (bubble_id + bubble_key), minted and
  consumed by bobi/events/server.py:ensure_bubble.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from bobi import paths
from bobi.fsutil import atomic_write_json


# One deployment per SESSION, never shared. When sessions shared one
# deployment (a single deployment.json per project), every agent's
# subscriptions were unioned onto it and the event server fanned every
# matching event out to every connected agent — project leads received
# the user's Slack DMs to the director and replied to them.


def _safe_session(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_"


def deployment_state_path(project_path: Path, session: str) -> Path:
    return (paths.state_path(project_path) / "deployments"
            / f"{_safe_session(session)}.json")


def session_cursor_path(project_path: Path, session: str) -> Path:
    """Per-session event cursor. Seq numbers are per-deployment, so a shared
    cursor file would corrupt replay positions across sessions."""
    return (paths.state_path(project_path) / "cursors"
            / f"{_safe_session(session)}.json")


def load_deployment_state(project_path: Path, session: str) -> dict:
    """Load a session's event server deployment_id + api_key."""
    state_file = deployment_state_path(project_path, session)
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_deployment_state(project_path: Path, session: str,
                          deployment_id: str, api_key: str) -> None:
    """Save a session's event server deployment_id + api_key."""
    state_file = deployment_state_path(project_path, session)
    # Atomic: a torn write here reads back as {} and the session loses the
    # credentials it needs to reach its event server.
    atomic_write_json(state_file, {
        "deployment_id": deployment_id,
        "api_key": api_key,
    }, indent=None)


# --- bubble (trust-domain) state -------------------------------------------
# One bubble per running instance. Minted once (lazily, lock-protected) and
# shared by every session of the instance via the local filesystem. The bubble
# key signs publishes + join registrations; it is a private local secret stored
# OUTSIDE .env (which is template-expanded into agent configs). See
# bobi/events/server.py:ensure_bubble.


def bubble_state_path(project_path: Path) -> Path:
    return paths.state_path(project_path) / "bubble.json"


def load_bubble_state(project_path: Path) -> dict:
    """Load the instance's bubble_id + bubble_key, or {} if not yet minted."""
    state_file = bubble_state_path(project_path)
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_bubble_state(project_path: Path, bubble_id: str, bubble_key: str) -> None:
    """Persist the bubble credential at mode 0600 (it is a signing secret)."""
    state_file = bubble_state_path(project_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 so the key is never group/world-readable.
    fd = os.open(str(state_file), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({
            "bubble_id": bubble_id,
            "bubble_key": bubble_key,
        }).encode())
    finally:
        os.close(fd)
    try:
        os.chmod(state_file, 0o600)
    except OSError:
        pass


def clear_bubble_state(project_path: Path) -> None:
    """Drop the bubble credential — a subsequent start mints a fresh bubble."""
    bubble_state_path(project_path).unlink(missing_ok=True)
