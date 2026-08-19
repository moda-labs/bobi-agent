"""Tests for versioned per-team fetch + install (#440 Phase 2).

`registry.fetch(..., version=…)` resolves a `name@version` to the immutable
per-team release asset `…/teams-latest/<name>-<version>.tar.gz` and installs it,
reusing the hardened `_install_team_tar` extraction. The pinned path is the unit
of reproducible distribution, so its URL shape, token-auth, no-fallback missing
asset behavior, and the `name@version` parse (D-6) are contract, not incidental.
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
    """A whole-repo GitHub tarball used only to assert it is not fetched."""
    buf = BytesIO()
    prefix = "moda-labs-bobi-deadbee"
    body = f"agent: {name}\nversion: '{version}'\nentry_point: manager\n".encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{prefix}/agents/{name}/agent.yaml")
        info.size = len(body)
        tar.addfile(info, BytesIO(body))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # Point the shared cache (BOBI_HOME/cache/agents) at a temp home.
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    # A stable fake token so the asset download carries the auth header.
    monkeypatch.setattr(registry, "_github_token", lambda: "tok-abc")


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

def test_fetch_pinned_downloads_only_the_versioned_asset(tmp_path, monkeypatch):
    """A pinned fetch hits the per-team asset URL and NOT tarball/main."""
    calls = []
    _router(monkeypatch,
            {"teams-latest/eng-team-1.1.0.tar.gz": (200, _asset_tarball())},
            capture=calls)

    dest = registry.fetch("eng-team", version="1.1.0", repo="o/r")

    assert dest == registry.cache_path("eng-team")
    # The install must land inside this test's temp home, never the real
    # ~/.bobi — BOBI_HOME is the only thing standing between the suite and
    # the developer's live cache.
    assert dest.is_relative_to(tmp_path)
    assert (dest / "agent.yaml").is_file()
    urls = [u for u, _ in calls]
    assert any("teams-latest/eng-team-1.1.0.tar.gz" in u for u in urls)
    assert not any("tarball/main" in u for u in urls)
    # meta pins the concrete version + records the asset source.
    assert registry.cached_version("eng-team") == "1.1.0"
    meta = registry._read_meta("eng-team")
    assert "eng-team-1.1.0.tar.gz" in meta["source"]


def test_pinned_asset_download_is_token_authed(tmp_path, monkeypatch):
    """The asset download must carry the GitHub token (works on a private repo),
    i.e. it must NOT route through the un-authed fetch_from_url/pooled.get."""
    calls = []
    _router(monkeypatch,
            {"teams-latest/eng-team-1.1.0.tar.gz": (200, _asset_tarball())},
            capture=calls)

    registry.fetch("eng-team", version="1.1.0", repo="o/r")

    asset_calls = [(u, h) for u, h in calls if "teams-latest" in u]
    assert asset_calls, "asset URL was never requested"
    for _, headers in asset_calls:
        assert headers and headers.get("Authorization") == "token tok-abc"


def test_fetch_latest_resolves_registry_version_to_versioned_asset(tmp_path, monkeypatch):
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

    dest = registry.fetch("eng-team")

    assert (dest / "agent.yaml").is_file()
    urls = [u for u, _ in calls]
    assert any("teams-latest/eng-team-1.1.0.tar.gz" in u for u in urls)
    assert not any("tarball/main" in u for u in urls)


def test_unpinned_asset_404_is_hard_error(tmp_path, monkeypatch):
    """An absent latest asset is a hard error; no repo tarball fallback."""
    calls = []
    _router(monkeypatch, {
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"eng-team": {"version": "1.1.0"}}}).encode()),
        "agents/eng-team/agent.yaml": (200, b"version: '1.1.0'\nagent: eng-team\n"),
        "tarball/main": (200, _repo_tarball()),
    }, capture=calls)

    with pytest.raises(RuntimeError, match="no published asset"):
        registry.fetch("eng-team")
    urls = [u for u, _ in calls]
    assert any("teams-latest/eng-team-1.1.0.tar.gz" in u for u in urls)
    assert not any("tarball/main" in u for u in urls)


def test_pinned_404_is_a_hard_error_never_falls_back(tmp_path, monkeypatch):
    """An explicit @version that 404s is a hard error naming team+version+URL —
    it must NOT silently fall back to latest or the repo tarball."""
    calls = []
    _router(monkeypatch, {"tarball/main": (200, _repo_tarball())}, capture=calls)

    with pytest.raises(RuntimeError) as exc:
        registry.fetch("eng-team", version="9.9.9", repo="o/r")

    msg = str(exc.value)
    assert "eng-team" in msg and "9.9.9" in msg
    assert "teams-latest/eng-team-9.9.9.tar.gz" in msg
    assert not any("tarball/main" in u for u, _ in calls)


def test_versionless_team_fetches_the_rolling_asset(tmp_path, monkeypatch):
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

    dest = registry.fetch("smoke-team")

    assert (dest / "agent.yaml").is_file()
    urls = [u for u, _ in calls]
    # rolling asset, NOT a versioned one
    assert any(u.endswith("teams-latest/smoke-team.tar.gz") for u in urls)


def test_unreadable_remote_version_never_downgrades_to_rolling(tmp_path,
                                                               monkeypatch):
    """D032 — a transient version read must not silently install main.

    `_read_remote_version` swallowed every exception and returned None, and
    fetch read None as "version-less team" → the rolling <name>.tar.gz, which
    is clobbered on every push to main. So `bobi install eng-team` during a
    raw.githubusercontent.com timeout or rate-limit silently installed
    UNRELEASED main content instead of the latest published immutable asset,
    with nothing distinguishing the two cases.
    """
    calls = []
    _router(monkeypatch, {
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"eng-team": {"version": "1.1.0"}}}).encode()),
        # The version read fails transiently (rate-limited).
        "agents/eng-team/agent.yaml": (429, b"rate limited"),
        # The rolling asset is available and would happily install.
        "teams-latest/eng-team.tar.gz": (200, _asset_tarball("eng-team", None)),
    }, capture=calls)

    with pytest.raises(RuntimeError, match="version"):
        registry.fetch("eng-team")

    assert not any(u.endswith("teams-latest/eng-team.tar.gz") for u, _ in calls), (
        "a failed version read must not fall through to the rolling asset")


def test_a_missing_agent_yaml_is_not_a_transient_failure(tmp_path, monkeypatch):
    """A 404 is an answer, not a hiccup.

    No agent.yaml at main means the team is version-less or absent from this
    repo — both of which the asset fetch reports accurately. Only a read that
    genuinely FAILED (timeout, rate limit, 5xx) may block the fetch, or an
    ordinary "no such team" turns into a misleading transient-failure error.
    """
    _router(monkeypatch, {
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"ghost": {}}}).encode()),
        # agent.yaml 404s, and so does every asset.
    })

    with pytest.raises(RuntimeError, match="no published asset"):
        registry.fetch("ghost")


def test_a_genuinely_versionless_team_is_not_an_error(tmp_path, monkeypatch):
    """The legitimate None — a 200 whose agent.yaml carries no version — still
    resolves to the rolling asset. Only the FAILURE case is now an error."""
    _router(monkeypatch, {
        "agents/registry.yaml": (200, yaml.dump(
            {"agents": {"smoke-team": {}}}).encode()),
        "agents/smoke-team/agent.yaml": (200, b"agent: smoke-team\n"),
        "teams-latest/smoke-team.tar.gz": (200,
            _asset_tarball("smoke-team", version=None)),
    })

    dest = registry.fetch("smoke-team")
    assert (dest / "agent.yaml").is_file()
