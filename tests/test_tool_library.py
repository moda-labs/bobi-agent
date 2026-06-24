"""Tests for the reusable tool library (#416) — `kind: cli` + catalog/resolver.

Covers the §6 verification plan of `docs/specs/416-tool-library.md`:
  * pin de-dup across `from:` layers (the headline — proves three-place drift is
    gone),
  * expansion basics, local-wins guide, explicit-wins requires,
  * `tool_library` union across layers + key consumed at compose,
  * unknown entry + unsupported-kind (the `EXPANDERS` seam) errors,
  * pin lint (no floating refs; `requires.fix` pin agrees with `build` pin),
  * the #452-style regression bar: `tool_library: [...]` composes byte-identical
    to the same team with those surfaces hand-written inline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from modastack import compose, tool_library


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
    dest = dest or (project / ".modastack")
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
    assert CODEX_PIN in cfg["build"]["npm"]
    guide = (tool_library.CATALOG_DIR / "codex" / "guide.md").read_text()
    assert (dest / "tools" / "codex.md").read_text() == guide


def test_tool_library_key_consumed(project):
    leaf = _team(project, "solo",
                 'version: "1.0.0"\nentry_point: director\ntool_library: [codex]\n')
    cfg = _agent_yaml(_compose(project, leaf)[0])
    assert "tool_library" not in cfg


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


def test_unsupported_kind_raises_naming_owner(project, tmp_path, monkeypatch):
    """An entry whose kind has no registered EXPANDER raises ComposeError naming
    the kind + owning issue — the seam #398/#428 plug into, NOT a hardcoded
    `if kind != 'cli'`."""
    cat = tmp_path / "cat"
    _write(cat / "futuristic" / "tool.yaml", "kind: mcp\nrequires: []\nbuild: {}\n")
    _write(cat / "futuristic" / "guide.md", "x")
    monkeypatch.setattr(tool_library, "CATALOG_DIR", cat)

    with pytest.raises(compose.ComposeError) as ei:
        tool_library.expand({"tool_library": ["futuristic"]}, tmp_path / "dest")
    msg = str(ei.value)
    assert "mcp" in msg and "#398" in msg


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


def test_requires_fix_pin_agrees_with_build_pin():
    """The pin co-located in `requires.fix` and `build` within one entry must
    agree — the one remaining co-location is guarded, not scattered (§4.1)."""
    pins = {"codex": CODEX_PIN, "venn": VENN_PIN, "openai": OPENAI_PIN}
    for name, pin in pins.items():
        entry = tool_library.load_entry(name)
        build_text = yaml.dump(entry.build)
        assert pin in build_text, f"{name}: pin {pin} missing from build"
        fix_text = " ".join(r.get("fix", "") for r in entry.requires)
        assert pin in fix_text, f"{name}: requires.fix pin disagrees with build"


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
    check: "command -v codex >/dev/null 2>&1 && { test -n \\"${OPENAI_API_KEY:-}\\" || codex --version >/dev/null 2>&1; }"
    fix: "npm install -g @openai/codex@0.142.0 && (codex auth login || echo 'Set OPENAI_API_KEY in .modastack/.env')"
  - name: venn
    why: "Reach external services (email, calendar, CRM) via the Venn CLI (tools/venn.md). Auth via VENN_API_KEY."
    check: "command -v venn >/dev/null 2>&1 && venn --help >/dev/null 2>&1"
    fix: "python3 -m venv /opt/venn-cli && /opt/venn-cli/bin/pip install venn-cli==0.2.0 && ln -sf /opt/venn-cli/bin/venn /usr/local/bin/venn && echo 'Set VENN_API_KEY in .modastack/.env'"
build:
  npm: ["@openai/codex@0.142.0"]
  apt: [python3-venv]
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
