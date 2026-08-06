"""Shared git shell-outs.

Small, best-effort readers for facts bobi needs about a local checkout.
Every helper here answers with an empty value rather than raising: these
are called on startup paths (subscription auto-detection, workflow repo
resolution) where "not a git repo" and "git is unhappy" are ordinary
states, not errors.

Interpreting what is read is deliberately NOT here. Callers disagree about
what a remote URL means — subscription detection accepts github.com only
(D075), workflow repo resolution matches a slug on any host — and folding
those into one normalizer would silently change one of them.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def origin_url(repo_path: Path | str) -> str:
    """Return the ``origin`` remote URL for the checkout at *repo_path*.

    Empty when *repo_path* is not a git repository, has no ``origin``, or
    git could not be run at all.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("git remote get-url origin failed in %s: %s", repo_path, e)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
