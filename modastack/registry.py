"""Agent team registry — fetch, cache, and version-check agent teams.

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
import tarfile
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
import yaml

from modastack import paths

log = logging.getLogger(__name__)

DEFAULT_REPO = "moda-labs/modastack"
GITHUB_RAW = "https://raw.githubusercontent.com"
# The single rolling GitHub release that holds every published team asset:
# rolling `<team>.tar.gz` (clobbered each main push) + immutable
# `<team>-<version>.tar.gz` (published once). Both `install` and `deploy`
# resolve a `name@version` to an asset under this tag (#440).
RELEASE_TAG = "teams-latest"


def split_team_ref(ref: str) -> tuple[str, str | None]:
    """Split a `name@version` reference into `(name, version_or_None)`.

    D-6: split on the **last** `@`, so a name that itself contains `@` keeps it.
    A bare name (no `@`) or a trailing `@` (e.g. `eng-team@`) means "latest" →
    version is ``None``. The single authority both `install`/`agents update`
    (cli.py) and `deploy` (`team:`) use, so the parse rule lives in one place.
    """
    if "@" in ref:
        name, _, version = ref.rpartition("@")
        if name:  # non-empty name before the last '@'
            return name, (version or None)
    return ref, None


def _asset_url(repo: str, name: str, version: str | None) -> str:
    """The per-team release asset URL. A concrete `version` → the immutable
    `<name>-<version>.tar.gz`; `version is None` → the rolling `<name>.tar.gz`."""
    fname = f"{name}-{version}.tar.gz" if version else f"{name}.tar.gz"
    return f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{fname}"


def _cache_dir(project_path: Path) -> Path:
    return paths.agents_dir(project_path)


def cache_path(project_path: Path, name: str) -> Path:
    """The install/deploy cache directory for a team (shared cache, D-3)."""
    return _cache_dir(project_path) / name


def cached_version(project_path: Path, name: str) -> str | None:
    """The version recorded in the cached pack's `.meta.json`, if any.

    Lets a pinned deploy reuse an already-installed `name@version` with no
    second download (§3.4) — the resolver checks this before fetching."""
    return _read_meta(project_path, name).get("version")


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


def _urlopen(url: str, timeout: int = 10) -> httpx.Response:
    from modastack import http as pooled

    headers: dict[str, str] = {}
    token = _github_token()
    if token:
        headers["Authorization"] = f"token {token}"
    resp = pooled.get(url, headers=headers or None, timeout=float(timeout))
    resp.raise_for_status()
    return resp


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


def _write_meta(project_path: Path, name: str, version: str, source: str) -> None:
    meta = {
        "version": version,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _meta_path(project_path, name).write_text(json.dumps(meta, indent=2))


def _read_remote_version(name: str, repo: str = DEFAULT_REPO) -> str | None:
    """Fetch just agent.yaml from GitHub to read the remote version."""
    url = f"{GITHUB_RAW}/{repo}/main/agents/{name}/agent.yaml"
    try:
        resp = _urlopen(url, timeout=5)
        data = yaml.safe_load(resp.content)
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


def _repo_for(project_path: Path, name: str) -> str | None:
    """The first configured registry whose `registry.yaml` lists `name`.

    Membership-based (not version-based) so version-less teams resolve too —
    `_read_remote_version` returns None for them, which would otherwise hide
    them from repo resolution."""
    for r in _all_registries(project_path):
        for pack in _list_remote_single(r):
            if pack.get("name") == name:
                return r
    return None


def fetch(project_path: Path, name: str, *, version: str | None = None,
          repo: str | None = None) -> Path:
    """Download an agent team from GitHub and install it to the project cache.

    Resolution (#440 Phase 2):
      - `version` given → download **only** the immutable per-team asset
        `…/teams-latest/<name>-<version>.tar.gz`. A 404 is a **hard error**
        (a pin must resolve to exactly that pin — never a silent fallback).
      - `version` None → resolve the team's latest version from the registry
        and fetch that per-team asset (rolling `<name>.tar.gz` for a
        version-less team, D-5). A 404 here logs a warning and falls back to
        the whole-repo `tarball/main` path, so a repo that hasn't published
        assets yet still installs.

    `version` is keyword-only and defaults to None, so existing callers are
    unaffected (the only change at version=None is "per-team asset instead of
    the repo tarball, with the repo tarball as a logged fallback")."""
    pinned = version is not None
    if not repo:
        repo = _repo_for(project_path, name)
        if not repo:
            raise RuntimeError(
                f"Agent team '{name}' not found in any registry. "
                f"Searched: {', '.join(_all_registries(project_path))}"
            )

    # The concrete version to fetch. For an explicit pin it's `version`; for
    # "latest" it's the registry's published version (None → version-less →
    # rolling asset).
    target = version if pinned else _read_remote_version(name, repo)
    asset_url = _asset_url(repo, name, target)
    try:
        return _fetch_asset(project_path, repo, name, target, asset_url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise RuntimeError(f"Failed to fetch '{name}' from {asset_url}: {e}") from e
        if pinned:
            raise RuntimeError(
                f"Agent team '{name}@{version}' has no published asset at "
                f"{asset_url}. A pinned version must exist — it is never "
                "resolved to 'latest' or the repo tarball."
            ) from e
        log.warning(
            "No release asset for '%s' at %s; falling back to the repo tarball "
            "at main.", name, asset_url)
    return _fetch_repo_tarball(project_path, name, repo)


def _fetch_asset(project_path: Path, repo: str, name: str,
                 version: str | None, url: str) -> Path:
    """Download + install one per-team release asset. Returns the cache dir.

    Token-authed via `_urlopen` (works against a private repo) — deliberately
    NOT `fetch_from_url`, whose `pooled.get` is un-authenticated. The asset is a
    single-team tarball, so it flows through the same hardened `_install_team_tar`
    extraction we trust for URL installs. Propagates `httpx.HTTPStatusError` so
    the caller can apply the pinned-vs-latest 404 policy."""
    log.info("Fetching agent team '%s'%s from %s", name,
             f"@{version}" if version else " (latest)", url)
    resp = _urlopen(url, timeout=30)
    tar = tarfile.open(fileobj=BytesIO(resp.content), mode="r:gz")
    dest, _ = _install_team_tar(project_path, tar, source=url,
                                source_meta=f"asset:{url}", name=name)
    return dest


def _fetch_repo_tarball(project_path: Path, name: str, repo: str) -> Path:
    """Install a team from the whole-repo `tarball/main` (the legacy / fallback
    path). Downloads the entire repo at main and extracts one `agents/<name>/`."""
    url = f"https://api.github.com/repos/{repo}/tarball/main"
    log.info(f"Fetching agent team '{name}' from {repo}")

    try:
        resp = _urlopen(url, timeout=30)
        tarball = BytesIO(resp.content)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
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
                f"Agent team '{name}' not found in {repo}. "
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
    _write_meta(project_path, name, version, f"github:{repo}")
    log.info(f"Installed {name} v{version} to {dest}")
    return dest


def _safe_members(tar: tarfile.TarFile, root: str) -> list[tarfile.TarInfo]:
    """Members of `tar` that live under `root`, with traversal/abs-path rejected.

    A team archive is arbitrary remote content (a public URL), so an attacker
    could craft entries like `../../etc/...` or absolute paths. We keep only
    regular files/dirs whose path stays within `root` and strip anything else.
    """
    prefix = f"{root}/" if root else ""
    safe: list[tarfile.TarInfo] = []
    for m in tar.getmembers():
        if not (m.name == root or m.name.startswith(prefix)):
            continue
        # No absolute paths, no `..` segments, no symlinks/hardlinks/devices.
        if m.name.startswith("/") or ".." in Path(m.name).parts:
            raise RuntimeError(f"Refusing unsafe path in archive: {m.name!r}")
        if not (m.isfile() or m.isdir()):
            log.debug("Skipping non-regular archive member: %s", m.name)
            continue
        safe.append(m)
    return safe


def fetch_from_url(project_path: Path, url: str,
                   name: str | None = None) -> tuple[Path, str]:
    """Download an agent team from a public `.tar.gz` URL and install it to the
    project cache. Returns (install_dir, team_name).

    The archive must contain exactly one team: a directory holding an
    `agent.yaml` (optionally nested under a wrapper directory, as GitHub's
    codeload tarballs are). The shallowest `agent.yaml` wins. The team name is
    taken from `name`, else the package's `agent:` field, else its directory
    name.

    No auth: the URL is assumed publicly fetchable (a release asset, raw blob,
    or your own server). This is the seam the container first-boot and CI use
    to inject a team without baking it into the image.
    """
    from modastack import http as pooled

    log.info("Fetching agent team from %s", url)
    try:
        resp = pooled.get(url, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Failed to fetch agent team from {url}: HTTP {e.response.status_code}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Failed to fetch agent team from {url}: {e}") from e

    try:
        tar = tarfile.open(fileobj=BytesIO(resp.content), mode="r:gz")
    except tarfile.TarError as e:
        raise RuntimeError(
            f"{url} is not a readable .tar.gz archive ({e}). Point --team-url at a "
            "gzipped tarball of one team directory."
        ) from e

    return _install_team_tar(project_path, tar, source=url,
                             source_meta=f"url:{url}", name=name)


def fetch_from_archive(project_path: Path, archive: Path,
                       name: str | None = None) -> tuple[Path, str]:
    """Install an agent team from a LOCAL `.tar.gz`/`.tgz` archive file.

    The on-disk twin of `fetch_from_url`: same one-team-per-archive contract and
    extraction safety, but the bytes come from the filesystem instead of HTTP.
    This is the seam ssh-push delivery uses — `modastack deploy` builds a local
    team package into a tarball, pushes it onto the instance's volume over
    `fly ssh`, and the instance runs `modastack install <pushed.tar.gz>`.
    """
    archive = Path(archive)
    try:
        tar = tarfile.open(archive, mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise RuntimeError(
            f"{archive} is not a readable .tar.gz archive ({e}). Pass a gzipped "
            "tarball of one team directory."
        ) from e

    return _install_team_tar(project_path, tar, source=str(archive),
                             source_meta=f"archive:{archive.name}", name=name)


def _install_team_tar(project_path: Path, tar: "tarfile.TarFile", *,
                      source: str, source_meta: str,
                      name: str | None) -> tuple[Path, str]:
    """Extract one team from an open tarfile into the project cache.

    Shared core of `fetch_from_url` and `fetch_from_archive`: find the shallowest
    `agent.yaml`, safely extract its team dir, resolve the team name, copy into
    the cache, and stamp install metadata. `source` is a human label for errors;
    `source_meta` is recorded as the install provenance.
    """
    with tar:
        agent_yaml = min(
            (m for m in tar.getmembers()
             if m.isfile() and m.name.rsplit("/", 1)[-1] == "agent.yaml"),
            key=lambda m: len(m.name.split("/")),
            default=None,
        )
        if agent_yaml is None:
            raise RuntimeError(
                f"No agent.yaml found in the archive at {source} — it does not look "
                "like an agent team package."
            )
        team_root = agent_yaml.name.rsplit("/", 1)[0] if "/" in agent_yaml.name else ""
        members = _safe_members(tar, team_root)

        cache = _cache_dir(project_path)
        with tempfile.TemporaryDirectory() as tmp:
            # `_safe_members` already rejected traversal/abs/links; the `data`
            # filter (Python 3.12+, backported to 3.11.4) is belt-and-suspenders
            # and the default from 3.14. Fall back cleanly on older runtimes.
            try:
                tar.extractall(tmp, members=members, filter="data")
            except TypeError:
                tar.extractall(tmp, members=members)
            extracted = Path(tmp) / team_root if team_root else Path(tmp)
            if not (extracted / "agent.yaml").is_file():
                raise RuntimeError(f"Extraction failed for the team at {source}")

            resolved = (
                name
                or _agent_name_from_yaml(extracted / "agent.yaml")
                or (Path(team_root).name if team_root else "")
            )
            if not resolved:
                raise RuntimeError(
                    f"Could not determine a team name from {source}; pass an explicit name."
                )

            dest = cache / resolved
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(extracted, dest, dirs_exist_ok=True)

    version = _read_local_version(project_path, resolved) or "unknown"
    _write_meta(project_path, resolved, version, source_meta)
    log.info("Installed %s v%s from %s to %s", resolved, version, source, dest)
    return dest, resolved


def _agent_name_from_yaml(agent_yaml: Path) -> str | None:
    """Read the team name from a package's agent.yaml `agent:` field."""
    try:
        data = yaml.safe_load(agent_yaml.read_text())
        name = (data or {}).get("agent")
        return str(name) if name else None
    except Exception:
        return None


def _list_remote_single(repo: str) -> list[dict]:
    """List agent teams from a single registry."""
    url = f"{GITHUB_RAW}/{repo}/main/agents/registry.yaml"
    try:
        resp = _urlopen(url, timeout=5)
        data = yaml.safe_load(resp.content)
    except Exception:
        return []
    if not data or "agents" not in data:
        return []
    return [
        {"name": name, "registry": repo, **info}
        for name, info in data["agents"].items()
    ]


def list_remote(project_path: Path | None = None, repo: str | None = None) -> list[dict]:
    """List agent teams available across all registries."""
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
    """List agent teams in the project cache with version info."""
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
