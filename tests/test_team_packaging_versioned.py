"""Phase 1 of #440 — versioned, immutable per-team packaging.

Guards the publishing half of registry-based `name@version`:

  - `build-team-tarballs.sh` ALSO emits an immutable `<team>-<version>.tar.gz`
    (byte-identical to the rolling `<team>.tar.gz`), reading the version via the
    `team-version.py` helper; a version-less team gets only the rolling tarball
    plus a warning (D-5).
  - `publish-team-tarballs.sh` uploads the rolling tarball with `--clobber` and
    the versioned tarball WITHOUT `--clobber`, treating an "already exists" (422)
    as a no-op skip — immutability is a property of the upload, not a TOCTOU
    `gh release view` window.
  - `check-team-versions.py` fails when a team's agent.yaml `version:` disagrees
    with its `registry.yaml` entry (D-4: the agreement check lives here, NOT in
    tests/test_packaging.py, to avoid colliding with the open #438).
  - The real `agents/registry.yaml` agrees with every team's agent.yaml (Phase 1
    Step-0 reconciliation: eng-team & market-research bumped 1.0.0 -> 1.1.0).

This is a NEW test module (not tests/test_packaging.py) on purpose — see D-4 /
spec §7 #438 collision avoidance.
"""
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BUILD_SH = REPO / "scripts" / "build-team-tarballs.sh"
PUBLISH_SH = REPO / "scripts" / "publish-team-tarballs.sh"
TEAM_VERSION_PY = REPO / "scripts" / "team-version.py"
CHECK_VERSIONS_PY = REPO / "scripts" / "check-team-versions.py"
WF_TEAMS = REPO / ".github" / "workflows" / "team-packages.yml"


# --- fixtures ---------------------------------------------------------------

def _make_team(root: Path, name: str, version: str | None) -> Path:
    """A minimal source-layout team dir (<dir>/agent.yaml)."""
    d = root / name
    d.mkdir(parents=True)
    lines = []
    if version is not None:
        lines.append(f'version: "{version}"')
    lines += ["agent: demo", "entry_point: manager"]
    (d / "agent.yaml").write_text("\n".join(lines) + "\n")
    return d


def _build(team_dir: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BUILD_SH), "--out", str(out), str(team_dir)],
        capture_output=True, text=True,
    )


# --- build-team-tarballs.sh: versioned + rolling ----------------------------

def test_versioned_team_produces_both_byte_identical(tmp_path):
    team = _make_team(tmp_path / "src", "demo-team", "2.3.4")
    out = tmp_path / "dist"
    proc = _build(team, out)
    assert proc.returncode == 0, proc.stderr

    rolling = out / "demo-team.tar.gz"
    versioned = out / "demo-team-2.3.4.tar.gz"
    assert rolling.exists(), proc.stdout + proc.stderr
    assert versioned.exists(), "versioned immutable asset must be produced"
    # Immutable copy is byte-identical to the rolling tarball at publish time.
    assert filecmp.cmp(rolling, versioned, shallow=False)


def test_versionless_team_rolling_only_with_warning(tmp_path):
    team = _make_team(tmp_path / "src", "smoke-ish", None)
    out = tmp_path / "dist"
    proc = _build(team, out)
    assert proc.returncode == 0, proc.stderr

    assert (out / "smoke-ish.tar.gz").exists()
    versioned = list(out.glob("smoke-ish-*.tar.gz"))
    assert versioned == [], f"version-less team must not get a pinned asset: {versioned}"
    assert "warning" in proc.stderr.lower() and "version" in proc.stderr.lower()


def test_build_prerelease_version_is_rolling_only(tmp_path):
    """Regression for the misclassification footgun: a non-semver version must
    NOT produce a versioned asset (which the publisher would clobber as if
    rolling). The team still ships its rolling tarball."""
    team = _make_team(tmp_path / "src", "demo-team", "1.2.0-rc1")
    out = tmp_path / "dist"
    proc = _build(team, out)
    assert proc.returncode == 0, proc.stderr
    assert (out / "demo-team.tar.gz").exists()
    assert list(out.glob("demo-team-*.tar.gz")) == []


def test_build_survives_one_bad_team(tmp_path):
    """One team with malformed agent.yaml must not abort a multi-team build."""
    src = tmp_path / "src"
    good = _make_team(src, "good-team", "1.0.0")
    bad = src / "bad-team"
    bad.mkdir()
    (bad / "agent.yaml").write_text("version: [unterminated")
    out = tmp_path / "dist"
    proc = subprocess.run(
        ["bash", str(BUILD_SH), "--out", str(out), str(good), str(bad)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Healthy team got both tarballs; bad team got rolling only (no crash).
    assert (out / "good-team.tar.gz").exists()
    assert (out / "good-team-1.0.0.tar.gz").exists()
    assert (out / "bad-team.tar.gz").exists()
    assert list(out.glob("bad-team-*.tar.gz")) == []


# --- team-version.py helper -------------------------------------------------

def _team_version(team_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TEAM_VERSION_PY), str(team_dir)],
        capture_output=True, text=True,
    )


