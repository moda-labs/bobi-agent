"""Tests for `from:` team inheritance compose (#446 resolution + #451 merge).

Covers the acceptance criteria of both specs:
  docs/specs/team-from-resolution.md  — resolution order, fail-fast, --pinned,
                                         cycle/depth, path refs, publish guard.
  docs/specs/team-compose-merge.md    — prose concat + replace, structured
                                         deep-merge, build accrete, prune,
                                         determinism, provenance, workspace.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from bobi import compose, paths, registry


# --- fixtures ----------------------------------------------------------------


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _team(root: Path, name: str, agent_yaml: str, *, agent_md: str | None = None,
          roles: dict[str, str] | None = None, tools: dict[str, str] | None = None,
          monitors: str | None = None, workspace: dict[str, str] | None = None) -> Path:
    """Create a team source dir under root/agents/<name>."""
    d = root / "agents" / name
    _write(d / "agent.yaml", agent_yaml)
    if agent_md is not None:
        _write(d / "agent.md", agent_md)
    for role, body in (roles or {}).items():
        _write(d / "roles" / role / "ROLE.md", body)
    for fn, body in (tools or {}).items():
        _write(d / "tools" / fn, body)
    if monitors is not None:
        _write(d / "monitors" / "defaults.yaml", monitors)
    for fn, body in (workspace or {}).items():
        _write(d / "workspace" / fn, body)
    return d


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    (tmp_path / "agents").mkdir(parents=True)
    return tmp_path


# --- resolution (#446) -------------------------------------------------------


def test_no_from_single_layer_chain(project):
    leaf = _team(project, "solo", 'version: "1.0.0"\nentry_point: director\n')
    chain = compose.resolve_chain(leaf, project)
    assert [l.dir.name for l in chain] == ["solo"]
    assert chain[0].source == "leaf"


def test_local_source_always_wins(project):
    _team(project, "core", 'version: "1.0.0"\nentry_point: director\n')
    leaf = _team(project, "moda", 'from: core@1.0.0\nversion: "2.0.0"\n')
    chain = compose.resolve_chain(leaf, project)
    assert [l.dir.name for l in chain] == ["core", "moda"]
    assert chain[0].source == "local-source"
    assert chain[0].version == "1.0.0"


def test_latest_ref_uses_local_regardless_of_version(project):
    _team(project, "core", 'version: "9.9.9"\nentry_point: director\n')
    leaf = _team(project, "moda", "from: core\nversion: \"2.0.0\"\n")
    chain = compose.resolve_chain(leaf, project)
    assert chain[0].version == "9.9.9"  # latest = local wins as-is


def test_pin_mismatch_fails_fast(project):
    _team(project, "core", 'version: "1.1.0"\nentry_point: director\n')
    leaf = _team(project, "moda", 'from: core@1.2.0\nversion: "2.0.0"\n')
    with pytest.raises(compose.ComposeError) as exc:
        compose.resolve_chain(leaf, project)
    msg = str(exc.value)
    assert "1.1.0" in msg and "1.2.0" in msg
    assert "moda" in msg          # names the referrer
    assert "path ref" in msg      # offers the three ways out
    assert "bump" in msg


def test_pin_match_succeeds(project):
    _team(project, "core", 'version: "1.2.0"\nentry_point: director\n')
    leaf = _team(project, "moda", 'from: core@1.2.0\nversion: "2.0.0"\n')
    chain = compose.resolve_chain(leaf, project)
    assert chain[0].version == "1.2.0"


def test_path_ref_resolves_relative_to_referrer(tmp_path):
    # core lives outside the project's agents/ dir; the path ref reaches it.
    proj = tmp_path / "proj"
    (proj / "agents").mkdir(parents=True)
    _team(tmp_path, "core", 'version: "1.0.0"\nentry_point: director\n')
    # leaf (proj/agents/moda) references core via a path relative to its own dir.
    leaf = _team(proj, "moda", "from: ../../../agents/core\nversion: \"2.0.0\"\n")
    chain = compose.resolve_chain(leaf, proj)
    assert chain[0].source == "path"
    assert chain[0].dir == (tmp_path / "agents" / "core").resolve()


def test_path_ref_rejected_under_pinned(project):
    leaf = _team(project, "moda", "from: ../core\nversion: \"2.0.0\"\n")
    with pytest.raises(compose.ComposeError) as exc:
        compose.resolve_chain(leaf, project, pinned=True)
    assert "pinned" in str(exc.value).lower()


def test_cycle_detected(project):
    _team(project, "a", "from: b\nversion: \"1.0.0\"\n")
    leaf = _team(project, "b", "from: a\nversion: \"1.0.0\"\n")
    with pytest.raises(compose.ComposeError) as exc:
        compose.resolve_chain(leaf, project)
    assert "cycle" in str(exc.value)


def test_cache_used_when_no_local_source(project, monkeypatch):
    # No agents/core source; a cached copy exists.
    cache = registry.cache_path(project, "core")
    _write(cache / "agent.yaml", 'version: "1.5.0"\nentry_point: director\n')
    leaf = _team(project, "moda", 'from: core@1.5.0\nversion: "2.0.0"\n')
    chain = compose.resolve_chain(leaf, project)
    assert chain[0].source == "cache"
    assert chain[0].version == "1.5.0"


def test_registry_fetch_when_nothing_local(project, monkeypatch):
    fetched_dir = project / "fetched-core"
    _write(fetched_dir / "agent.yaml", 'version: "3.0.0"\nentry_point: director\n')
    calls = {}

    def fake_fetch(proj, name, *, version=None, repo=None):
        calls["name"], calls["version"] = name, version
        return fetched_dir

    monkeypatch.setattr(registry, "fetch", fake_fetch)
    monkeypatch.setattr(registry, "is_cached", lambda p, n: False)
    leaf = _team(project, "moda", 'from: core@3.0.0\nversion: "2.0.0"\n')
    chain = compose.resolve_chain(leaf, project)
    assert chain[0].source == "registry"
    assert calls == {"name": "core", "version": "3.0.0"}


def test_pinned_skips_local_source(project, monkeypatch):
    # Even with a local sibling present, --pinned goes to the registry.
    _team(project, "core", 'version: "1.0.0"\nentry_point: director\n')
    fetched = project / "fetched"
    _write(fetched / "agent.yaml", 'version: "1.0.0"\nentry_point: director\n')
    seen = {}

    def fake_fetch(proj, name, *, version=None, repo=None):
        seen["called"] = True
        return fetched

    monkeypatch.setattr(registry, "fetch", fake_fetch)
    leaf = _team(project, "moda", 'from: core@1.0.0\nversion: "2.0.0"\n')
    chain = compose.resolve_chain(leaf, project, pinned=True)
    assert seen.get("called") and chain[0].source == "registry"


def test_pinned_lock_pins_latest_ref(project, monkeypatch):
    fetched = project / "fetched"
    _write(fetched / "agent.yaml", 'version: "1.0.0"\nentry_point: director\n')
    seen = {}

    def fake_fetch(proj, name, *, version=None, repo=None):
        seen["version"] = version
        return fetched

    monkeypatch.setattr(registry, "fetch", fake_fetch)
    leaf = _team(project, "moda", "from: core\nversion: \"2.0.0\"\n")
    compose.resolve_chain(leaf, project, pinned=True, locked={"core": "1.4.2"})
    assert seen["version"] == "1.4.2"  # latest ref locked to recorded version


# --- prose merge (#451 §2) ---------------------------------------------------


def _compose(project, leaf, dest=None):
    chain = compose.resolve_chain(leaf, project)
    dest = dest or paths.package_dir(project)
    prov = compose.compose(chain, dest)
    return dest, prov


def test_prose_concatenates_in_chain_order(project):
    _team(project, "core", 'version: "1.0.0"\n', agent_md="# Core\nbase intro",
          roles={"engineer": "# Engineer\nrun your review gate"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n',
                 agent_md="moda house", roles={"engineer": "use /review"})
    dest, _ = _compose(project, leaf)
    assert (dest / "agent.md").read_text() == "# Core\nbase intro\n\nmoda house\n"
    role = (dest / "roles" / "engineer" / "ROLE.md").read_text()
    assert role == "# Engineer\nrun your review gate\n\nuse /review\n"


def test_replace_frontmatter_drops_base(project):
    _team(project, "core", 'version: "1.0.0"\n',
          roles={"engineer": "# Engineer\nbase craft"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n',
                 roles={"engineer": "---\nreplace: true\n---\n# Engineer\nfull override"})
    dest, _ = _compose(project, leaf)
    role = (dest / "roles" / "engineer" / "ROLE.md").read_text()
    assert "base craft" not in role
    assert role == "# Engineer\nfull override\n"


def test_overlay_only_role_appears(project):
    _team(project, "core", 'version: "1.0.0"\n', roles={"director": "# Director"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n',
                 roles={"engineer": "# Engineer"})
    dest, _ = _compose(project, leaf)
    assert (dest / "roles" / "director" / "ROLE.md").exists()
    assert (dest / "roles" / "engineer" / "ROLE.md").exists()


# --- structured merge (#451 §3) ----------------------------------------------


def test_tools_merge_by_filename(project):
    _team(project, "core", 'version: "1.0.0"\n',
          tools={"github.md": "gh", "shared.md": "core-shared"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n',
                 tools={"linear.md": "linear", "shared.md": "overlay-shared"})
    dest, _ = _compose(project, leaf)
    names = sorted(p.name for p in (dest / "tools").iterdir())
    assert names == ["github.md", "linear.md", "shared.md"]
    assert (dest / "tools" / "shared.md").read_text() == "overlay-shared"  # leaf wins


def test_services_merge_by_name_and_remove(project):
    _team(project, "core", 'version: "1.0.0"\nservices:\n'
          '  - {name: github, required: true}\n'
          '  - {name: slack, required: true}\n')
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\nservices:\n'
                 '  - {name: linear, required: true}\n'
                 '  - {name: github, credentials: {token: X}}\n'
                 '  - {name: slack, remove: true}\n')
    dest, _ = _compose(project, leaf)
    cfg = yaml.safe_load((dest / "agent.yaml").read_text())
    svcs = {s["name"]: s for s in cfg["services"]}
    assert set(svcs) == {"github", "linear"}            # slack removed
    assert svcs["github"]["required"] is True           # base field kept
    assert svcs["github"]["credentials"] == {"token": "X"}  # overlay field added


def test_build_lists_accrete_scalars_override(project):
    _team(project, "core", 'version: "1.0.0"\nbuild:\n'
          '  apt: [nodejs, jq]\n  verify: requires\n')
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\nbuild:\n'
                 '  apt: [jq, curl]\n  npm: [codex]\n')
    dest, _ = _compose(project, leaf)
    build = yaml.safe_load((dest / "agent.yaml").read_text())["build"]
    assert build["apt"] == ["nodejs", "jq", "curl"]  # appended + de-duped
    assert build["npm"] == ["codex"]
    assert build["verify"] == "requires"             # base scalar survives


def test_auto_dispatch_append_and_id_replace(project):
    _team(project, "core", 'version: "1.0.0"\nauto_dispatch:\n'
          '  - {id: a, event: x, workflow: w1}\n'
          '  - {event: y, workflow: w2}\n')
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\nauto_dispatch:\n'
                 '  - {id: a, event: x, workflow: OVERRIDDEN}\n'
                 '  - {event: z, workflow: w3}\n')
    dest, _ = _compose(project, leaf)
    rules = yaml.safe_load((dest / "agent.yaml").read_text())["auto_dispatch"]
    assert rules[0] == {"id": "a", "event": "x", "workflow": "OVERRIDDEN"}
    assert [r.get("workflow") for r in rules] == ["OVERRIDDEN", "w2", "w3"]


def test_monitors_deep_merge_by_name(project):
    _team(project, "core", 'version: "1.0.0"\n',
          monitors="monitors:\n  - {name: stale-pr, interval: 1h, enabled: true}\n")
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n',
                 monitors="monitors:\n  - {name: stale-pr, enabled: false}\n"
                          "  - {name: new-mon, interval: 5m}\n")
    dest, _ = _compose(project, leaf)
    mons = {m["name"]: m for m in
            yaml.safe_load((dest / "monitors" / "defaults.yaml").read_text())["monitors"]}
    assert mons["stale-pr"]["interval"] == "1h"     # base field kept
    assert mons["stale-pr"]["enabled"] is False     # overlay flipped
    assert "new-mon" in mons


def test_from_not_emitted(project):
    _team(project, "core", 'version: "1.0.0"\nentry_point: director\n')
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n')
    dest, _ = _compose(project, leaf)
    cfg = yaml.safe_load((dest / "agent.yaml").read_text())
    assert "from" not in cfg
    assert cfg["agent"] == "moda"
    assert cfg["version"] == "2.0.0"


# --- prune (#451 §4) ---------------------------------------------------------


def test_prune_drops_inherited(project):
    _team(project, "core", 'version: "1.0.0"\n',
          tools={"codex.md": "x", "github.md": "gh"},
          roles={"director": "# Director", "spare": "# Spare"},
          monitors="monitors:\n  - {name: keep, interval: 1h}\n"
                   "  - {name: drop-me, interval: 5m}\n")
    leaf = _team(project, "moda",
                 'from: core\nversion: "2.0.0"\n'
                 'prune:\n  tools: [codex]\n  roles: [spare]\n  monitors: [drop-me]\n')
    dest, _ = _compose(project, leaf)
    assert not (dest / "tools" / "codex.md").exists()
    assert (dest / "tools" / "github.md").exists()
    assert not (dest / "roles" / "spare").exists()
    assert (dest / "roles" / "director").exists()
    mons = [m["name"] for m in
            yaml.safe_load((dest / "monitors" / "defaults.yaml").read_text())["monitors"]]
    # policy-curator is a framework default (#471), seeded as the base layer, so
    # it leads the list; the inherited `drop-me` is gone, `keep` survives.
    assert mons == ["policy-curator", "keep"]


def test_prune_nothing_warns(project):
    _team(project, "core", 'version: "1.0.0"\n', tools={"github.md": "gh"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n'
                 'prune:\n  tools: [does-not-exist]\n')
    chain = compose.resolve_chain(leaf, project)
    prov = compose.compose(chain, paths.package_dir(project))
    assert any("does-not-exist" in w for w in prov.warnings)


# --- framework-default monitors (#471) ---------------------------------------

# An eng-team-shaped policy-curator record, byte-identical to what the
# framework now seeds (bobi/monitors/framework_defaults.yaml). Used to prove
# that removing a team's own copy is a no-op.
_POLICY_CURATOR_RECORD = (
    "  - name: policy-curator\n"
    "    description: >\n"
    "      Distill new agent transcripts since the last run into the team's\n"
    "      policy.md (#456). Runs out-of-band on an interval; the curator agent\n"
    "      works from a dedicated prompt (bobi/prompts/curator.md, team-\n"
    "      overridable) — this description is a human-readable label, not the\n"
    "      agent's working instructions. The `curator: true` marker routes this\n"
    "      monitor to the artifact-writing curator path, not the verdict path.\n"
    "    interval: 6h\n"
    "    event: system/policy.updated\n"
    "    curator: true\n"
)


def _composed_monitors(dest: Path) -> list[dict]:
    return yaml.safe_load((dest / "monitors" / "defaults.yaml").read_text())["monitors"]


def test_framework_curator_seeded_for_team_with_no_monitors(project):
    # A brand-new team that declares NO monitors still composes with the curator.
    leaf = _team(project, "solo", 'version: "1.0.0"\nentry_point: director\n')
    dest, _ = _compose(project, leaf)
    mons = {m["name"]: m for m in _composed_monitors(dest)}
    assert "policy-curator" in mons
    assert mons["policy-curator"]["interval"] == "6h"
    assert mons["policy-curator"]["curator"] is True


def test_framework_curator_prune_opts_out(project):
    # `prune: { monitors: [policy-curator] }` removes the framework default.
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'prune:\n  monitors: [policy-curator]\n')
    dest, _ = _compose(project, leaf)
    names = [m["name"] for m in _composed_monitors(dest)]
    assert "policy-curator" not in names


def test_framework_curator_prune_precedence_in_from_chain(project):
    # A leaf in a `from:` chain can prune the framework default — it is the
    # most-base inherited layer, so the leaf prune reaches it (#471 decision 5).
    _team(project, "core", 'version: "1.0.0"\n',
          monitors="monitors:\n  - {name: keep, interval: 1h}\n")
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n'
                 'prune:\n  monitors: [policy-curator]\n')
    dest, _ = _compose(project, leaf)
    names = [m["name"] for m in _composed_monitors(dest)]
    assert names == ["keep"]  # framework default pruned, base monitor survives


def test_framework_curator_team_override_wins(project):
    # A team's own policy-curator record overlays the framework default — the
    # framework seed is the BASE, so the team's interval wins.
    leaf = _team(project, "solo", 'version: "1.0.0"\nentry_point: director\n',
                 monitors="monitors:\n  - {name: policy-curator, interval: 12h}\n")
    dest, _ = _compose(project, leaf)
    mons = {m["name"]: m for m in _composed_monitors(dest)}
    assert mons["policy-curator"]["interval"] == "12h"
    # Untouched framework fields survive the overlay.
    assert mons["policy-curator"]["curator"] is True
    assert mons["policy-curator"]["event"] == "system/policy.updated"


def test_removing_team_curator_entry_is_byte_identical(project):
    # Acceptance (#471 decision 4): the live eng-team must be neutral before/after
    # removing the eng-team policy-curator entry. Compose an eng-team-shaped
    # team WITH and WITHOUT the redundant record; the monitor files must be equal.
    other = ("monitors:\n"
             "  - {name: pr-conflict-check, interval: 15m, check: pr_conflicts}\n"
             "  - {name: disk-free-check, interval: 5m, check: disk_free}\n")
    with_entry = _team(project, "with", 'version: "1.0.0"\n',
                       monitors=other + _POLICY_CURATOR_RECORD)
    without_entry = _team(project, "without", 'version: "1.0.0"\n',
                          monitors=other)
    dest_with, _ = _compose(project, with_entry, dest=project / "out-with")
    dest_without, _ = _compose(project, without_entry, dest=project / "out-without")
    assert ((dest_with / "monitors" / "defaults.yaml").read_bytes()
            == (dest_without / "monitors" / "defaults.yaml").read_bytes())


# --- determinism, provenance, workspace --------------------------------------


def test_compose_is_deterministic(project):
    _team(project, "core", 'version: "1.0.0"\nbuild:\n  apt: [nodejs]\n',
          agent_md="# Core", tools={"github.md": "gh"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\nbuild:\n  npm: [codex]\n',
                 agent_md="house", tools={"linear.md": "lin"})
    chain = compose.resolve_chain(leaf, project)
    compose.compose(chain, project / "out1")
    compose.compose(chain, project / "out2")
    f1 = sorted(p.relative_to(project / "out1").as_posix()
                for p in (project / "out1").rglob("*") if p.is_file())
    f2 = sorted(p.relative_to(project / "out2").as_posix()
                for p in (project / "out2").rglob("*") if p.is_file())
    assert f1 == f2
    for rel in f1:
        assert (project / "out1" / rel).read_bytes() == (project / "out2" / rel).read_bytes()


def test_provenance_records_source_layer(project):
    _team(project, "core", 'version: "1.0.0"\n', tools={"github.md": "gh"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n',
                 tools={"linear.md": "lin"})
    _, prov = _compose(project, leaf)
    assert prov.items["tools/github.md"] == "core@1.0.0"
    assert prov.items["tools/linear.md"] == "moda@2.0.0"


def test_workspace_not_frozen(project):
    _team(project, "core", 'version: "1.0.0"\n', workspace={"seed.md": "x"})
    leaf = _team(project, "moda", 'from: core\nversion: "2.0.0"\n')
    dest, _ = _compose(project, leaf)
    assert not (dest / "workspace").exists()  # never frozen into the image


def test_merge_workspace_leaf_wins(project):
    # compose() never freezes workspace (above), but the DEPLOY flatten must carry
    # the overlay's per-principal workspace (e.g. assistant-context.md) to the
    # instance. merge_workspace copies base→leaf with LEAF-wins.
    _team(project, "core", 'version: "1.0.0"\n',
          workspace={"assistant-context.md": "BASE", "shared.md": "base-only"})
    leaf = _team(project, "over", 'from: core\nversion: "2.0.0"\n',
                 workspace={"assistant-context.md": "OVERLAY"})
    chain = compose.resolve_chain(leaf, project)
    dest = project / "out"
    compose.merge_workspace(chain, dest)
    # leaf overrides the base for the same file ...
    assert (dest / "workspace" / "assistant-context.md").read_text() == "OVERLAY"
    # ... and a base-only file still lands.
    assert (dest / "workspace" / "shared.md").read_text() == "base-only"


def test_deploy_flatten_carries_overlay_workspace(project):
    # The real deploy-path regression: a `from:` overlay's workspace must ride the
    # flattened tarball, else the per-principal assistant-context.md never reaches
    # the box (and the assistant runs context-less).
    from bobi import deploy
    _team(project, "pa", 'version: "1.0.0"\nentry_point: assistant\n',
          workspace={"assistant-context.md": "TEMPLATE"})
    _team(project, "zpa", 'from: pa\nversion: "1.0.0"\nentry_point: assistant\n',
          workspace={"assistant-context.md": "ZACHS"})
    flat = deploy.resolve_team_dir(project, "zpa")
    assert (flat / "workspace" / "assistant-context.md").read_text() == "ZACHS"


# --- publish guard (#446 §7.1) -----------------------------------------------


def test_reject_path_from(project, tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text("from: ../core\nversion: \"1.0.0\"\n")
    with pytest.raises(compose.ComposeError):
        compose.reject_path_from(f)


def test_reject_path_from_allows_name_ref(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text("from: core@1.0.0\nversion: \"1.0.0\"\n")
    compose.reject_path_from(f)  # no raise


# --- deploy flattening (resolve_team_dir composes a `from:` chain) -----------


def test_deploy_resolve_team_dir_flattens_chain(project):
    from bobi import deploy
    _team(project, "core", 'version: "1.0.0"\nentry_point: director\n'
          'build:\n  apt: [nodejs]\n', tools={"github.md": "gh"})
    _team(project, "moda", 'from: core\nversion: "2.0.0"\n'
          'build:\n  npm: [codex]\n', tools={"linear.md": "lin"})
    out = deploy.resolve_team_dir(project, "moda")
    cfg = yaml.safe_load((out / "agent.yaml").read_text())
    assert "from" not in cfg
    assert cfg["build"]["apt"] == ["nodejs"] and cfg["build"]["npm"] == ["codex"]
    assert (out / "tools" / "github.md").exists()  # inherited from core
    assert (out / "tools" / "linear.md").exists()
    assert out.name == "moda"


def test_deploy_resolve_team_dir_passthrough_no_from(project):
    from bobi import deploy
    src = _team(project, "solo", 'version: "1.0.0"\nentry_point: director\n')
    out = deploy.resolve_team_dir(project, "solo")
    assert out == src.resolve()  # unchanged when there's no `from:`


# --- #452 acceptance: eng-team standalone + a synthetic outside-org overlay


REPO = Path(__file__).resolve().parents[1]
ENG_TEAM_CORE = REPO / "agents" / "eng-team"


def test_eng_team_core_installs_standalone(tmp_path):
    """eng-team composes on its own (no `from:`) — GitHub + Slack only,
    generic tool-agnostic seams. Proves the pristine base is a usable team."""
    proj = tmp_path
    (proj / "agents").mkdir()
    shutil.copytree(ENG_TEAM_CORE, proj / "agents" / "eng-team")
    chain = compose.resolve_chain(proj / "agents" / "eng-team", proj)
    assert [l.dir.name for l in chain] == ["eng-team"]
    dest = paths.package_dir(proj)
    compose.compose(chain, dest)
    cfg = yaml.safe_load((dest / "agent.yaml").read_text())
    assert {s["name"] for s in cfg["services"]} == {"github", "slack"}  # no linear
    assert {r["name"] for r in cfg["requires"]} == {"gh"}               # no gstack/codex
    for role in ("director", "engineer", "project_lead"):
        assert (dest / "roles" / role / "ROLE.md").exists()
    # Tool-agnostic: core names no house tool in its engineer role.
    eng = (dest / "roles" / "engineer" / "ROLE.md").read_text().lower()
    assert "/review" not in eng and "linear" not in eng


def test_synthetic_outside_org_overlay_composes(tmp_path):
    """A third org reuses eng-team without forking: `from: eng-team`
    + a thin overlay (no gstack, a Jira-flavored tracker note, Go house style).
    Proves cross-org reuse is append-only (#452 §6)."""
    proj = tmp_path
    (proj / "agents").mkdir()
    shutil.copytree(ENG_TEAM_CORE, proj / "agents" / "eng-team")
    acme = proj / "agents" / "acme-eng-team"
    _write(acme / "agent.yaml",
           'from: eng-team@1.1.0\nversion: "0.1.0"\n'
           'services:\n  - {name: jira, required: true}\n'
           'build:\n  npm: [some-linter]\n')
    _write(acme / "roles" / "engineer" / "ROLE.md",
           "## Acme house bindings\nUse Jira for tracking; Go/Rust house style.")
    _write(acme / "tools" / "jira.md", "jira guide")
    chain = compose.resolve_chain(acme, proj)
    assert [l.dir.name for l in chain] == ["eng-team", "acme-eng-team"]
    dest = paths.package_dir(proj)
    compose.compose(chain, dest)
    cfg = yaml.safe_load((dest / "agent.yaml").read_text())
    # core services + the overlay's jira; core's generic build accreted the linter.
    assert {s["name"] for s in cfg["services"]} == {"github", "slack", "jira"}
    assert cfg["build"]["apt"] == ["nodejs", "npm", "jq"]      # inherited from core
    assert cfg["build"]["npm"] == ["some-linter"]             # overlay delta
    assert "from" not in cfg
    # engineer role = core craft + acme house bindings appended.
    eng = (dest / "roles" / "engineer" / "ROLE.md").read_text()
    assert "Acme house bindings" in eng and "Jira" in eng
    assert (dest / "tools" / "jira.md").exists()
    assert (dest / "tools" / "github.md").exists()            # core tool inherited


# --- install clearing semantics (reinstall drops stale; project files survive)


def test_reinstall_drops_stale_surface_files(tmp_path, monkeypatch):
    """A reinstall clears the previously frozen copy of each surface the chain
    contributes, so a file the team no longer ships is dropped."""
    from bobi.cli import _install_pack
    proj = tmp_path
    (proj / "agents").mkdir()
    team = _team(proj, "t", 'version: "1.0.0"\nentry_point: director\n',
                 tools={"a.md": "A", "b.md": "B"})
    monkeypatch.chdir(proj)
    _install_pack(team, proj, local_source=True)
    dest = paths.package_dir(proj)
    assert {p.name for p in (dest / "tools").iterdir()} == {"a.md", "b.md"}
    # Drop b.md from the source and reinstall — the frozen b.md must go.
    (team / "tools" / "b.md").unlink()
    _install_pack(team, proj, local_source=True)
    assert {p.name for p in (dest / "tools").iterdir()} == {"a.md"}


def test_reinstall_keeps_uncontributed_project_dirs(tmp_path, monkeypatch):
    """A surface NO layer contributes is left untouched on reinstall — so a
    project-added `.bobi/workflows/*.yaml` survives (pre-compose semantics)."""
    from bobi.cli import _install_pack
    proj = tmp_path
    (proj / "agents").mkdir()
    team = _team(proj, "t", 'version: "1.0.0"\nentry_point: director\n',
                 tools={"a.md": "A"})  # no workflows/
    monkeypatch.chdir(proj)
    _install_pack(team, proj, local_source=True)
    # A project adds its own workflow after install.
    proj_wf = paths.package_dir(proj) / "workflows"
    proj_wf.mkdir(parents=True, exist_ok=True)
    (proj_wf / "adhoc.yaml").write_text("name: adhoc\nsteps: []\n")
    _install_pack(team, proj, local_source=True)  # reinstall
    assert (proj_wf / "adhoc.yaml").exists()  # survived


# --- versioned-tarball + setup/install compatibility (#440 ↔ #446/#451) ------


def test_install_versioned_from_team_fetches_base_from_registry(tmp_path, monkeypatch):
    """The end-to-end new-system path: `bobi install over@2.0.0` for a team
    whose agent.yaml declares `from: core@1.0.0` composes by fetching the
    *versioned base* from the registry — proving versioned tarballs (#440),
    resolution (#446) and merge (#451) work together for install/setup.

    Mirrors how setup's `fetch_into` lands the leaf overlay and install then
    composes its base from the registry (the base isn't a local sibling)."""
    from bobi.cli import _install_pack

    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    proj = tmp_path
    (proj / "agents").mkdir()

    # A "published" versioned base, served by a stubbed registry.fetch into the
    # shared cache (exactly what registry.fetch does for an immutable asset).
    published_core = tmp_path / "published" / "core"
    _write(published_core / "agent.yaml",
           'version: "1.0.0"\nentry_point: director\n'
           'services:\n  - {name: github, required: true}\n'
           'build:\n  apt: [nodejs]\n')
    _write(published_core / "tools" / "github.md", "gh")
    _write(published_core / "roles" / "engineer" / "ROLE.md",
           "# Engineer\nrun your review gate")

    def fake_fetch(project_path, name, *, version=None, repo=None):
        assert (name, version) == ("core", "1.0.0")  # versioned base, exact pin
        dest = registry.cache_path(project_path, name)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(published_core, dest)
        (dest / ".meta.json").write_text('{"version": "1.0.0"}')
        return dest

    monkeypatch.setattr(registry, "fetch", fake_fetch)

    # The fetched leaf overlay (as setup/install would land it), pinning the base.
    over = tmp_path / "over"
    _write(over / "agent.yaml",
           'from: core@1.0.0\nversion: "2.0.0"\n'
           'services:\n  - {name: linear, required: true}\n'
           'build:\n  npm: [codex]\n')
    _write(over / "roles" / "engineer" / "ROLE.md", "use /review (house)")
    _write(over / "tools" / "linear.md", "linear")

    monkeypatch.chdir(proj)
    _install_pack(over, proj, local_source=False)

    dest = paths.package_dir(proj)
    cfg = yaml.safe_load((dest / "agent.yaml").read_text())
    # Composed from the registry-fetched base + the overlay leaf.
    assert {s["name"] for s in cfg["services"]} == {"github", "linear"}
    assert cfg["build"]["apt"] == ["nodejs"] and cfg["build"]["npm"] == ["codex"]
    assert "from" not in cfg and cfg["version"] == "2.0.0"
    eng = (dest / "roles" / "engineer" / "ROLE.md").read_text()
    assert "run your review gate" in eng and "use /review (house)" in eng
    assert (dest / "tools" / "github.md").exists()   # from the fetched base
    assert (dest / "tools" / "linear.md").exists()    # from the leaf
    # The compose-lock records the resolved versioned chain (reproducibility).
    import json
    lock = json.loads((dest / "compose-lock.json").read_text())
    assert [(c["ref"], c["version"]) for c in lock["chain"]] == \
        [("core@1.0.0", "1.0.0"), (None, "2.0.0")]
