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


CODEX_PIN = "@openai/codex@0.142.0"
VENN_PIN = "venn-cli==0.2.0"
OPENAI_PIN = "openai==2.43.0"


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


def test_codex_brain_implicitly_expands_codex_tool(project):
    """A Codex-brained team should bake the Codex CLI without an explicit
    `tool_library: [codex]` opt-in."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'brain:\n  kind: codex\n')
    dest = _compose(project, leaf)[0]
    cfg = _agent_yaml(dest)

    assert any(r["name"] == "codex" for r in cfg["requires"])
    assert cfg["build"]["apt"] == ["nodejs", "npm"]
    assert CODEX_PIN in cfg["build"]["npm"]
    assert "tool_library" not in cfg
    guide = (tool_library.CATALOG_DIR / "codex" / "guide.md").read_text()
    assert (dest / "tools" / "codex.md").read_text() == guide


def test_codex_brain_dedupes_explicit_codex_tool(project):
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'brain:\n  kind: codex\n'
                 'tool_library: [codex]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])

    assert cfg["build"]["npm"].count(CODEX_PIN) == 1
    codex_reqs = [r for r in cfg["requires"] if r["name"] == "codex"]
    assert len(codex_reqs) == 1


def test_codex_brain_preserves_explicit_codex_build_override(project):
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\n'
                 'brain:\n  kind: codex\n'
                 'requires:\n'
                 '  - name: codex\n'
                 '    why: "team custom codex install"\n'
                 '    check: "command -v codex-custom"\n'
                 'build:\n'
                 '  npm: ["@openai/codex@9.9.9"]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])

    assert cfg["build"]["npm"] == ["@openai/codex@9.9.9"]
    codex_reqs = [r for r in cfg["requires"] if r["name"] == "codex"]
    assert len(codex_reqs) == 1
    assert codex_reqs[0]["check"] == "command -v codex-custom"


def test_codex_brain_overlay_expands_codex_tool(project):
    _team(project, "core",
          'version: "1.0.0"\nentry_point: director\n'
          'brain:\n  kind: claude\n')
    leaf = _team(project, "moda",
                 'from: core\nversion: "2.0.0"\n'
                 'brain:\n  kind: codex\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])

    assert cfg["brain"]["kind"] == "codex"
    assert any(r["name"] == "codex" for r in cfg["requires"])
    assert CODEX_PIN in cfg["build"]["npm"]


# --- local / explicit wins (escape hatches) ----------------------------------


def test_local_tool_guide_wins(project):
    """A team shipping its own tools/codex.md keeps it — catalog does not clobber."""
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\ntool_library: [codex]\n',
                 tools={"codex.md": "TEAM OWN CODEX GUIDE"})
    dest = _compose(project, leaf)[0]
    assert (dest / "tools" / "codex.md").read_text() == "TEAM OWN CODEX GUIDE"


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
    pins = {"codex": CODEX_PIN, "venn": VENN_PIN, "openai": OPENAI_PIN}
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
    deps = [_dep("codex", install={"npm": ["@openai/codex@0.142.0"]}),
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
    base = [_dep("codex", install={"npm": ["@openai/codex@0.142.0"]})]
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
    check: "command -v codex >/dev/null 2>&1 && { if [ \\"${BOBI_AUTH:-api_key}\\" != \\"subscription\\" ] && [ -n \\"${OPENAI_API_KEY:-}\\" ]; then mkdir -p ~/.codex && python3 -c 'import json, os, pathlib; p=pathlib.Path.home()/\\".codex\\"/\\"auth.json\\"; p.write_text(json.dumps({\\"OPENAI_API_KEY\\": os.environ[\\"OPENAI_API_KEY\\"]})+\\"\\\\n\\"); p.chmod(0o600)'; fi; if [ -f ~/.codex/auth.json ]; then if [ \\"${BOBI_AUTH:-api_key}\\" = \\"subscription\\" ] && python3 -c 'import json, pathlib, sys; p=pathlib.Path.home()/\\".codex\\"/\\"auth.json\\"; data=json.loads(p.read_text()); sys.exit(0 if isinstance(data, dict) and \\"OPENAI_API_KEY\\" in data else 1)'; then false; else python3 -c 'import subprocess, sys; sys.exit(subprocess.run([\\"codex\\", \\"exec\\", \\"-s\\", \\"read-only\\", \\"--skip-git-repo-check\\", \\"reply OK\\"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=8).returncode)'; fi; elif [ \\"${BOBI_VERIFY_PHASE:-}\\" = \\"build\\" ]; then codex --version >/dev/null 2>&1; else false; fi; }"
    fix: "npm install -g @openai/codex@0.142.0 && { if [ \\"${BOBI_AUTH:-api_key}\\" != \\"subscription\\" ] && [ -n \\"${OPENAI_API_KEY:-}\\" ]; then mkdir -p ~/.codex && python3 -c 'import json, os, pathlib; p=pathlib.Path.home()/\\".codex\\"/\\"auth.json\\"; p.write_text(json.dumps({\\"OPENAI_API_KEY\\": os.environ[\\"OPENAI_API_KEY\\"]})+\\"\\\\n\\"); p.chmod(0o600)'; else codex auth login || echo 'Set OPENAI_API_KEY in run/.env or run codex auth login'; fi; }"
  - name: venn
    why: "Reach external services (email, calendar, CRM) via the Venn CLI (tools/venn.md). Auth via VENN_API_KEY."
    check: "command -v venn >/dev/null 2>&1 && venn --help >/dev/null 2>&1"
    fix: "python3 -m venv /opt/venn-cli && /opt/venn-cli/bin/pip install venn-cli==0.2.0 && ln -sf /opt/venn-cli/bin/venn /usr/local/bin/venn && echo 'Set VENN_API_KEY in run/.env'"
build:
  apt: [nodejs, npm, python3-venv]
  npm: ["@openai/codex@0.142.0"]
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
