"""Tests for rendering team-shipped global instructions (#779).

The package-root ``AGENTS.md`` renders into the paths each brain natively
auto-loads (``~/AGENTS.md`` always; ``$CODEX_HOME/AGENTS.md`` for codex;
``$CLAUDE_CONFIG_DIR/CLAUDE.md`` for claude/gateway) inside a managed block:
foreign content survives, re-renders are idempotent, and a team that ships no
instructions removes a previously managed block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bobi.brain import instructions


# --- pure render -------------------------------------------------------------


def test_render_empty_content_from_scratch_is_empty():
    assert instructions.render_instructions("", "") == ""


def test_render_wraps_content_in_managed_block():
    out = instructions.render_instructions("", "# House rules\n\nBe kind.\n")
    assert out.startswith(instructions.MANAGED_BEGIN + "\n")
    assert out.endswith(instructions.MANAGED_END + "\n")
    assert "# House rules" in out


def test_render_preserves_foreign_content():
    existing = "# My own notes\n\nDo not lose me.\n"
    out = instructions.render_instructions(existing, "rules\n")
    assert out.startswith("# My own notes")
    assert "Do not lose me." in out
    assert out.index("Do not lose me.") < out.index(instructions.MANAGED_BEGIN)
    assert "rules" in out


def test_render_is_idempotent():
    existing = "# foreign\n"
    once = instructions.render_instructions(existing, "rules\n")
    twice = instructions.render_instructions(once, "rules\n")
    assert once == twice


def test_render_replaces_stale_managed_content():
    first = instructions.render_instructions("# foreign\n", "old rules\n")
    out = instructions.render_instructions(first, "new rules\n")
    assert "old rules" not in out
    assert "new rules" in out
    assert "# foreign" in out
    assert out.count(instructions.MANAGED_BEGIN) == 1


def test_render_empty_content_removes_block_keeps_foreign():
    existing = instructions.render_instructions("# foreign\n", "rules\n")
    out = instructions.render_instructions(existing, "")
    assert instructions.MANAGED_BEGIN not in out
    assert "rules" not in out
    assert out == "# foreign\n"


def test_render_drops_marker_lines_inside_content():
    """A team's AGENTS.md quoting the sentinels (documenting this mechanism)
    must not terminate the block early - an embedded END would reclassify the
    rest of the old block as foreign on the next re-render."""
    content = (
        "rules\n" + instructions.MANAGED_END + "\n"
        + instructions.MANAGED_BEGIN + "\nmore rules\n"
    )
    once = instructions.render_instructions("# foreign\n", content)
    assert once.count(instructions.MANAGED_BEGIN) == 1
    assert once.count(instructions.MANAGED_END) == 1
    twice = instructions.render_instructions(once, content)
    assert twice == once
    # A content change replaces the whole block - nothing leaked to foreign.
    updated = instructions.render_instructions(once, "fresh\n")
    assert "more rules" not in updated
    assert "# foreign" in updated


def test_render_unclosed_begin_marker_drops_to_eof():
    """A truncated managed block (BEGIN with no END) must not leak stale managed
    content into the foreign region and duplicate on re-render."""
    existing = (
        "# foreign\n" + instructions.MANAGED_BEGIN + "\nstale managed text\n"
    )
    out = instructions.render_instructions(existing, "fresh\n")
    assert "stale managed text" not in out
    assert "# foreign" in out
    assert "fresh" in out


# --- disk writer -------------------------------------------------------------


def test_write_instructions_creates_parents_and_is_noop_when_unchanged(tmp_path):
    target = tmp_path / "deep" / "AGENTS.md"
    instructions.write_instructions(target, "rules\n")
    first = target.read_text()
    mtime = target.stat().st_mtime_ns
    instructions.write_instructions(target, "rules\n")
    assert target.read_text() == first
    assert target.stat().st_mtime_ns == mtime


def test_write_instructions_preserves_claude_memory_writes(tmp_path):
    """The agent's own #-memory appends to CLAUDE.md must survive re-renders."""
    target = tmp_path / "CLAUDE.md"
    instructions.write_instructions(target, "rules v1\n")
    with target.open("a") as f:
        f.write("\n- remembered: the deploy dashboard is at example.com\n")
    instructions.write_instructions(target, "rules v2\n")
    text = target.read_text()
    assert "remembered: the deploy dashboard" in text
    assert "rules v2" in text
    assert "rules v1" not in text


def test_write_is_atomic_no_temp_left_behind(tmp_path):
    target = tmp_path / "CLAUDE.md"
    instructions.write_instructions(target, "rules\n")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "CLAUDE.md"]
    assert leftovers == []


def test_has_managed_block_ignores_inline_mention(tmp_path):
    """A file that merely mentions the sentinel text (docs, a pasted log line)
    is not managed - it must not be re-written on every boot."""
    target = tmp_path / "AGENTS.md"
    target.write_text(f"see `{instructions.MANAGED_BEGIN}` for the marker\n")
    assert instructions.has_managed_block(target) is False


