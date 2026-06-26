"""Deploy resolution of a registry team by `name@version` (#440 Phase 2, D-2).

`resolve_team_dir` is the single seam every deploy consumer (secret-prune scan,
deps-render, deps-hash, and the ssh-push at the `deploy()` body) routes through,
so a pinned `team: <name>@<version>` lands the right package everywhere. The
local `team:` path stays byte-for-byte unchanged; an explicit pin never falls
back to a local dir.
"""

from pathlib import Path

import pytest

from bobi import deploy as D
from bobi import registry


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("bobi.paths._root", tmp_path)
    return tmp_path


def _local_team(project: Path, name: str = "eng-team") -> Path:
    pkg = project / "agents" / name
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text(f"agent: {name}\nentry_point: manager\n")
    return pkg


def _cached_team(project: Path, name: str, version: str) -> Path:
    """Mimic what registry.fetch would land in the shared cache."""
    dest = registry.cache_path(project, name)
    dest.mkdir(parents=True, exist_ok=True)
    dest.joinpath("agent.yaml").write_text(
        f"agent: {name}\nversion: '{version}'\nentry_point: manager\n")
    registry._write_meta(project, name, version, f"asset:{name}-{version}.tar.gz")
    return dest


# --- D-6: team_name / team_version --------------------------------------------

class TestTeamRefProperties:
    def test_bare_team_has_no_version(self):
        cfg = D.DeployConfig(name="x", team="eng-team")
        assert cfg.team_name == "eng-team"
        assert cfg.team_version is None

    def test_pinned_team_splits_on_last_at(self):
        cfg = D.DeployConfig(name="x", team="eng-team@1.1.0")
        assert cfg.team_name == "eng-team"
        assert cfg.team_version == "1.1.0"

    def test_no_team_is_empty(self):
        cfg = D.DeployConfig(name="x", team_url="https://r/t.tar.gz")
        assert cfg.team_name == ""
        assert cfg.team_version is None


# --- resolve_team_dir ---------------------------------------------------------

def test_pinned_fetches_into_shared_cache(project, monkeypatch):
    """An explicit @version resolves via registry.fetch(version=…) — the same
    shared cache install populates (D-3)."""
    calls = []

    def fake_fetch(pp, name, *, version=None, repo=None):
        calls.append((name, version))
        return _cached_team(pp, name, version)

    monkeypatch.setattr(registry, "fetch", fake_fetch)
    out = D.resolve_team_dir(project, "eng-team@1.1.0")

    assert calls == [("eng-team", "1.1.0")]
    assert out == registry.cache_path(project, "eng-team")


def test_pinned_reuses_cache_with_no_second_download(project, monkeypatch):
    """If the pinned version is already cached, resolution reuses it (§3.4) —
    registry.fetch is not called a second time."""
    _cached_team(project, "eng-team", "1.1.0")
    monkeypatch.setattr(registry, "fetch", lambda *a, **k: pytest.fail(
        "should not re-download an already-cached pin"))

    out = D.resolve_team_dir(project, "eng-team@1.1.0")
    assert out == registry.cache_path(project, "eng-team")


def test_bare_name_prefers_local_dir(project, monkeypatch):
    """A bare `team:` with a local dir uses it — today's behavior, no fetch."""
    pkg = _local_team(project, "eng-team")
    monkeypatch.setattr(registry, "fetch", lambda *a, **k: pytest.fail(
        "a local dir must win for a bare name"))

    out = D.resolve_team_dir(project, "eng-team")
    assert out == pkg.resolve()


def test_bare_name_no_local_fetches_latest(project, monkeypatch):
    """A bare `team:` with no local dir fetches latest into the cache."""
    calls = []

    def fake_fetch(pp, name, *, version=None, repo=None):
        calls.append((name, version))
        return _cached_team(pp, name, "1.1.0")

    monkeypatch.setattr(registry, "fetch", fake_fetch)
    out = D.resolve_team_dir(project, "eng-team")

    assert calls == [("eng-team", None)]
    assert out == registry.cache_path(project, "eng-team")


def test_team_as_filesystem_path_with_at_is_not_mis_split(project, monkeypatch):
    """A `team:` that is a literal path containing '@' must resolve to that dir,
    not be mis-parsed as name@version (regression: local_package_dir never split)."""
    weird = project / "checkout@v2" / "eng-team"
    weird.mkdir(parents=True)
    weird.joinpath("agent.yaml").write_text("agent: eng-team\nentry_point: manager\n")
    monkeypatch.setattr(registry, "fetch", lambda *a, **k: pytest.fail(
        "a literal team path must not trigger a registry fetch"))

    out = D.resolve_team_dir(project, str(weird))
    assert out == weird.resolve()


