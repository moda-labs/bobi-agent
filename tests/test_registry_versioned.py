"""Tests for versioned per-team fetch + install (#440 Phase 2).

`registry.fetch(..., version=…)` resolves a `name@version` to the immutable
per-team release asset `…/teams-latest/<name>-<version>.tar.gz` and installs it,
reusing the hardened `_install_team_tar` extraction. The pinned path is the unit
of reproducible distribution, so its URL shape, token-auth, fallback rules, and
the `name@version` parse (D-6) are contract, not incidental.
"""

import io
import tarfile
from io import BytesIO

import httpx
import pytest
import yaml

from bobi import registry


# --- helpers -----------------------------------------------------------------

def _asset_tarball(name: str = "eng-team", version: str | None = "1.1.0") -> bytes:
    """A per-team release asset: a single `<name>/` dir holding agent.yaml.

    Same one-team-per-archive contract as `fetch_from_url` (the shallowest
    agent.yaml wins), so it flows through the shared `_install_team_tar` core.
    """
    buf = BytesIO()
    ver_line = f"version: '{version}'\n" if version else ""
    body = f"agent: {name}\n{ver_line}entry_point: manager\n".encode()
    role = b"# Manager\n"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{name}/agent.yaml")
        info.size = len(body)
        tar.addfile(info, BytesIO(body))
        info = tarfile.TarInfo(f"{name}/roles/manager/ROLE.md")
        info.size = len(role)
        tar.addfile(info, BytesIO(role))
    return buf.getvalue()


def _repo_tarball(name: str = "eng-team", version: str = "1.1.0") -> bytes:
    """A whole-repo GitHub tarball: `<prefix>/agents/<name>/…` (fallback path)."""
    buf = BytesIO()
    prefix = "moda-labs-bobi-deadbee"
    body = f"agent: {name}\nversion: '{version}'\nentry_point: manager\n".encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{prefix}/agents/{name}/agent.yaml")
        info.size = len(body)
        tar.addfile(info, BytesIO(body))
    return buf.getvalue()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("bobi.paths._root", tmp_path)
    # A stable fake token so the asset download carries the auth header.
    monkeypatch.setattr(registry, "_github_token", lambda: "tok-abc")
    return tmp_path


def _router(monkeypatch, routes: dict, *, capture: list | None = None):
    """Patch the pooled HTTP get with a URL-substring router.

    `routes` maps a URL substring → (status, payload bytes). Unmatched URLs
    404. `capture`, if given, records every (url, headers) the code requested —
    the seam the token-auth assertions read.
    """
    def fake_get(url, headers=None, timeout=None, **kw):
        if capture is not None:
            capture.append((url, headers))
        for needle, (status, payload) in routes.items():
            if needle in url:
                return httpx.Response(status, content=payload,
                                      request=httpx.Request("GET", url))
        return httpx.Response(404, content=b"", request=httpx.Request("GET", url))

    monkeypatch.setattr("bobi.http.get", fake_get)


# --- D-6: name@version parse rule --------------------------------------------

class TestSplitTeamRef:
    def test_bare_name_has_no_version(self):
        assert registry.split_team_ref("eng-team") == ("eng-team", None)

    def test_name_at_version(self):
        assert registry.split_team_ref("eng-team@1.1.0") == ("eng-team", "1.1.0")

    def test_splits_on_the_last_at(self):
        # D-6: split on the LAST '@' — a '@' earlier in the name is preserved.
        assert registry.split_team_ref("a@b@1.1.0") == ("a@b", "1.1.0")

    def test_trailing_at_is_latest(self):
        assert registry.split_team_ref("eng-team@") == ("eng-team", None)


# --- versioned fetch ---------------------------------------------------------

def test_fetch_pinned_downloads_only_the_versioned_asset(project, monkeypatch):
    """A pinned fetch hits the per-team asset URL and NOT tarball/main."""
    calls = []
    _router(monkeypatch,
            {"teams-latest/eng-team-1.1.0.tar.gz": (200, _asset_tarball())},
            capture=calls)

    dest = registry.fetch(project, "eng-team", version="1.1.0", repo="o/r")

    assert dest == registry.cache_path(project, "eng-team")
    assert (dest / "agent.yaml").is_file()
    urls = [u for u, _ in calls]
    assert any("teams-latest/eng-team-1.1.0.tar.gz" in u for u in urls)
    assert not any("tarball/main" in u for u in urls)
    # meta pins the concrete version + records the asset source.
    assert registry.cached_version(project, "eng-team") == "1.1.0"
    meta = registry._read_meta(project, "eng-team")
    assert "eng-team-1.1.0.tar.gz" in meta["source"]


