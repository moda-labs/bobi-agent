"""Agent pack registry — fetch, cache, and version-check agent packs.

Packs are fetched from a GitHub repo (default: moda-labs/moda-agents) and
cached at ~/.modastack/agents/<name>/. A .meta.json file tracks the
installed version and fetch timestamp.

Resolution order (handled by callers in cli.py / resolver.py):
  1. <project>/agents/<name>            — project-level
  2. <project>/.modastack/agents/<name> — project override
  3. ~/.modastack/agents/<name>          — user cache (this module)
"""

from __future__ import annotations

import json
import logging
import shutil
import ssl
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import certifi
import yaml

log = logging.getLogger(__name__)

DEFAULT_REPO = "moda-labs/modastack"
CACHE_DIR = Path.home() / ".modastack" / "agents"
GITHUB_RAW = "https://raw.githubusercontent.com"


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def _github_token() -> str:
    """Get a GitHub token from env or gh CLI."""
    import os
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        try:
            import subprocess
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                token = result.stdout.strip()
        except Exception:
            pass
    return token


def _urlopen(url: str, timeout: int = 10):
    headers = {"User-Agent": "modastack"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def _meta_path(name: str) -> Path:
    return CACHE_DIR / name / ".meta.json"


def _read_meta(name: str) -> dict:
    p = _meta_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_meta(name: str, version: str, repo: str) -> None:
    meta = {
        "version": version,
        "source": f"github:{repo}",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _meta_path(name).write_text(json.dumps(meta, indent=2))


def _read_remote_version(name: str, repo: str = DEFAULT_REPO) -> str | None:
    """Fetch just defaults.yaml from GitHub to read the remote version."""
    url = f"{GITHUB_RAW}/{repo}/main/agents/{name}/defaults.yaml"
    try:
        with _urlopen(url, timeout=5) as resp:
            data = yaml.safe_load(resp.read())
            return data.get("version") if data else None
    except Exception:
        return None


def _read_local_version(name: str) -> str | None:
    """Read version from cached pack's defaults.yaml."""
    defaults = CACHE_DIR / name / "defaults.yaml"
    if not defaults.exists():
        return None
    try:
        data = yaml.safe_load(defaults.read_text())
        return data.get("version") if data else None
    except Exception:
        return None


def is_cached(name: str) -> bool:
    """Check if a pack exists in the user cache."""
    return (CACHE_DIR / name / "defaults.yaml").exists()


def check_update(name: str, repo: str = DEFAULT_REPO) -> tuple[str | None, str | None]:
    """Compare local vs remote version. Returns (local_version, remote_version).

    If remote is None, the check failed (network error, pack not found).
    """
    local = _read_local_version(name)
    remote = _read_remote_version(name, repo)
    return local, remote


def fetch(name: str, repo: str = DEFAULT_REPO) -> Path:
    """Download an agent pack from GitHub and install to cache.

    Downloads the repo tarball and extracts just the named pack directory.
    """
    url = f"https://api.github.com/repos/{repo}/tarball/main"
    log.info(f"Fetching agent pack '{name}' from {repo}")

    try:
        with _urlopen(url, timeout=30) as resp:
            tarball = BytesIO(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"Agent repo '{repo}' not found on GitHub") from e
        raise RuntimeError(f"Failed to fetch from GitHub: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to fetch from GitHub: {e}") from e

    with tarfile.open(fileobj=tarball, mode="r:gz") as tar:
        prefix = None
        pack_prefix = None
        pack_members = []
        for member in tar.getmembers():
            parts = member.name.split("/")
            if prefix is None:
                prefix = parts[0]
            # Match agents/<name>/... in the tarball
            if len(parts) >= 3 and parts[1] == "agents" and parts[2] == name:
                pack_members.append(member)
                if pack_prefix is None:
                    pack_prefix = f"{parts[0]}/agents/{name}"
            # Also match <name>/... at root (flat layout)
            elif len(parts) >= 2 and parts[1] == name and not pack_members:
                pack_members.append(member)
                if pack_prefix is None:
                    pack_prefix = f"{parts[0]}/{name}"

        if not pack_members:
            raise RuntimeError(
                f"Agent pack '{name}' not found in {repo}. "
                f"Available packs can be listed with: modastack agents list --remote"
            )

        dest = CACHE_DIR / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            tar.extractall(tmp, members=pack_members)
            extracted = Path(tmp) / pack_prefix
            if not extracted.is_dir():
                raise RuntimeError(f"Extraction failed for '{name}'")
            shutil.copytree(extracted, dest, dirs_exist_ok=True)

    version = _read_local_version(name) or "unknown"
    _write_meta(name, version, repo)
    log.info(f"Installed {name} v{version} to {dest}")
    return dest


def list_remote(repo: str = DEFAULT_REPO) -> list[dict]:
    """List agent packs available in the remote registry."""
    url = f"{GITHUB_RAW}/{repo}/main/agents/registry.yaml"
    try:
        with _urlopen(url, timeout=5) as resp:
            data = yaml.safe_load(resp.read())
    except Exception:
        return []
    if not data or "agents" not in data:
        return []
    return [
        {"name": name, **info}
        for name, info in data["agents"].items()
    ]


def list_cached() -> list[dict]:
    """List agent packs in the local cache with version info."""
    if not CACHE_DIR.is_dir():
        return []
    packs = []
    for d in sorted(CACHE_DIR.iterdir()):
        if d.is_dir() and (d / "defaults.yaml").exists():
            meta = _read_meta(d.name)
            version = _read_local_version(d.name) or "unknown"
            packs.append({
                "name": d.name,
                "version": version,
                "source": meta.get("source", "unknown"),
                "fetched_at": meta.get("fetched_at", ""),
            })
    return packs