def test_explicit_pin_never_falls_back_to_local(project, monkeypatch):
    """A pin that fails to resolve is a hard error even when a local dir exists —
    a stale local checkout must never silently shadow a requested pin."""
    _local_team(project, "eng-team")  # present, but the pin must ignore it

    def boom(pp, name, *, version=None, repo=None):
        raise RuntimeError("no published asset at …/eng-team-9.9.9.tar.gz")

    monkeypatch.setattr(registry, "fetch", boom)
    with pytest.raises(D.DeployError) as exc:
        D.resolve_team_dir(project, "eng-team@9.9.9")
    assert "9.9.9" in str(exc.value)


def test_pin_failure_in_deps_helpers_is_not_swallowed(project, monkeypatch):
    """The deps-render / deps-hash helpers swallow DeployError for a bare name
    (legitimately "generic image"), but a PIN that fails to resolve must
    propagate — never silently produce a non-team-flavored image (F3)."""
    def boom(pp, name, *, version=None, repo=None):
        raise RuntimeError("no published asset")

    monkeypatch.setattr(registry, "fetch", boom)
    cfg = D.DeployConfig(name="x", team="eng-team@9.9.9")  # no local dir

    with pytest.raises(D.DeployError):
        D._render_team_deps_into_context(project, cfg, assets=None)
    with pytest.raises(D.DeployError):
        D._local_team_deps_hash(project, cfg)


def test_bare_name_no_local_no_registry_stays_generic(project, monkeypatch):
    """Contrast with the pin case: a BARE name that can't resolve is a generic
    image (None / "") — the swallow is still correct for the unpinned path."""
    monkeypatch.setattr(registry, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("not in any registry")))
    cfg = D.DeployConfig(name="x", team="eng-team")  # no local dir, bare name

    assert D._render_team_deps_into_context(project, cfg, assets=None) is None
    assert D._local_team_deps_hash(project, cfg) == ""


# --- call-site wiring: a pinned deploy ships the FETCHED dir (:996) -----------

def _stub_fly(monkeypatch):
    calls = []

    def fake_run(cmd, *, cwd=None, check=True, input_bytes=None, extra_env=None,
                 secret=False):
        calls.append({"cmd": cmd, "input": input_bytes})

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(D, "_run", fake_run)
    monkeypatch.setattr(D, "_fly_bin", lambda: "fly")
    monkeypatch.setattr(D, "fly_secrets_list", lambda app: set())
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    # Skip the Docker-Desktop probe (#387) — it shells out to `docker` which is
    # absent in CI sandboxes; on a real Linux host the socket short-circuits it.
    monkeypatch.setattr(D, "_resolve_local_build", lambda: (False, None))
    return calls


def test_pinned_deploy_pushes_the_fetched_package(project, monkeypatch):
    """End-to-end through deploy(): a pinned team with NO local dir resolves via
    registry.fetch at every call site, and the ssh-push (`:996`) tarballs the
    FETCHED dir — guards the 5th call site against shipping a stale/absent dir."""
    # minimal source root for resolve_assets
    (project / "scripts").mkdir()
    (project / "scripts" / "provision-instance.sh").write_text("#!/usr/bin/env bash\n")
    (project / "Dockerfile").write_text("FROM scratch\n")
    (project / "deployments").mkdir()
    (project / "deployments" / "eng.yaml").write_text("team: eng-team@1.1.0\n")

    fetched = _cached_team(project, "eng-team", "1.1.0")
    fetched.joinpath("agent.yaml").write_text(
        "agent: eng-team\nversion: '1.1.0'\nslack_token: ${SLACK_BOT_TOKEN}\n")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(registry, "fetch", lambda pp, name, *, version=None, repo=None:
                        registry.cache_path(pp, name))

    pushed = {}
    monkeypatch.setattr(D, "push_team",
                        lambda app, pkg, *, restart: pushed.update(pkg=pkg))
    _stub_fly(monkeypatch)

    cfg = D.deploy(project, "eng")
    assert cfg.delivery == "ssh-push"          # pinned team still ssh-push
    assert pushed["pkg"] == fetched            # the FETCHED dir is shipped
