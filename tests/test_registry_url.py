"""Tests for installing an agent team from a public .tar.gz URL.

`registry.fetch_from_url` is the seam the container first-boot and CI use to
inject a team without baking it into the image, so its archive handling
(nesting, naming, and traversal safety) is contract, not incidental.
"""

import io
import tarfile
from pathlib import Path

import pytest

from bobi import registry


def _make_team_dir(root: Path, name: str = "eng-team", *, agent: str | None = "eng-team") -> Path:
    """Write a minimal but valid team package under root/<name>."""
    team = root / name
    (team / "roles" / "manager").mkdir(parents=True)
    (team / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
    agent_line = f"agent: {agent}\n" if agent else ""
    team.joinpath("agent.yaml").write_text(
        f"version: '1.2.3'\n{agent_line}entry_point: manager\n"
        "event_server: ${BOBI_EVENT_SERVER}\n"
    )
    return team


def _targz(arcname_to_path: dict[str, Path]) -> bytes:
    """Build an in-memory .tar.gz from {arcname: source_path} pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, path in arcname_to_path.items():
            tar.add(path, arcname=arcname)
    return buf.getvalue()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("bobi.paths._root", tmp_path)
    return tmp_path


def _serve(monkeypatch, payload: bytes, *, status: int = 200):
    """Patch the pooled HTTP get so fetch_from_url reads `payload`."""
    import httpx

    request = httpx.Request("GET", "https://example.com/team.tar.gz")
    resp = httpx.Response(status, content=payload, request=request)
    monkeypatch.setattr("bobi.http.get", lambda url, **kw: resp)


def test_fetch_plain_team_tarball(project, tmp_path, monkeypatch):
    """A tarball of a single team dir installs under the project cache."""
    src = _make_team_dir(tmp_path / "src", "eng-team")
    payload = _targz({"eng-team": src,
                       "eng-team/agent.yaml": src / "agent.yaml",
                       "eng-team/roles/manager/ROLE.md": src / "roles/manager/ROLE.md"})
    _serve(monkeypatch, payload)

    dest, name = registry.fetch_from_url(project, "https://example.com/team.tar.gz")

    assert name == "eng-team"
    assert dest == registry._cache_dir(project) / "eng-team"
    assert (dest / "agent.yaml").is_file()
    assert (dest / "roles" / "manager" / "ROLE.md").read_text() == "# Manager\n"


def test_fetch_github_style_wrapper_prefix(project, tmp_path, monkeypatch):
    """GitHub codeload tarballs nest everything under one wrapper dir; the
    shallowest agent.yaml still identifies the team root."""
    src = _make_team_dir(tmp_path / "src", "eng-team")
    payload = _targz({
        "moda-team-abc123": src.parent,
        "moda-team-abc123/eng-team": src,
        "moda-team-abc123/eng-team/agent.yaml": src / "agent.yaml",
        "moda-team-abc123/eng-team/roles/manager/ROLE.md": src / "roles/manager/ROLE.md",
    })
    _serve(monkeypatch, payload)

    dest, name = registry.fetch_from_url(project, "https://example.com/team.tar.gz")

    assert name == "eng-team"
    assert (dest / "agent.yaml").is_file()
    assert (dest / "roles" / "manager" / "ROLE.md").is_file()


def test_name_falls_back_to_dir_when_no_agent_field(project, tmp_path, monkeypatch):
    src = _make_team_dir(tmp_path / "src", "support-team", agent=None)
    payload = _targz({"support-team": src,
                      "support-team/agent.yaml": src / "agent.yaml"})
    _serve(monkeypatch, payload)

    _dest, name = registry.fetch_from_url(project, "https://example.com/t.tar.gz")
    assert name == "support-team"


def test_explicit_name_overrides(project, tmp_path, monkeypatch):
    src = _make_team_dir(tmp_path / "src", "eng-team")
    payload = _targz({"eng-team": src, "eng-team/agent.yaml": src / "agent.yaml"})
    _serve(monkeypatch, payload)

    _dest, name = registry.fetch_from_url(project, "https://x/t.tar.gz", name="renamed")
    assert name == "renamed"
    assert (registry._cache_dir(project) / "renamed" / "agent.yaml").is_file()


def test_meta_records_url_source(project, tmp_path, monkeypatch):
    src = _make_team_dir(tmp_path / "src", "eng-team")
    payload = _targz({"eng-team": src, "eng-team/agent.yaml": src / "agent.yaml"})
    _serve(monkeypatch, payload)

    registry.fetch_from_url(project, "https://example.com/eng-team.tar.gz")
    meta = registry._read_meta(project, "eng-team")
    assert meta["source"] == "url:https://example.com/eng-team.tar.gz"
    assert meta["version"] == "1.2.3"


def test_reinstall_replaces_existing(project, tmp_path, monkeypatch):
    src = _make_team_dir(tmp_path / "src", "eng-team")
    payload = _targz({"eng-team": src,
                      "eng-team/agent.yaml": src / "agent.yaml",
                      "eng-team/roles/manager/ROLE.md": src / "roles/manager/ROLE.md"})
    _serve(monkeypatch, payload)
    dest, _ = registry.fetch_from_url(project, "https://x/t.tar.gz")
    stray = dest / "roles" / "manager" / "STALE.md"
    stray.write_text("remove me\n")

    registry.fetch_from_url(project, "https://x/t.tar.gz")
    assert not stray.exists()  # old tree wiped before re-extract


def test_non_gzip_payload_is_rejected(project, monkeypatch):
    _serve(monkeypatch, b"this is not a tarball")
    with pytest.raises(RuntimeError, match="readable .tar.gz"):
        registry.fetch_from_url(project, "https://x/t.tar.gz")


def test_archive_without_agent_yaml_is_rejected(project, tmp_path, monkeypatch):
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "readme.txt").write_text("nope\n")
    payload = _targz({"junk/readme.txt": tmp_path / "junk" / "readme.txt"})
    _serve(monkeypatch, payload)
    with pytest.raises(RuntimeError, match="No agent.yaml"):
        registry.fetch_from_url(project, "https://x/t.tar.gz")


def test_http_error_is_wrapped(project, monkeypatch):
    import httpx

    def _raise(url, **kw):
        req = httpx.Request("GET", url)
        return httpx.Response(404, request=req)

    monkeypatch.setattr("bobi.http.get", _raise)
    with pytest.raises(RuntimeError, match="Failed to fetch"):
        registry.fetch_from_url(project, "https://x/missing.tar.gz")


def test_cli_install_detects_url(project, tmp_path, monkeypatch):
    """`bobi agents install <url> --name` routes to fetch_from_url and installs
    the package into the selected Bobi Agent runtime."""
    from click.testing import CliRunner

    from bobi.cli import main

    src = _make_team_dir(tmp_path / "src", "eng-team")
    src.joinpath("agent.yaml").write_text(
        "version: '2.0'\nagent: eng-team\nentry_point: manager\n"
        "event_server: ${BOBI_EVENT_SERVER}\n"
    )
    payload = _targz({"eng-team": src,
                      "eng-team/agent.yaml": src / "agent.yaml",
                      "eng-team/roles/manager/ROLE.md": src / "roles/manager/ROLE.md"})
    _serve(monkeypatch, payload)

    monkeypatch.chdir(project)
    monkeypatch.setenv("BOBI_EVENT_SERVER", "https://events.example.com")

    result = CliRunner().invoke(
        main,
        ["agents", "install", "https://example.com/eng-team.tar.gz",
         "--name", "eng", "--non-interactive"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "URL" in result.output  # took the URL branch, not registry-by-name
    installed = Path(project / "home" / "agents" / "eng" / "run" / "package")
    assert (installed / "agent.yaml").is_file()
    cfg = (installed / "agent.yaml").read_text()
    assert "entry_point: manager" in cfg


def test_path_traversal_member_is_rejected(project, tmp_path, monkeypatch):
    """A malicious archive with a `..` escape must not be extracted."""
    src = _make_team_dir(tmp_path / "src", "eng-team")
    evil = tmp_path / "evil.txt"
    evil.write_text("pwned\n")
    payload = _targz({
        "eng-team": src,
        "eng-team/agent.yaml": src / "agent.yaml",
        "eng-team/../../escape.txt": evil,
    })
    _serve(monkeypatch, payload)
    with pytest.raises(RuntimeError, match="unsafe path"):
        registry.fetch_from_url(project, "https://x/t.tar.gz")


# --- fetch_from_archive: the on-disk twin (ssh-push delivery seam) -----------

def test_fetch_from_archive_installs_local_tarball(project, tmp_path):
    """A local .tar.gz installs identically to a URL — the path ssh-push uses
    after pushing a built team tarball onto an instance's volume."""
    src = _make_team_dir(tmp_path / "src", "eng-team", agent="eng-team")
    arc = tmp_path / "eng-team.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        t.add(src, arcname="eng-team")

    dest, name = registry.fetch_from_archive(project, arc)
    assert name == "eng-team"
    assert (dest / "agent.yaml").is_file()
    assert (dest / "roles" / "manager" / "ROLE.md").is_file()


def test_fetch_from_archive_rejects_non_archive(project, tmp_path):
    bad = tmp_path / "nope.tar.gz"
    bad.write_text("not a tarball")
    with pytest.raises(RuntimeError, match="not a readable .tar.gz"):
        registry.fetch_from_archive(project, bad)


def test_install_cli_routes_local_archive(project, tmp_path, monkeypatch):
    """`bobi agents install ./team.tar.gz --name` takes the local-archive branch."""
    from click.testing import CliRunner
    from bobi.cli import main

    src = _make_team_dir(tmp_path / "src", "eng-team", agent="eng-team")
    arc = tmp_path / "eng-team.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        t.add(src, arcname="eng-team")
    monkeypatch.chdir(project)
    monkeypatch.setenv("BOBI_EVENT_SERVER", "https://ev.example.workers.dev")

    result = CliRunner().invoke(
        main,
        ["agents", "install", str(arc), "--name", "eng", "--non-interactive"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "local archive" in result.output
    assert (project / "home" / "agents" / "eng" / "run" / "package" / "agent.yaml").is_file()