def test_team_version_prints_version(tmp_path):
    team = _make_team(tmp_path, "t", "9.9.9")
    proc = _team_version(team)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "9.9.9"


def test_team_version_silent_when_absent(tmp_path):
    team = _make_team(tmp_path, "t", None)
    proc = _team_version(team)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize("bad", ["1.2.0-rc1", "1.0", "1", "1.0/0", "v1.0.0", "latest"])
def test_team_version_rejects_non_semver(tmp_path, bad):
    """Only strict X.Y.Z is pinnable. A non-semver version must NOT become an
    asset filename (it would be misclassified as rolling by the publisher and
    silently clobbered, losing immutability) — degrade to rolling-only + warn."""
    team = _make_team(tmp_path, "t", bad)
    proc = _team_version(team)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"{bad!r} must not be emitted as a pin"
    assert "version" in proc.stderr.lower()


def test_team_version_clean_error_on_bad_yaml(tmp_path):
    team = tmp_path / "broken"
    team.mkdir()
    (team / "agent.yaml").write_text('version: "1.0.0\nagent: [oops')  # invalid YAML
    proc = _team_version(team)
    # Malformed YAML must not crash with a traceback; degrade to rolling-only.
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert "yaml" in proc.stderr.lower() or "parse" in proc.stderr.lower()


# --- check-team-versions.py: registry.yaml vs agent.yaml --------------------

def _agents_fixture(root: Path, registry: dict, teams: dict) -> Path:
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "registry.yaml").write_text(yaml.safe_dump(registry))
    for name, version in teams.items():
        _make_team(agents, name, version)
    return agents


def _check(agents_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECK_VERSIONS_PY), str(agents_dir)],
        capture_output=True, text=True,
    )


def test_check_passes_on_agreement(tmp_path):
    agents = _agents_fixture(
        tmp_path,
        {"agents": {"alpha": {"version": "1.2.0"}, "beta": {"version": "0.1.0"}}},
        {"alpha": "1.2.0", "beta": "0.1.0"},
    )
    proc = _check(agents)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_check_fails_on_drift(tmp_path):
    agents = _agents_fixture(
        tmp_path,
        {"agents": {"alpha": {"version": "1.2.0"}}},
        {"alpha": "1.3.0"},
    )
    proc = _check(agents)
    assert proc.returncode == 1
    assert "alpha" in proc.stderr
    assert "1.2.0" in proc.stderr and "1.3.0" in proc.stderr


def test_check_fails_on_non_semver_registry_version(tmp_path):
    """A pinned 'latest' pointer must be strict X.Y.Z so a published asset can
    exist for it. A prerelease/partial version fails loudly, not silently."""
    agents = _agents_fixture(
        tmp_path,
        {"agents": {"alpha": {"version": "1.2.0-rc1"}}},
        {"alpha": "1.2.0-rc1"},
    )
    proc = _check(agents)
    assert proc.returncode == 1
    assert "alpha" in proc.stderr


def test_check_clean_error_on_bad_agent_yaml(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "registry.yaml").write_text(
        yaml.safe_dump({"agents": {"alpha": {"version": "1.0.0"}}}))
    bad = agents / "alpha"
    bad.mkdir()
    (bad / "agent.yaml").write_text("version: [unterminated")
    proc = _check(agents)
    assert proc.returncode == 1
    assert "alpha" in proc.stderr


def test_check_fails_when_pinned_team_has_no_version(tmp_path):
    agents = _agents_fixture(
        tmp_path,
        {"agents": {"alpha": {"version": "1.2.0"}}},
        {"alpha": None},
    )
    proc = _check(agents)
    assert proc.returncode == 1
    assert "alpha" in proc.stderr


def test_real_registry_agrees_with_agent_yamls():
    """Step-0 reconciliation guard: the live agents/ must pass the agreement
    check (eng-team & market-research bumped to 1.1.0). Mirrors the CI step
    locally without touching tests/test_packaging.py (#438 collision)."""
    proc = _check(REPO / "agents")
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- publish-team-tarballs.sh: rolling clobbers, versioned is immutable ------

