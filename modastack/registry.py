"""Agent pack registry — fetch, cache, and version-check agent packs.

Packs are fetched from a GitHub repo (default: moda-labs/moda-agents) and
cached at <project>/.modastack/agents/<name>/. A .meta.json file tracks the
installed version and fetch timestamp.

Resolution order (handled by callers in cli.py / resolver.py):
  1. <project>/agents/<name>            — project-level (checked in)
  2. <project>/.modastack/agents/<name> — local agents (overrides + cached)
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
GITHUB_RAW = "https://raw.githubusercontent.com"


def _cache_dir(project_path: Path) -> Path:
    return project_path / ".modastack" / "agents"


def _all_registries(project_path: Path) -> list[str]:
    """Get all configured registries (default + user-added)."""
    try:
        from modastack.config import Config
        cfg = Config.load(project_path)
        user_registries = cfg.registries or []
    except Exception:
        user_registries = []
    seen = set()
    result = []
    for repo in [DEFAULT_REPO] + user_registries:
        if repo not in seen:
            seen.add(repo)
            result.append(repo)
    return result


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


def _meta_path(project_path: Path, name: str) -> Path:
    return _cache_dir(project_path) / name / ".meta.json"


def _read_meta(project_path: Path, name: str) -> dict:
    p = _meta_path(project_path, name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_meta(project_path: Path, name: str, version: str, repo: str) -> None:
    meta = {
        "version": version,
        "source": f"github:{repo}",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _meta_path(project_path, name).write_text(json.dumps(meta, indent=2))


def _read_remote_version(name: str, repo: str = DEFAULT_REPO) -> str | None:
    """Fetch just agent.yaml from GitHub to read the remote version."""
    url = f"{GITHUB_RAW}/{repo}/main/agents/{name}/agent.yaml"
    try:
        with _urlopen(url, timeout=5) as resp:
            data = yaml.safe_load(resp.read())
            return data.get("version") if data else None
    except Exception:
        return None


def _read_local_version(project_path: Path, name: str) -> str | None:
    """Read version from cached pack's agent.yaml."""
    defaults = _cache_dir(project_path) / name / "agent.yaml"
    if not defaults.exists():
        return None
    try:
        data = yaml.safe_load(defaults.read_text())
        return data.get("version") if data else None
    except Exception:
        return None


def is_cached(project_path: Path, name: str) -> bool:
    """Check if a pack exists in the project cache."""
    return (_cache_dir(project_path) / name / "agent.yaml").exists()


def check_update(project_path: Path, name: str, repo: str | None = None) -> tuple[str | None, str | None]:
    """Compare local vs remote version. Returns (local_version, remote_version)."""
    local = _read_local_version(project_path, name)
    registries = [repo] if repo else _all_registries(project_path)
    for r in registries:
        remote = _read_remote_version(name, r)
        if remote:
            return local, remote
    return local, None


def fetch(project_path: Path, name: str, repo: str | None = None) -> Path:
    """Download an agent pack from GitHub and install to project cache."""
    if not repo:
        registries = _all_registries(project_path)
        for r in registries:
            if _read_remote_version(name, r):
                repo = r
                break
        if not repo:
            raise RuntimeError(
                f"Agent pack '{name}' not found in any registry. "
                f"Searched: {', '.join(registries)}"
            )

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

    cache = _cache_dir(project_path)
    with tarfile.open(fileobj=tarball, mode="r:gz") as tar:
        prefix = None
        pack_prefix = None
        pack_members = []
        for member in tar.getmembers():
            parts = member.name.split("/")
            if prefix is None:
                prefix = parts[0]
            if len(parts) >= 3 and parts[1] == "agents" and parts[2] == name:
                pack_members.append(member)
                if pack_prefix is None:
                    pack_prefix = f"{parts[0]}/agents/{name}"
            elif len(parts) >= 2 and parts[1] == name and not pack_members:
                pack_members.append(member)
                if pack_prefix is None:
                    pack_prefix = f"{parts[0]}/{name}"

        if not pack_members:
            raise RuntimeError(
                f"Agent pack '{name}' not found in {repo}. "
                f"Available packs can be listed with: modastack agents list --remote"
            )

        dest = cache / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            tar.extractall(tmp, members=pack_members)
            extracted = Path(tmp) / pack_prefix
            if not extracted.is_dir():
                raise RuntimeError(f"Extraction failed for '{name}'")
            shutil.copytree(extracted, dest, dirs_exist_ok=True)

    version = _read_local_version(project_path, name) or "unknown"
    _write_meta(project_path, name, version, repo)
    log.info(f"Installed {name} v{version} to {dest}")
    return dest


def _list_remote_single(repo: str) -> list[dict]:
    """List agent packs from a single registry."""
    url = f"{GITHUB_RAW}/{repo}/main/agents/registry.yaml"
    try:
        with _urlopen(url, timeout=5) as resp:
            data = yaml.safe_load(resp.read())
    except Exception:
        return []
    if not data or "agents" not in data:
        return []
    return [
        {"name": name, "registry": repo, **info}
        for name, info in data["agents"].items()
    ]


def list_remote(project_path: Path | None = None, repo: str | None = None) -> list[dict]:
    """List agent packs available across all registries."""
    if repo:
        return _list_remote_single(repo)
    seen: set[str] = set()
    results: list[dict] = []
    registries = _all_registries(project_path) if project_path else [DEFAULT_REPO]
    for r in registries:
        for pack in _list_remote_single(r):
            if pack["name"] not in seen:
                seen.add(pack["name"])
                results.append(pack)
    return results


def list_cached(project_path: Path) -> list[dict]:
    """List agent packs in the project cache with version info."""
    cache = _cache_dir(project_path)
    if not cache.is_dir():
        return []
    packs = []
    for d in sorted(cache.iterdir()):
        if d.is_dir() and (d / "agent.yaml").exists():
            meta = _read_meta(project_path, d.name)
            version = _read_local_version(project_path, d.name) or "unknown"
            packs.append({
                "name": d.name,
                "version": version,
                "source": meta.get("source", "unknown"),
                "fetched_at": meta.get("fetched_at", ""),
            })
    return packs