def test_pinned_asset_download_is_token_authed(project, monkeypatch):
    """The asset download must carry the GitHub token (works on a private repo),
    i.e. it must NOT route through the un-authed fetch_from_url/pooled.get."""
    calls = []
    _router(monkeypatch,
            {"teams-latest/eng-team-1.1.0.tar.gz": (200, _asset_tarball())},
            capture=calls)

    registry.fetch(project, "eng-team", version="1.1.0", repo="o/r")

    asset_calls = [(u, h) for u, h in calls if "teams-latest" in u]
    assert asset_calls, "asset URL was never requested"
    for _, headers in asset_calls:
        assert headers and headers.get("Authorization") == "token tok-abc"


def test_fetch_latest_resolves_registry_version_to_versioned_asset(project, monkeypatch):
    """version=None reads the team's latest version and fetches THAT asset,
    not the whole-repo tarball."""
    calls = []
    _router(monkeypatch, {
        # repo membership + version resolution read raw files from main.
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"eng-team": {"version": "1.1.0"}}}).encode()),
        "agents/eng-team/agent.yaml": (200, b"version: '1.1.0'\nagent: eng-team\n"),
        "teams-latest/eng-team-1.1.0.tar.gz": (200, _asset_tarball()),
    }, capture=calls)

    dest = registry.fetch(project, "eng-team")

    assert (dest / "agent.yaml").is_file()
    urls = [u for u, _ in calls]
    assert any("teams-latest/eng-team-1.1.0.tar.gz" in u for u in urls)
    assert not any("tarball/main" in u for u in urls)


def test_unpinned_falls_back_to_repo_tarball_when_asset_404(project, monkeypatch, caplog):
    """An absent asset (unpinned) logs a warning and falls back to tarball/main."""
    _router(monkeypatch, {
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"eng-team": {"version": "1.1.0"}}}).encode()),
        "agents/eng-team/agent.yaml": (200, b"version: '1.1.0'\nagent: eng-team\n"),
        # No teams-latest asset → 404 → fallback.
        "tarball/main": (200, _repo_tarball()),
    })

    import logging
    with caplog.at_level(logging.WARNING):
        dest = registry.fetch(project, "eng-team")

    assert (dest / "agent.yaml").is_file()
    assert any("fall" in r.message.lower() or "fallback" in r.message.lower()
               for r in caplog.records)


def test_pinned_404_is_a_hard_error_never_falls_back(project, monkeypatch):
    """An explicit @version that 404s is a hard error naming team+version+URL —
    it must NOT silently fall back to latest or the repo tarball."""
    calls = []
    _router(monkeypatch, {"tarball/main": (200, _repo_tarball())}, capture=calls)

    with pytest.raises(RuntimeError) as exc:
        registry.fetch(project, "eng-team", version="9.9.9", repo="o/r")

    msg = str(exc.value)
    assert "eng-team" in msg and "9.9.9" in msg
    assert "teams-latest/eng-team-9.9.9.tar.gz" in msg
    assert not any("tarball/main" in u for u, _ in calls)


def test_versionless_team_fetches_the_rolling_asset(project, monkeypatch):
    """A version-less team (no version in the registry) resolves 'latest' to the
    rolling <team>.tar.gz (D-5) — there is no pinned asset for it."""
    calls = []
    _router(monkeypatch, {
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"smoke-team": {}}}).encode()),
        # version-less: no `version:` in agent.yaml
        "agents/smoke-team/agent.yaml": (200, b"agent: smoke-team\n"),
        "teams-latest/smoke-team.tar.gz": (200,
            _asset_tarball("smoke-team", version=None)),
    }, capture=calls)

    dest = registry.fetch(project, "smoke-team")

    assert (dest / "agent.yaml").is_file()
    urls = [u for u, _ in calls]
    # rolling asset, NOT a versioned one
    assert any(u.endswith("teams-latest/smoke-team.tar.gz") for u in urls)


def test_existing_signature_unpinned_still_installs(project, monkeypatch):
    """Back-compat: fetch(project, name) (no version kwarg) still works."""
    _router(monkeypatch, {
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"eng-team": {"version": "1.1.0"}}}).encode()),
        "agents/eng-team/agent.yaml": (200, b"version: '1.1.0'\nagent: eng-team\n"),
        "teams-latest/eng-team-1.1.0.tar.gz": (200, _asset_tarball()),
    })
    dest = registry.fetch(project, "eng-team")
    assert (dest / "agent.yaml").is_file()
