"""Tests for the unified dependency library (#428, was #416) — `tool_library:`
catalog refs + inline mappings, resolver, and dependency-list hash.

Covers the verification plan:
  * pin de-dup across `from:` layers (the headline — proves three-place drift is
    gone),
  * expansion basics, local-wins guide, explicit-wins requires,
  * `tool_library` union across layers + key consumed at compose,
  * unknown entry + missing-`success` validation (the required contract),
  * inline mapping dependencies (declared directly, no catalog entry),
  * dependency-list hashing/change-detection (mirror of `team_deps_hash`),
  * pin lint (no floating refs; `fix` pin agrees with `install` pin),
  * the #452-style regression bar: `tool_library: [...]` composes byte-identical
    to the same team with those surfaces hand-written inline.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from bobi import compose, tool_library


# --- fixtures ----------------------------------------------------------------


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _team(root: Path, name: str, agent_yaml: str, *,
          tools: dict[str, str] | None = None) -> Path:
    d = root / "agents" / name
    _write(d / "agent.yaml", agent_yaml)
    for fn, body in (tools or {}).items():
        _write(d / "tools" / fn, body)
    return d


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "agents").mkdir(parents=True)
    return tmp_path


def _compose(project, leaf, dest=None):
    chain = compose.resolve_chain(leaf, project)
    dest = dest or (project / ".bobi")
    prov = compose.compose(chain, dest)
    return dest, prov


def _agent_yaml(dest: Path) -> dict:
    return yaml.safe_load((dest / "agent.yaml").read_text())


CODEX_PIN = "@openai/codex@0.144.4"
VENN_PIN = "venn-cli==0.2.0"


# --- headline: pin de-dup across layers --------------------------------------


def test_pin_dedups_across_from_layers(project):
    """base and leaf BOTH reference `tool_library: [codex]` → the codex pin
    appears exactly once in build.npm and the codex requires entry exactly once.
    This is the three-place-drift fix. Fails on main (no expansion)."""
    _team(project, "core",
          'version: "1.0.0"\nentry_point: director\ntool_library: [codex]\n')
    leaf = _team(project, "moda",
                 'from: core\nversion: "2.0.0"\ntool_library: [codex]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])

    assert cfg["build"]["npm"].count(CODEX_PIN) == 1
    codex_reqs = [r for r in cfg["requires"] if r["name"] == "codex"]
    assert len(codex_reqs) == 1


# --- expansion basics --------------------------------------------------------


def test_expansion_basics(project):
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\ntool_library: [codex]\n')
    dest = _compose(project, leaf)[0]
    cfg = _agent_yaml(dest)

    assert any(r["name"] == "codex" for r in cfg["requires"])
    assert cfg["build"]["apt"] == ["nodejs", "npm"]
    assert CODEX_PIN in cfg["build"]["npm"]
    guide = (tool_library.CATALOG_DIR / "codex" / "guide.md").read_text()
    assert (dest / "tools" / "codex.md").read_text() == guide


def test_tool_library_key_consumed(project):
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\ntool_library: [codex]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    assert "tool_library" not in cfg


def test_codex_brain_does_not_bake_codex(project):
    """The Codex CLI ships in the base image (#428), so a `brain: codex` team
    bakes NOTHING extra - no implied codex dependency, no build. (This reverses
    the old #416 brain-implied expansion, now obsolete.)"""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'brain:\n  kind: codex\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])

    assert not any(
        r.get("name") == "codex" for r in (cfg.get("requires") or []))
    assert "build" not in cfg or CODEX_PIN not in (cfg["build"].get("npm") or [])
    assert "tool_library" not in cfg


def test_explicit_codex_tool_still_bakes(project):
    """An explicit `tool_library: [codex]` still works (e.g. to pin a specific
    codex version different from the base image's) - only the brain-IMPLIED
    auto-add was removed."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library: [codex]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    assert any(r["name"] == "codex" for r in cfg["requires"])
    assert CODEX_PIN in cfg["build"]["npm"]


def test_gstack_catalog_entry_bakes_browser_toolchain(project):
    """The gstack catalog entry is self-contained: it declares its own
    nodejs/npm (the base image is Node-free), the pinned bun/Playwright/`./setup`
    install, a `success` check, and its usage guide - so any team gets a working
    browser-QA toolchain from `tool_library: [gstack]` alone, with no per-team
    build recipe."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library: [gstack]\n')
    dest = _compose(project, leaf)[0]
    cfg = _agent_yaml(dest)
    build = cfg["build"]
    # Self-contained node: gstack must NOT rely on another dependency (e.g. the
    # old codex bake) to supply the npm runtime its install needs.
    assert "nodejs" in build["apt"] and "npm" in build["apt"]
    assert "bun@1.3.14" in build["npm"]
    assert any("playwright@1.61.0" in s for s in build["run_root"])
    assert any("garrytan/gstack" in s for s in build["run"])
    assert any(r["name"] == "gstack" for r in cfg["requires"])
    assert (dest / "tools" / "gstack.md").is_file()


# --- local / explicit wins (escape hatches) ----------------------------------


def test_local_tool_guide_wins(project):
    """A team shipping its own tools/codex.md keeps it — catalog does not clobber."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\ntool_library: [codex]\n',
                 tools={"codex.md": "TEAM OWN CODEX GUIDE"})
    dest = _compose(project, leaf)[0]
    assert (dest / "tools" / "codex.md").read_text() == "TEAM OWN CODEX GUIDE"


@pytest.fixture
def gizmo_catalog(tmp_path, monkeypatch):
    """A one-entry catalog whose guide the test can rewrite (a framework
    upgrade), standing in for `bobi/tool_library/<name>/`."""
    cat = tmp_path / "cat"
    _write(cat / "gizmo" / "tool.yaml", 'success: "command -v gizmo"\n')
    _write(cat / "gizmo" / "guide.md", "GUIDE v1")
    monkeypatch.setattr(tool_library, "CATALOG_DIR", cat)
    return cat


_GIZMO_TEAM = 'version: "1.0.0"\nentry_point: director\ntool_library: [gizmo]\n'


def test_generated_guide_refreshes_when_the_catalog_updates(project, gizmo_catalog):
    """A reinstall reuses dest, and install.py only clears surfaces a layer
    contributes — so a team shipping no tools/ keeps the previous install's
    guide. It must still pick up a catalog guide update, not freeze forever."""
    leaf = _team(project, "solo", _GIZMO_TEAM)
    dest = _compose(project, leaf)[0]
    assert (dest / "tools" / "gizmo.md").read_text() == "GUIDE v1"

    _write(gizmo_catalog / "gizmo" / "guide.md", "GUIDE v2")  # framework upgrade
    _compose(project, leaf, dest=dest)                        # reinstall, same dest

    assert (dest / "tools" / "gizmo.md").read_text() == "GUIDE v2"


def test_generated_guide_is_dropped_when_the_dependency_goes_away(
        project, gizmo_catalog):
    """Removing the dependency removes its guide — an orphaned tools/<name>.md
    would keep telling the runtime agent about a tool it no longer has."""
    leaf = _team(project, "solo", _GIZMO_TEAM)
    dest = _compose(project, leaf)[0]
    assert (dest / "tools" / "gizmo.md").is_file()

    _write(leaf / "agent.yaml", 'version: "1.0.0"\nentry_point: director\n')
    _compose(project, leaf, dest=dest)

    assert not (dest / "tools" / "gizmo.md").exists()


def test_team_shipped_guide_survives_a_reinstall(project, gizmo_catalog):
    """The leaf-wins escape hatch holds across reinstalls into a reused dest:
    a team's own tools/<name>.md is never refreshed away."""
    leaf = _team(project, "solo", _GIZMO_TEAM, tools={"gizmo.md": "TEAM OWN"})
    dest = _compose(project, leaf)[0]
    _write(gizmo_catalog / "gizmo" / "guide.md", "GUIDE v2")
    _compose(project, leaf, dest=dest)

    assert (dest / "tools" / "gizmo.md").read_text() == "TEAM OWN"


def test_guide_record_torn_write_does_not_orphan_the_generated_guides(
        project, gizmo_catalog):
    """The ownership record must never be observable half-written.

    `_read_guide_record` maps unparseable JSON to `{}`, and an empty record
    reclassifies every generated guide as team-shipped: the next compose skips
    it forever and `_write_guide_record({})` then unlinks the record, making
    the loss permanent. Exactly what the record exists to prevent.
    """
    leaf = _team(project, "solo", _GIZMO_TEAM)
    dest = _compose(project, leaf)[0]
    record = dest / tool_library.GUIDE_RECORD
    before = record.read_text()

    real_write_text = Path.write_text

    def killed(self, data, *a, **kw):
        if tool_library.GUIDE_RECORD not in self.name:
            return real_write_text(self, data, *a, **kw)
        real_write_text(self, data[:12], *a, **kw)
        raise KeyboardInterrupt("simulated kill mid-write")

    _write(gizmo_catalog / "gizmo" / "guide.md", "GUIDE v2")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "write_text", killed)
        with pytest.raises(KeyboardInterrupt):
            _compose(project, leaf, dest=dest)

    assert record.read_text() == before          # whole previous record survives
    assert [p.name for p in dest.iterdir() if p.name.endswith(".tmp")] == []
    # Ownership intact, so the next compose still refreshes its own guide.
    _compose(project, leaf, dest=dest)
    assert (dest / "tools" / "gizmo.md").read_text() == "GUIDE v2"


def test_expanding_a_dependency_takes_no_destination(project):
    """Splicing agent.yaml surfaces touches no filesystem.

    Guide materialization moved out to `_materialize_guides`, so a `dest` here
    is a dead second seam suggesting `_expand_dependency` still writes files.
    """
    params = inspect.signature(tool_library._expand_dependency).parameters
    assert list(params) == ["dep", "merged_yaml"]


def test_generated_guide_record_is_gitignored(project):
    """`run/package/tool-library-guides.json` is generated, so install ignores it.

    install owns package/.gitignore and rewrites it every run; a generated
    artifact missing from that list shows up untracked on a fresh clone and
    churns on every reinstall.
    """
    from bobi import install, paths

    paths.package_dir(project).mkdir(parents=True, exist_ok=True)
    for local_source in (True, False):
        install.write_install_gitignore(project, local_source)
        entries = (paths.package_dir(project) / ".gitignore").read_text().split("\n")
        assert tool_library.GUIDE_RECORD in entries


def test_explicit_requires_wins(project):
    """An explicit team `requires: [{name: codex, ...}]` is neither duplicated nor
    clobbered by the catalog entry (leaf field wins / escape hatch)."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library: [codex]\n'
                 'requires:\n'
                 '  - name: codex\n'
                 '    why: "team-custom reason"\n'
                 '    check: "true"\n'
                 '    fix: "team-custom fix"\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    codex_reqs = [r for r in cfg["requires"] if r["name"] == "codex"]
    assert len(codex_reqs) == 1
    assert codex_reqs[0]["why"] == "team-custom reason"
    assert codex_reqs[0]["fix"] == "team-custom fix"


# --- union across layers -----------------------------------------------------


def test_tool_library_unions_across_layers(project):
    _team(project, "core",
          'version: "1.0.0"\nentry_point: director\ntool_library: [venn]\n')
    leaf = _team(project, "moda",
                 'from: core\nversion: "2.0.0"\ntool_library: [codex]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    names = {r["name"] for r in cfg["requires"]}
    assert {"codex", "venn"} <= names


def test_leaf_layer_overrides_an_inherited_dependency(project):
    """The leaf always wins (compose §3.1): an overlay re-declaring an inherited
    dependency by name replaces it — the union appends the overlay's entry after
    the base's, so keeping the base's would invert compose's precedence and
    silently bake the base's pin."""
    _team(project, "core",
          'version: "1.0.0"\nentry_point: director\ntool_library: [codex]\n')
    leaf = _team(project, "moda",
                 'from: core\nversion: "2.0.0"\n'
                 'tool_library:\n'
                 '  - name: codex\n'
                 '    success: "codex --version"\n'
                 '    install:\n'
                 '      npm: ["@openai/codex@0.150.0"]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])

    assert "@openai/codex@0.150.0" in cfg["build"]["npm"]
    assert CODEX_PIN not in cfg["build"]["npm"]
    codex_reqs = [r for r in cfg["requires"] if r["name"] == "codex"]
    assert len(codex_reqs) == 1
    assert codex_reqs[0]["check"] == "codex --version"


def test_overriding_a_dependency_keeps_the_declaration_order(project):
    """An override replaces in place: the dependency list order (and so the
    accreted build/requires order) stays the base's, not the overlay's."""
    _team(project, "core",
          'version: "1.0.0"\nentry_point: director\ntool_library: [codex, venn]\n')
    leaf = _team(project, "moda",
                 'from: core\nversion: "2.0.0"\n'
                 'tool_library:\n'
                 '  - name: codex\n'
                 '    success: "codex --version"\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    assert [r["name"] for r in cfg["requires"]] == ["codex", "venn"]


# --- error paths -------------------------------------------------------------


def test_unknown_entry_raises_listing_available(project):
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\ntool_library: [nope]\n')
    with pytest.raises(compose.ComposeError) as ei:
        _compose(project, leaf)
    msg = str(ei.value)
    assert "nope" in msg
    # lists the real catalog entries so the author can self-correct
    assert "codex" in msg and "venn" in msg


def test_catalog_entry_missing_success_rejected(tmp_path, monkeypatch):
    """`success` is the required contract — a catalog entry without one is
    rejected with a clear, self-correcting error (no more `kind` axis)."""
    cat = tmp_path / "cat"
    _write(cat / "halfbaked" / "tool.yaml", "install:\n  apt: [foo]\n")
    _write(cat / "halfbaked" / "guide.md", "x")
    monkeypatch.setattr(tool_library, "CATALOG_DIR", cat)

    with pytest.raises(compose.ComposeError) as ei:
        tool_library.load_entry("halfbaked")
    msg = str(ei.value)
    assert "halfbaked" in msg and "success" in msg


def test_inline_dependency_missing_success_rejected(project):
    """An inline `tool_library:` mapping must also carry `success`."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library:\n'
                 '  - name: gizmo\n'
                 '    install:\n'
                 '      apt: [gizmo]\n')
    with pytest.raises(compose.ComposeError) as ei:
        _compose(project, leaf)
    msg = str(ei.value)
    assert "gizmo" in msg and "success" in msg


def test_inline_dependency_expands_like_a_catalog_entry(project):
    """An inline mapping dependency contributes the same surfaces a catalog entry
    does: a `requires` check from `success`, a `build` from `install`, and a
    `tools/<name>.md` from `guide`."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library:\n'
                 '  - name: gizmo\n'
                 '    success: "command -v gizmo"\n'
                 '    why: "Use the gizmo CLI."\n'
                 '    guide: "Run gizmo --help."\n'
                 '    install:\n'
                 '      apt: [gizmo-pkg]\n')
    dest = _compose(project, leaf)[0]
    cfg = _agent_yaml(dest)

    gizmo = [r for r in cfg["requires"] if r["name"] == "gizmo"]
    assert len(gizmo) == 1
    assert gizmo[0]["check"] == "command -v gizmo"
    assert gizmo[0]["why"] == "Use the gizmo CLI."
    assert cfg["build"]["apt"] == ["gizmo-pkg"]
    assert (dest / "tools" / "gizmo.md").read_text() == "Run gizmo --help."
    assert "tool_library" not in cfg


# --- mcp: per-brain wiring (#428 Stage 4) ------------------------------------


def test_mcp_dependency_emits_mcp_servers(project):
    """A dependency's `mcp:` spec is emitted into the composed agent.yaml's
    top-level `mcp_servers:` (the SDK-native `{name: spec}` shape) so the Claude
    pass-through carries it unchanged. Team-brought MCP, no built-in shim."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library:\n'
                 '  - name: weather\n'
                 '    success: "command -v weather-mcp"\n'
                 '    mcp:\n'
                 '      weather:\n'
                 '        type: stdio\n'
                 '        command: /usr/local/bin/weather-mcp\n'
                 '        args: ["--stdio"]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])

    assert cfg["mcp_servers"]["weather"] == {
        "type": "stdio",
        "command": "/usr/local/bin/weather-mcp",
        "args": ["--stdio"],
    }


def test_mcp_dependency_no_mcp_key_when_absent(project):
    """A dependency without `mcp:` must not synthesize an empty `mcp_servers:`
    key (byte-noise / churn guard)."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library:\n'
                 '  - name: gizmo\n'
                 '    success: "command -v gizmo"\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    assert "mcp_servers" not in cfg


def test_explicit_mcp_server_wins_over_dependency(project):
    """An explicit team `mcp_servers.<name>` overrides a dependency's `mcp:` for
    that name wholesale (leaf-wins escape hatch, mirrors requires/host)."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'mcp_servers:\n'
                 '  weather:\n'
                 '    type: http\n'
                 '    url: https://team.example/mcp\n'
                 'tool_library:\n'
                 '  - name: weather\n'
                 '    success: "true"\n'
                 '    mcp:\n'
                 '      weather:\n'
                 '        type: stdio\n'
                 '        command: /usr/local/bin/weather-mcp\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    assert cfg["mcp_servers"]["weather"] == {
        "type": "http",
        "url": "https://team.example/mcp",
    }


def test_mcp_first_dependency_wins_on_name_clash(project):
    """Two dependencies contributing the same MCP server name → first wins
    (resolve order), the second is not field-merged (mirrors host dedup)."""
    _team(project, "core",
          'version: "1.0.0"\nentry_point: director\n'
          'tool_library:\n'
          '  - name: alpha\n'
          '    success: "true"\n'
          '    mcp:\n'
          '      shared:\n'
          '        type: stdio\n'
          '        command: /opt/alpha\n')
    leaf = _team(project, "moda",
                 'from: core\nversion: "2.0.0"\n'
                 'tool_library:\n'
                 '  - name: beta\n'
                 '    success: "true"\n'
                 '    mcp:\n'
                 '      shared:\n'
                 '        type: stdio\n'
                 '        command: /opt/beta\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    # core resolves before the leaf's inline dep (from-chain order), so alpha wins.
    assert cfg["mcp_servers"]["shared"]["command"] == "/opt/alpha"


def test_mcp_two_dependencies_distinct_names_both_present(project):
    """Two dependencies each bringing a distinct MCP server → both emitted."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'tool_library:\n'
                 '  - name: alpha\n'
                 '    success: "true"\n'
                 '    mcp:\n'
                 '      a:\n'
                 '        type: stdio\n'
                 '        command: /opt/a\n'
                 '  - name: beta\n'
                 '    success: "true"\n'
                 '    mcp:\n'
                 '      b:\n'
                 '        type: stdio\n'
                 '        command: /opt/b\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    assert set(cfg["mcp_servers"]) == {"a", "b"}


# --- pin lint (extends the #380 reproducibility convention) -------------------


def _all_entries():
    return sorted(p.name for p in tool_library.CATALOG_DIR.iterdir()
                  if p.is_dir() and (p / "tool.yaml").is_file())


def test_no_floating_refs_in_catalog():
    """No `@latest`, bare `HEAD`, or unpinned ref in any tool.yaml (#380)."""
    floating = ("@latest", "@HEAD", "@main", "@master")
    for name in _all_entries():
        text = (tool_library.CATALOG_DIR / name / "tool.yaml").read_text()
        for tok in floating:
            assert tok not in text, f"{name}: floating ref {tok}"


def test_fix_pin_agrees_with_install_pin():
    """The pin co-located in `fix` and `install` within one entry must agree —
    the one remaining co-location is guarded, not scattered (§4.1)."""
    pins = {"codex": CODEX_PIN, "venn": VENN_PIN}
    for name, pin in pins.items():
        entry = tool_library.load_entry(name)
        install_text = yaml.dump(entry.install)
        assert pin in install_text, f"{name}: pin {pin} missing from install"
        assert pin in entry.fix, f"{name}: fix pin disagrees with install"


def test_codex_requires_accepts_subscription_auth_without_api_key(tmp_path, monkeypatch):
    """Existing ~/.codex/auth.json is enough; subscription-auth containers must
    not need OPENAI_API_KEY in the environment."""
    entry = tool_library.load_entry("codex")
    check = entry.success
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    log = tmp_path / "codex.log"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"tokens":"subscription"}\n')
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$CODEX_LOG\"\n"
        "exit 0\n"
    )
    codex.chmod(0o755)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    proc = subprocess.run(
        ["bash", "-c", check],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CODEX_LOG": str(log),
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert log.read_text().startswith("exec ")
    assert "timeout 8s" not in check
    assert "timeout=8" in check


def test_codex_requires_subscription_does_not_overwrite_oauth_auth(tmp_path, monkeypatch):
    entry = tool_library.load_entry("codex")
    check = entry.success
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    (home / ".codex").mkdir(parents=True)
    auth_file = home / ".codex" / "auth.json"
    auth_file.write_text('{"tokens":"subscription"}\n')
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/usr/bin/env bash\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-materialize")
    monkeypatch.setenv("BOBI_AUTH", "subscription")
    monkeypatch.delenv("BOBI_VERIFY_PHASE", raising=False)

    proc = subprocess.run(
        ["bash", "-c", check],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert auth_file.read_text() == '{"tokens":"subscription"}\n'


def test_codex_requires_subscription_recognizes_real_oauth_auth(tmp_path, monkeypatch):
    """A REAL codex OAuth `auth.json` carries an `OPENAI_API_KEY` field (null)
    ALONGSIDE its `tokens` - `codex login` writes both. The subscription check
    must treat this as OAuth (pass), not misread the mere presence of the
    `OPENAI_API_KEY` key as an API-key auth file. Regression for the device-login
    flood: the entrypoint used the same naive `"OPENAI_API_KEY" in data` test and
    wiped valid OAuth creds on every boot, re-posting a device code each time."""
    entry = tool_library.load_entry("codex")
    check = entry.success
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    (home / ".codex").mkdir(parents=True)
    # The shape `codex login --device-auth` actually writes.
    (home / ".codex" / "auth.json").write_text(
        '{"OPENAI_API_KEY": null, "tokens": {"id_token": "i", '
        '"access_token": "a", "refresh_token": "r"}, "last_refresh": "t"}\n')
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/usr/bin/env bash\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BOBI_AUTH", "subscription")
    monkeypatch.delenv("BOBI_VERIFY_PHASE", raising=False)

    proc = subprocess.run(
        ["bash", "-c", check], capture_output=True, text=True,
        env={**os.environ, "HOME": str(home),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, (
        f"real OAuth auth.json misread as an API-key file: {proc.stderr}")


def test_codex_requires_subscription_rejects_api_key_auth_file(tmp_path, monkeypatch):
    entry = tool_library.load_entry("codex")
    check = entry.success
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        '{"OPENAI_API_KEY":"sk-stale"}\n'
    )
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/usr/bin/env bash\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("BOBI_AUTH", "subscription")
    monkeypatch.delenv("BOBI_VERIFY_PHASE", raising=False)

    proc = subprocess.run(
        ["bash", "-c", check],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )

    assert proc.returncode != 0


def test_codex_requires_subscription_fails_without_oauth_auth(tmp_path, monkeypatch):
    entry = tool_library.load_entry("codex")
    check = entry.success
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/usr/bin/env bash\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BOBI_AUTH", "subscription")

    proc = subprocess.run(
        ["bash", "-c", check],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )

    assert proc.returncode != 0


def test_codex_requires_api_key_mode_fails_without_api_key_or_auth(
    tmp_path,
    monkeypatch,
):
    entry = tool_library.load_entry("codex")
    check = entry.success
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/usr/bin/env bash\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BOBI_AUTH", raising=False)
    monkeypatch.delenv("BOBI_VERIFY_PHASE", raising=False)

    proc = subprocess.run(
        ["bash", "-c", check],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )

    assert proc.returncode != 0


def test_codex_requires_allows_binary_probe_during_build_verify(tmp_path, monkeypatch):
    entry = tool_library.load_entry("codex")
    check = entry.success
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    log = tmp_path / "codex.log"
    home.mkdir()
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$CODEX_LOG\"\n"
        "exit 0\n"
    )
    codex.chmod(0o755)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    proc = subprocess.run(
        ["bash", "-c", check],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CODEX_LOG": str(log),
            "BOBI_VERIFY_PHASE": "build",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert log.read_text() == "--version\n"


def test_codex_requires_fix_preserves_subscription_login_path():
    entry = tool_library.load_entry("codex")
    fix = entry.fix

    assert '[ "${BOBI_AUTH:-api_key}" != "subscription" ]' in fix
    assert '[ -n "${OPENAI_API_KEY:-}" ]' in fix
    assert "codex auth login" in fix


# --- dependency-list hash (re-bootstrap change-detection) --------------------


def _dep(name, success="ok", **kw):
    return tool_library.Dependency(name=name, success=success, **kw)


def test_dependency_list_hash_is_stable():
    """Same declared set → same hash (the warm-boot skip key)."""
    deps = [_dep("codex", install={"npm": ["@openai/codex@0.144.4"]}),
            _dep("venn", guide="x")]
    assert tool_library.dependency_list_hash(deps) == \
        tool_library.dependency_list_hash(list(deps))


def test_dependency_list_hash_is_order_independent():
    """The set's identity, not its declaration order — reordering must not churn
    the snapshot key."""
    a = [_dep("codex"), _dep("venn")]
    b = [_dep("venn"), _dep("codex")]
    assert tool_library.dependency_list_hash(a) == \
        tool_library.dependency_list_hash(b)


def test_dependency_list_hash_changes_on_materialization_change():
    """A change to any field a bootstrap would act on (success/guide/install/
    host/mcp) changes the hash, so a changed set triggers re-bootstrap."""
    base = [_dep("codex", install={"npm": ["@openai/codex@0.144.4"]})]
    assert tool_library.dependency_list_hash(base) != \
        tool_library.dependency_list_hash(
            [_dep("codex", install={"npm": ["@openai/codex@0.143.0"]})])
    assert tool_library.dependency_list_hash(base) != \
        tool_library.dependency_list_hash([_dep("codex", success="different")])
    assert tool_library.dependency_list_hash(base) != \
        tool_library.dependency_list_hash(base + [_dep("venn")])


def test_dependency_list_hash_ignores_documentation_fields():
    """`why`/`fix` are documentation/legacy-doctor hints — they do not change
    what is materialized, so they must not churn the snapshot key."""
    assert tool_library.dependency_list_hash([_dep("codex")]) == \
        tool_library.dependency_list_hash(
            [_dep("codex", why="a reason", fix="a repair")])


# --- #452-style regression bar -----------------------------------------------


# The same three surfaces a `tool_library: [codex, venn]` entry expands into,
# hand-written inline. If this drifts from the catalog the regression test fails
# loudly — which is the point (it guards the catalog too).
_INLINE_AGENT_YAML = """\
version: "1.0.0"
entry_point: director
agent: acme
requires:
  - name: codex
    why: "Delegate a coding sub-task to the Codex CLI (tools/codex.md)."
    check: "command -v codex >/dev/null 2>&1 && { if { [ \\"${BOBI_BRAIN:-}\\" = \\"codex\\" ] || [ \\"${BOBI_BRAIN:-}\\" = \\"gateway-openai\\" ]; } && [ -n \\"${BOBI_GATEWAY_BASE_URL:-}\\" ]; then python3 -c 'import re, subprocess, sys; out=subprocess.check_output([\\"codex\\", \\"--version\\"], text=True); m=re.search(r\\"(\\\\d+)\\\\.(\\\\d+)\\\\.(\\\\d+)\\", out); sys.exit(0 if m and tuple(map(int, m.groups())) >= (0, 144, 4) else 1)'; elif [ \\"${BOBI_AUTH:-api_key}\\" != \\"subscription\\" ] && [ -n \\"${OPENAI_API_KEY:-}\\" ]; then mkdir -p ~/.codex && python3 -c 'import json, os, pathlib; p=pathlib.Path.home()/\\".codex\\"/\\"auth.json\\"; p.write_text(json.dumps({\\"OPENAI_API_KEY\\": os.environ[\\"OPENAI_API_KEY\\"]})+\\"\\\\n\\"); p.chmod(0o600)'; fi; if { [ \\"${BOBI_BRAIN:-}\\" = \\"codex\\" ] || [ \\"${BOBI_BRAIN:-}\\" = \\"gateway-openai\\" ]; } && [ -n \\"${BOBI_GATEWAY_BASE_URL:-}\\" ]; then true; elif [ -f ~/.codex/auth.json ]; then if [ \\"${BOBI_AUTH:-api_key}\\" = \\"subscription\\" ] && python3 -c 'import json, pathlib, sys; p=pathlib.Path.home()/\\".codex\\"/\\"auth.json\\"; data=json.loads(p.read_text()); sys.exit(0 if isinstance(data, dict) and data.get(\\"OPENAI_API_KEY\\") and not data.get(\\"tokens\\") else 1)'; then false; else python3 -c 'import subprocess, sys; sys.exit(subprocess.run([\\"codex\\", \\"exec\\", \\"-s\\", \\"read-only\\", \\"--skip-git-repo-check\\", \\"reply OK\\"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=8).returncode)'; fi; elif [ \\"${BOBI_VERIFY_PHASE:-}\\" = \\"build\\" ]; then codex --version >/dev/null 2>&1; else false; fi; }"
    fix: "npm install -g @openai/codex@0.144.4 && { if { [ \\"${BOBI_BRAIN:-}\\" = \\"codex\\" ] || [ \\"${BOBI_BRAIN:-}\\" = \\"gateway-openai\\" ]; } && [ -n \\"${BOBI_GATEWAY_BASE_URL:-}\\" ]; then codex --version >/dev/null 2>&1; elif [ \\"${BOBI_AUTH:-api_key}\\" != \\"subscription\\" ] && [ -n \\"${OPENAI_API_KEY:-}\\" ]; then mkdir -p ~/.codex && python3 -c 'import json, os, pathlib; p=pathlib.Path.home()/\\".codex\\"/\\"auth.json\\"; p.write_text(json.dumps({\\"OPENAI_API_KEY\\": os.environ[\\"OPENAI_API_KEY\\"]})+\\"\\\\n\\"); p.chmod(0o600)'; else codex auth login || echo 'Set OPENAI_API_KEY in run/.env or run codex auth login'; fi; }"
  - name: venn
    why: "Reach external services (email, calendar, CRM) via the Venn CLI (tools/venn.md). Auth via VENN_API_KEY."
    check: "command -v venn >/dev/null 2>&1 && venn --help >/dev/null 2>&1"
    fix: "python3 -m venv /opt/venn-cli && /opt/venn-cli/bin/pip install venn-cli==0.2.0 && ln -sf /opt/venn-cli/bin/venn /usr/local/bin/venn && echo 'Set VENN_API_KEY in run/.env'"
build:
  apt: [nodejs, npm, python3-venv]
  npm: ["@openai/codex@0.144.4"]
  run_root:
    - >-
      python3 -m venv /opt/venn-cli &&
      /opt/venn-cli/bin/pip install --no-cache-dir venn-cli==0.2.0 &&
      ln -sf /opt/venn-cli/bin/venn /usr/local/bin/venn
"""


def test_regression_byte_identical_to_inline(project, tmp_path):
    """`tool_library: [codex, venn]` composes byte-identical (agent.yaml + tools/)
    to the same team with those surfaces hand-written inline — proving the
    migration is provably zero-behavior-change (#452 bar)."""
    tl = _team(project, "acme",
               'version: "1.0.0"\nentry_point: director\n'
               'tool_library: [codex, venn]\n')
    out_tl = _compose(project, tl, dest=tmp_path / "out_tl")[0]

    # Same team name, surfaces hand-written inline (no tool_library).
    inline_root = tmp_path / "inline_proj"
    (inline_root / "agents").mkdir(parents=True)
    codex_guide = (tool_library.CATALOG_DIR / "codex" / "guide.md").read_text()
    venn_guide = (tool_library.CATALOG_DIR / "venn" / "guide.md").read_text()
    inline = _team(inline_root, "acme", _INLINE_AGENT_YAML,
                   tools={"codex.md": codex_guide, "venn.md": venn_guide})
    out_inline = _compose(inline_root, inline, dest=tmp_path / "out_inline")[0]

    assert (out_tl / "agent.yaml").read_bytes() == (out_inline / "agent.yaml").read_bytes()
    assert (out_tl / "tools" / "codex.md").read_bytes() == (out_inline / "tools" / "codex.md").read_bytes()
    assert (out_tl / "tools" / "venn.md").read_bytes() == (out_inline / "tools" / "venn.md").read_bytes()