def test_has_managed_block(tmp_path):
    target = tmp_path / "AGENTS.md"
    assert instructions.has_managed_block(target) is False
    instructions.write_instructions(target, "rules\n")
    assert instructions.has_managed_block(target) is True
    instructions.write_instructions(target, "")
    assert instructions.has_managed_block(target) is False


# --- targets per brain kind ----------------------------------------------------


@pytest.fixture
def fake_homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path


def test_targets_always_include_home_agents_md(fake_homes):
    for kind in ("claude", "codex", "gateway", "stub"):
        assert Path(fake_homes / "home" / "AGENTS.md") in \
            instructions.instruction_targets(kind)


def test_targets_codex(fake_homes):
    # gateway-openai is the codex engine's alias: it must get the identical
    # targets (pre-#789 it matched neither branch and its teams' house rules
    # silently never reached $CODEX_HOME/AGENTS.md - the latent miss).
    for kind in ("codex", "gateway-openai"):
        assert instructions.instruction_targets(kind) == [
            fake_homes / "home" / "AGENTS.md",
            fake_homes / "codex" / "AGENTS.md",
        ]


def test_targets_claude_and_gateway_use_claude_config_dir(fake_homes):
    for kind in ("claude", "gateway"):
        assert instructions.instruction_targets(kind) == [
            fake_homes / "home" / "AGENTS.md",
            fake_homes / "claude" / "CLAUDE.md",
        ]


def test_targets_stub_is_home_only(fake_homes):
    assert instructions.instruction_targets("stub") == [
        fake_homes / "home" / "AGENTS.md",
    ]


def test_claude_config_dir_defaults_to_dot_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert instructions.claude_config_dir() == tmp_path / ".claude"


# --- boot-hook orchestration ---------------------------------------------------


def _project_with_package(tmp_path, agents_md: str | None) -> Path:
    project = tmp_path / "run"
    (project / "package").mkdir(parents=True)
    if agents_md is not None:
        (project / "package" / "AGENTS.md").write_text(agents_md)
    return project


def test_render_team_instructions_writes_all_targets(fake_homes):
    project = _project_with_package(fake_homes, "# House rules\n")
    written = instructions.render_team_instructions(project, brain_kind="codex")
    assert written == [
        fake_homes / "home" / "AGENTS.md",
        fake_homes / "codex" / "AGENTS.md",
    ]
    for p in written:
        text = p.read_text()
        assert instructions.MANAGED_BEGIN in text
        assert "# House rules" in text


def test_render_team_instructions_without_package_file_touches_nothing(fake_homes):
    project = _project_with_package(fake_homes, None)
    written = instructions.render_team_instructions(project, brain_kind="claude")
    assert written == []
    assert not (fake_homes / "home" / "AGENTS.md").exists()
    assert not (fake_homes / "claude" / "CLAUDE.md").exists()


def test_render_team_instructions_removal_lifecycle(fake_homes):
    """A team that drops its AGENTS.md cleans the previously managed block."""
    project = _project_with_package(fake_homes, "# House rules\n")
    instructions.render_team_instructions(project, brain_kind="claude")
    claude_md = fake_homes / "claude" / "CLAUDE.md"
    with claude_md.open("a") as f:
        f.write("\n- my memory\n")

    (project / "package" / "AGENTS.md").unlink()
    written = instructions.render_team_instructions(project, brain_kind="claude")

    assert set(written) == {fake_homes / "home" / "AGENTS.md", claude_md}
    assert instructions.MANAGED_BEGIN not in claude_md.read_text()
    assert "my memory" in claude_md.read_text()
    assert (fake_homes / "home" / "AGENTS.md").read_text() == ""


def test_render_team_instructions_cleans_previous_brain_target(fake_homes):
    """A brain-kind switch must not leave the old brain reading retired rules."""
    project = _project_with_package(fake_homes, "# House rules\n")
    instructions.render_team_instructions(project, brain_kind="codex")
    codex_md = fake_homes / "codex" / "AGENTS.md"
    assert instructions.has_managed_block(codex_md)

    written = instructions.render_team_instructions(project, brain_kind="claude")

    assert codex_md in written
    assert not instructions.has_managed_block(codex_md)
    assert instructions.has_managed_block(fake_homes / "claude" / "CLAUDE.md")


def test_render_team_instructions_skips_undecodable_foreign_target(fake_homes):
    """One stray non-UTF-8 byte in an operator dotfile must not crash boot."""
    project = _project_with_package(fake_homes, "# House rules\n")
    home_md = fake_homes / "home" / "AGENTS.md"
    home_md.parent.mkdir(parents=True, exist_ok=True)
    home_md.write_bytes(b"caf\xe9 notes\n")  # latin-1, not valid UTF-8

    written = instructions.render_team_instructions(project, brain_kind="claude")

    assert home_md not in written
    assert home_md.read_bytes() == b"caf\xe9 notes\n"
    assert instructions.has_managed_block(fake_homes / "claude" / "CLAUDE.md")


def test_render_team_instructions_defaults_to_process_brain(fake_homes, monkeypatch):
    monkeypatch.setenv("BOBI_BRAIN", "codex")
    project = _project_with_package(fake_homes, "rules\n")
    written = instructions.render_team_instructions(project)
    assert fake_homes / "codex" / "AGENTS.md" in written
