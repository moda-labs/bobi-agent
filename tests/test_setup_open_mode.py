"""Unit tests for modastack.setup.open_mode team listing."""

from modastack import registry
from modastack.setup import open_mode


def _make_team(d, name, desc):
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(f"agent: {name}\nentry_point: lead\n")
    (d / "agent.md").write_text(f"{desc}\n")


def test_list_registry_teams_flags_official(monkeypatch, tmp_path):
    # Remote teams from the canonical modastack registry are "official"; teams
    # from a user-added registry, and cached teams, are not. Isolate from the
    # bundled starter templates so the assertions are about the registry path.
    monkeypatch.setattr(open_mode, "list_bundled_templates", lambda: [])
    monkeypatch.setattr(registry, "list_remote", lambda project: [
        {"name": "eng-team-core", "description": "Portable eng org",
         "registry": registry.DEFAULT_REPO},
        {"name": "third-party", "description": "From elsewhere",
         "registry": "someone/their-repo"},
    ])
    monkeypatch.setattr(registry, "list_cached", lambda project: [
        {"name": "cached-team", "description": "Already pulled"},
    ])

    teams = {t["name"]: t for t in open_mode.list_registry_teams(tmp_path)}

    assert teams["eng-team-core"]["official"] is True
    assert teams["third-party"]["official"] is False
    assert teams["cached-team"]["official"] is False
    # The flag is always present so the UI never reads `undefined`.
    assert all("official" in t for t in teams.values())


def test_bundled_templates_listed_and_copied_offline(monkeypatch, tmp_path):
    # Starter templates ship with modastack and must surface (tagged official +
    # bundled) and copy from local disk — no registry, no network.
    bundle = tmp_path / "templates"
    _make_team(bundle / "alpha-team", "alpha-team", "Does alpha things.")
    _make_team(bundle / "beta-team", "beta-team", "Does beta things.")
    monkeypatch.setattr(open_mode, "_bundled_templates_dir", lambda: bundle)

    listed = {t["name"]: t for t in open_mode.list_bundled_templates()}
    assert set(listed) == {"alpha-team", "beta-team"}
    assert listed["alpha-team"]["official"] is True
    assert listed["alpha-team"]["bundled"] is True
    assert listed["alpha-team"]["description"] == "Does alpha things."

    # They flow into the intro's template list...
    monkeypatch.setattr(registry, "list_remote", lambda project: [])
    monkeypatch.setattr(registry, "list_cached", lambda project: [])
    names = {t["name"] for t in open_mode.list_registry_teams(tmp_path)}
    assert {"alpha-team", "beta-team"} <= names

    # ...and selecting one copies locally instead of hitting the network.
    def _boom(*a, **k):
        raise AssertionError("bundled template must not fetch from a registry")
    monkeypatch.setattr(registry, "fetch", _boom)
    dest = tmp_path / "work" / "alpha-team"
    open_mode.fetch_into(tmp_path, "alpha-team", dest)
    assert (dest / "agent.yaml").is_file()
    assert open_mode.is_team(dest)