def _fake_gh(bin_dir: Path, already_published: str = "") -> Path:
    """A `gh` shim that records `release upload` calls and simulates an
    immutable 422 for any asset basename listed in $ALREADY_PUBLISHED."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'log="$GH_CALL_LOG"\n'
        '# Args: release upload <tag> <file> [--clobber]\n'
        'if [ "$1" = "release" ] && [ "$2" = "upload" ]; then\n'
        '  file="$4"; base="$(basename "$file")"; clobber=no\n'
        '  for a in "$@"; do [ "$a" = "--clobber" ] && clobber=yes; done\n'
        '  echo "$base clobber=$clobber" >> "$log"\n'
        '  if [ "$clobber" = no ]; then\n'
        '    for p in $ALREADY_PUBLISHED; do\n'
        '      if [ "$p" = "$base" ]; then\n'
        '        echo "HTTP 422: Validation Failed (already_exists)" >&2\n'
        '        exit 1\n'
        '      fi\n'
        '    done\n'
        '  fi\n'
        '  exit 0\n'
        "fi\n"
        'exit 0\n'
    )
    gh.chmod(0o755)
    return gh


def _publish(dist: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(PUBLISH_SH), "teams-latest", str(dist)],
        capture_output=True, text=True, env=env,
    )


def _seed_dist(tmp_path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    for n in ("eng-team.tar.gz", "eng-team-1.1.0.tar.gz", "smoke.tar.gz"):
        (dist / n).write_bytes(b"x")
    return dist


def test_publish_rolling_clobbers_versioned_does_not(tmp_path, monkeypatch):
    import os
    dist = _seed_dist(tmp_path)
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "GH_CALL_LOG": str(log), "ALREADY_PUBLISHED": ""}
    proc = _publish(dist, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    calls = log.read_text().splitlines()
    # rolling assets clobber; the versioned immutable asset never clobbers.
    assert "eng-team.tar.gz clobber=yes" in calls
    assert "smoke.tar.gz clobber=yes" in calls
    assert "eng-team-1.1.0.tar.gz clobber=no" in calls


def test_publish_skips_already_published_versioned_asset(tmp_path):
    import os
    dist = _seed_dist(tmp_path)
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "GH_CALL_LOG": str(log),
           "ALREADY_PUBLISHED": "eng-team-1.1.0.tar.gz"}
    proc = _publish(dist, env)
    # A pre-existing immutable asset is a NO-OP success, not a failure.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skip: eng-team-1.1.0.tar.gz already published (immutable)" in proc.stdout


def test_publish_real_upload_error_is_fatal(tmp_path):
    """A non-422 failure on a versioned asset must abort (not be swallowed as
    an immutable skip)."""
    import os
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "eng-team-1.1.0.tar.gz").write_bytes(b"x")
    bindir = tmp_path / "bin"
    gh = bindir / "gh"
    bindir.mkdir()
    gh.write_text("#!/usr/bin/env bash\necho 'HTTP 500: server error' >&2\nexit 1\n")
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "GH_CALL_LOG": str(tmp_path / 'l.log'), "ALREADY_PUBLISHED": ""}
    proc = _publish(dist, env)
    assert proc.returncode != 0


# --- workflow wiring --------------------------------------------------------

def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _job_scripts(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


def test_workflow_build_runs_version_agreement_check():
    jobs = _load(WF_TEAMS)["jobs"]
    build = "\n".join(_job_scripts(j) for j in jobs.values()
                      if "build" in (j.get("name", "").lower() + " "))
    assert "check-team-versions.py" in "\n".join(
        _job_scripts(j) for j in jobs.values())


def test_workflow_publish_uses_immutable_publish_script():
    jobs = _load(WF_TEAMS)["jobs"]
    publish = _job_scripts(jobs["publish"])
    assert "publish-team-tarballs.sh" in publish


@pytest.mark.skipif(shutil.which("shellcheck") is None,
                    reason="shellcheck not installed")
def test_new_shell_scripts_pass_shellcheck():
    for script in (PUBLISH_SH,):
        sc = subprocess.run(["shellcheck", str(script)],
                            capture_output=True, text=True)
        assert sc.returncode == 0, f"{script.name}:\n{sc.stdout}{sc.stderr}"


def test_build_rejects_path_based_from(tmp_path):
    """Packaging must hard-fail on a path-based `from:` (#446 §7.1) — a path
    override is local-only and would arrive broken at a consumer."""
    src = tmp_path / "src"
    team = _make_team(src, "overlay-team", "1.0.0")
    # Re-write agent.yaml to declare a path-based `from:`.
    (team / "agent.yaml").write_text(
        'from: ../eng-team\nversion: "1.0.0"\nagent: demo\n')
    out = tmp_path / "dist"
    proc = _build(team, out)
    assert proc.returncode != 0
    assert "path override" in proc.stderr
    assert not (out / "overlay-team.tar.gz").exists()


def test_build_allows_name_based_from(tmp_path):
    """A `name@version` `from:` is publishable and packages normally."""
    src = tmp_path / "src"
    team = _make_team(src, "overlay-team", "1.0.0")
    (team / "agent.yaml").write_text(
        'from: eng-team@1.0.0\nversion: "1.0.0"\nagent: demo\n')
    out = tmp_path / "dist"
    proc = _build(team, out)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (out / "overlay-team.tar.gz").exists()
