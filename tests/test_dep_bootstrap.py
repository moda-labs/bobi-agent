"""Unit tests for the bootstrap-agent harness (#428 Stage 2).

Covers the harness orchestration with injected runners (no real brain, no
container): materialization (pinned install vs agent-from-guide), the recipe
verdict parse, per-brain agentic preflight in the build tier, and the
snapshot gate. Also the `resolve_team_dependencies` seam that feeds the harness
a team's full from-chain dependency set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bobi import dep_bootstrap, tool_library
from bobi.dep_bootstrap import (
    DependencyOutcome,
    MaterializeResult,
    ResolvedRecipe,
    bootstrap,
    build_bootstrap_prompt,
    materialize,
    preflight,
)
from bobi.tool_library import Dependency


# --- helpers ----------------------------------------------------------------


def _dep(name="widget", success="command -v widget", guide="", install=None):
    return Dependency(name=name, success=success, guide=guide,
                      install=install or {})


def _agent_emitting(payload_line: str, *, sink: list | None = None):
    """A fake agent runner that returns a fixed final text and records calls."""
    def _run(prompt: str, brain: str) -> str:
        if sink is not None:
            sink.append((prompt, brain))
        return payload_line
    return _run


def _shell(rc_by_brain: dict | int, *, sink: list | None = None):
    """A fake shell runner. `rc_by_brain` is a flat rc, or a map keyed on the
    BOBI_BRAIN env each invocation carries."""
    def _run(cmd: str, env: dict, timeout: float):
        if sink is not None:
            sink.append((cmd, env.get("BOBI_BRAIN"), env.get("BOBI_VERIFY_PHASE")))
        rc = rc_by_brain if isinstance(rc_by_brain, int) else rc_by_brain[env["BOBI_BRAIN"]]
        return rc, "", "" if rc == 0 else f"exit {rc}"
    return _run


# --- ResolvedRecipe ---------------------------------------------------------


def test_recipe_from_install_adopts_verbatim():
    r = ResolvedRecipe.from_install({"apt": ["nodejs", "npm"], "npm": ["@openai/codex@0.142.0"]})
    assert r.apt == ["nodejs", "npm"]
    assert r.npm == ["@openai/codex@0.142.0"]
    assert r.run_root == [] and r.run == []
    assert not r.is_empty


def test_recipe_coerces_scalar_and_drops_blanks_and_unknown_keys():
    r = ResolvedRecipe.from_agent(
        {"run_root": "python3 -m venv /opt/x", "apt": ["", "  ", "curl"],
         "bogus": ["ignored"]})
    assert r.run_root == ["python3 -m venv /opt/x"]
    assert r.apt == ["curl"]
    assert r.to_dict() == {"apt": ["curl"], "npm": [],
                           "run_root": ["python3 -m venv /opt/x"], "run": []}


def test_empty_recipe_is_empty():
    assert ResolvedRecipe().is_empty
    assert ResolvedRecipe.from_agent({}).is_empty


# --- materialize: pinned install path (no agent) ----------------------------


def test_materialize_install_dep_uses_recipe_verbatim_and_never_calls_agent():
    calls: list = []
    dep = _dep(install={"apt": ["python3-venv"], "run_root": ["make-venv"]})
    m = materialize(dep, agent_runner=_agent_emitting("SHOULD-NOT-RUN", sink=calls),
                    brains=["claude", "codex"])
    assert m.ok and not m.agent_used
    assert m.recipe.apt == ["python3-venv"]
    assert m.recipe.run_root == ["make-venv"]
    assert calls == []  # agent must not run for a pinned install


# --- materialize: guide path (agent) ----------------------------------------


def test_materialize_guide_dep_runs_agent_and_captures_recipe():
    sink: list = []
    line = ('done. {"ok": true, "recipe": {"apt": [], "npm": ["gstack@1.2.3"], '
            '"run_root": [], "run": ["gstack init"]}, "notes": "installed"}')
    dep = _dep(guide="https://example/gstack#install", install=None)
    m = materialize(dep, agent_runner=_agent_emitting(line, sink=sink),
                    brains=["claude", "codex"])
    assert m.ok and m.agent_used
    assert m.recipe.npm == ["gstack@1.2.3"]
    assert m.recipe.run == ["gstack init"]
    assert m.notes == "installed"
    # materialization happens ONCE, under the primary brain
    assert len(sink) == 1 and sink[0][1] == "claude"


def test_materialize_guide_dep_agent_reports_failure():
    line = '{"ok": false, "recipe": {"apt": [], "npm": [], "run_root": [], "run": []}}'
    m = materialize(_dep(guide="g"), agent_runner=_agent_emitting(line),
                    brains=["claude"])
    assert not m.ok and m.agent_used
    assert "could not satisfy" in m.error


def test_materialize_guide_dep_no_parseable_recipe_fails():
    m = materialize(_dep(guide="g"),
                    agent_runner=_agent_emitting("I installed it, trust me."),
                    brains=["claude"])
    assert not m.ok
    assert "no parseable recipe" in m.error


def test_materialize_guide_dep_success_but_empty_recipe_fails():
    # An agent can't claim victory with nothing to freeze — a snapshot from an
    # empty recipe would silently drop the dependency.
    line = '{"ok": true, "recipe": {"apt": [], "npm": [], "run_root": [], "run": []}}'
    m = materialize(_dep(guide="g"), agent_runner=_agent_emitting(line),
                    brains=["claude"])
    assert not m.ok
    assert "empty recipe" in m.error


def test_materialize_dep_without_install_or_guide_fails():
    m = materialize(_dep(guide=""), agent_runner=_agent_emitting("x"),
                    brains=["claude"])
    assert not m.ok and not m.agent_used
    assert "neither 'install' nor 'guide'" in m.error


def test_materialize_agent_crash_is_a_failed_materialize():
    def _boom(prompt, brain):
        raise RuntimeError("kaboom")
    m = materialize(_dep(guide="g"), agent_runner=_boom, brains=["claude"])
    assert not m.ok and m.agent_used
    assert "crashed" in m.error


# --- preflight: per-brain build tier ----------------------------------------


def test_preflight_runs_once_per_brain_with_build_phase_and_brain_env():
    sink: list = []
    dep = _dep(success="command -v widget")
    results = preflight(dep, brains=["claude", "codex"],
                        shell_runner=_shell(0, sink=sink), base_env={})
    assert [r.brain for r in results] == ["claude", "codex"]
    assert all(r.ok for r in results)
    # every invocation carried build phase + its brain
    assert {b for _, b, _ in sink} == {"claude", "codex"}
    assert {phase for _, _, phase in sink} == {"build"}


def test_preflight_fails_when_any_brain_fails():
    dep = _dep(success="check")
    results = preflight(dep, brains=["claude", "codex"],
                        shell_runner=_shell({"claude": 0, "codex": 1}), base_env={})
    by_brain = {r.brain: r for r in results}
    assert by_brain["claude"].ok
    assert not by_brain["codex"].ok
    assert by_brain["codex"].detail  # carries a failure detail


def test_preflight_runner_exception_is_a_failed_check():
    def _boom(cmd, env, timeout):
        raise OSError("no shell")
    results = preflight(_dep(), brains=["claude"], shell_runner=_boom, base_env={})
    assert not results[0].ok
    assert "runner error" in results[0].detail


# --- DependencyOutcome / report gating --------------------------------------


def test_outcome_ok_requires_materialize_and_all_preflight_pass():
    m_ok = MaterializeResult("d", ResolvedRecipe(apt=["x"]), agent_used=False, ok=True)
    good = DependencyOutcome("d", m_ok, [dep_bootstrap.PreflightResult("d", "claude", True)])
    assert good.ok
    mixed = DependencyOutcome("d", m_ok, [
        dep_bootstrap.PreflightResult("d", "claude", True),
        dep_bootstrap.PreflightResult("d", "codex", False)])
    assert not mixed.ok


def test_outcome_with_no_preflight_is_not_a_vacuous_pass():
    # materialize failed → preflight skipped → must be a failure, not a pass.
    m_bad = MaterializeResult("d", ResolvedRecipe(), agent_used=True, ok=False,
                              error="boom")
    assert not DependencyOutcome("d", m_bad, []).ok


# --- bootstrap orchestrator (full gate) -------------------------------------


def test_bootstrap_all_green_passes_the_gate():
    deps = [
        _dep(name="codexcli", success="codex --version",
             install={"npm": ["@openai/codex@0.142.0"]}),
        _dep(name="venncli", success="venn --help",
             install={"run_root": ["make-venn"]}),
    ]
    report = bootstrap(deps, brains=["claude", "codex"],
                       agent_runner=_agent_emitting("unused"),
                       shell_runner=_shell(0), base_env={})
    assert report.ok
    assert report.summary() == "2/2 dependencies bootstrapped"
    assert all(not o.materialize.agent_used for o in report.outcomes)


def test_bootstrap_failed_preflight_fails_the_gate_and_lists_failure():
    deps = [_dep(name="broken", success="false", install={"apt": ["x"]})]
    report = bootstrap(deps, brains=["claude"],
                       shell_runner=_shell(1), base_env={})
    assert not report.ok
    fails = report.failures()
    assert len(fails) == 1 and fails[0].dep == "broken"


def test_bootstrap_failed_materialize_skips_preflight():
    calls: list = []
    deps = [_dep(name="noguide", guide="", install=None)]
    report = bootstrap(deps, brains=["claude"],
                       agent_runner=_agent_emitting("x"),
                       shell_runner=_shell(0, sink=calls), base_env={})
    assert not report.ok
    assert report.outcomes[0].preflight == []
    assert calls == []  # preflight never ran for the unmaterializable dep


def test_bootstrap_requires_at_least_one_brain():
    with pytest.raises(ValueError, match="at least one target brain"):
        bootstrap([_dep()], brains=[])


def test_bootstrap_mixes_install_and_guide_deps():
    line = '{"ok": true, "recipe": {"npm": ["g@1.0.0"]}, "notes": ""}'
    deps = [
        _dep(name="pinned", success="ok", install={"apt": ["a"]}),
        _dep(name="loose", success="ok", guide="how-to"),
    ]
    report = bootstrap(deps, brains=["claude"],
                       agent_runner=_agent_emitting(line),
                       shell_runner=_shell(0), base_env={})
    assert report.ok
    outcomes = {o.dep: o for o in report.outcomes}
    assert not outcomes["pinned"].materialize.agent_used
    assert outcomes["loose"].materialize.agent_used
    assert outcomes["loose"].materialize.recipe.npm == ["g@1.0.0"]


# --- prompt -----------------------------------------------------------------


def test_bootstrap_prompt_carries_success_guide_brains_and_pin_rule():
    dep = _dep(name="gstack", success="screenshot works", guide="the guide text")
    prompt = build_bootstrap_prompt(dep, ["claude", "codex"])
    assert "gstack" in prompt
    assert "screenshot works" in prompt
    assert "the guide text" in prompt
    assert "claude, codex" in prompt
    assert "PINNED" in prompt
    assert '"ok": true' in prompt  # the JSON recipe contract is spelled out


# --- resolve_team_dependencies (the harness input seam) ---------------------


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# --- render_team_deps: the image-build seam (#428 Stage 3) ------------------


def _team_at(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / "agents" / name
    _write(d / "agent.yaml", body)
    return d


def test_render_team_deps_bakes_guide_recipe_and_stamps_dep_hash(tmp_path: Path):
    from bobi.build_render import DEP_LIST_STAMP, TEAM_DEPS_STAMP
    from bobi.tool_library import dependency_list_hash, resolve_team_dependencies

    team = _team_at(tmp_path, "gt",
                    "agent: gt\ntool_library:\n"
                    "  - name: gstack\n"
                    "    guide: 'https://example/gstack#install'\n"
                    "    success: 'gstack --version'\n")
    line = ('{"ok": true, "recipe": {"npm": ["gstack@1.2.3"], '
            '"run": ["gstack init"]}, "notes": "ok"}')
    script = dep_bootstrap.render_team_deps(
        team, tmp_path, brains=["claude"],
        agent_runner=_agent_emitting(line), shell_runner=_shell(0))
    assert script is not None
    assert "gstack@1.2.3" in script and "gstack init" in script
    h = dependency_list_hash(resolve_team_dependencies(team, tmp_path))
    assert f"{h} > {DEP_LIST_STAMP}" in script
    assert f"> {TEAM_DEPS_STAMP}" in script


def test_render_team_deps_pinned_install_needs_no_agent(tmp_path: Path):
    # A pinned install bakes via compose's build merge; the agent never runs.
    calls: list = []
    team = _team_at(tmp_path, "pt",
                    "agent: pt\ntool_library:\n"
                    "  - name: tool\n"
                    "    success: 'tool --version'\n"
                    "    install:\n      npm: ['tool@2.0.0']\n")
    script = dep_bootstrap.render_team_deps(
        team, tmp_path, brains=["claude", "codex"],
        agent_runner=_agent_emitting("UNUSED", sink=calls), shell_runner=_shell(0))
    assert script is not None and "tool@2.0.0" in script
    assert calls == []  # pinned install → no bootstrap agent


def test_render_team_deps_generic_team_returns_none(tmp_path: Path):
    team = _team_at(tmp_path, "plain", "agent: plain\n")
    assert dep_bootstrap.render_team_deps(team, tmp_path, brains=["claude"]) is None


def test_render_team_deps_failed_gate_raises(tmp_path: Path):
    team = _team_at(tmp_path, "gt",
                    "agent: gt\ntool_library:\n"
                    "  - name: gstack\n    guide: g\n    success: 'gstack --version'\n")
    line = '{"ok": true, "recipe": {"npm": ["gstack@1.2.3"]}}'
    with pytest.raises(dep_bootstrap.BootstrapError, match="gstack"):
        dep_bootstrap.render_team_deps(
            team, tmp_path, brains=["claude"],
            agent_runner=_agent_emitting(line), shell_runner=_shell(1))  # preflight fails


def test_team_has_bake_covers_guide_declarative_and_generic(tmp_path: Path):
    guide = _team_at(tmp_path, "g",
                     "agent: g\ntool_library:\n  - name: x\n    guide: g\n    success: s\n")
    pinned = _team_at(tmp_path, "p",
                      "agent: p\nbuild:\n  apt: [git]\n")
    generic = _team_at(tmp_path, "plain", "agent: plain\n")
    assert dep_bootstrap.team_has_bake(guide, tmp_path) is True
    assert dep_bootstrap.team_has_bake(pinned, tmp_path) is True
    assert dep_bootstrap.team_has_bake(generic, tmp_path) is False


# --- CLI (image-build entry points) -----------------------------------------


def test_cli_check_matches_team_has_bake(tmp_path: Path):
    guide = _team_at(tmp_path, "g",
                     "agent: g\ntool_library:\n  - name: x\n    guide: g\n    success: s\n")
    generic = _team_at(tmp_path, "plain", "agent: plain\n")
    assert dep_bootstrap._main([str(guide), "--check"]) == 0
    assert dep_bootstrap._main([str(generic), "--check"]) == 2


def test_cli_needs_agent(tmp_path: Path):
    # Guide-only dep → needs the agent (exit 0); pinned/declarative → not (exit 2).
    guide = _team_at(tmp_path, "g",
                     "agent: g\ntool_library:\n  - name: x\n    guide: g\n    success: s\n")
    pinned = _team_at(tmp_path, "p",
                      "agent: p\ntool_library:\n  - name: y\n    success: s\n"
                      "    install:\n      npm: ['y@1']\n")
    generic = _team_at(tmp_path, "plain", "agent: plain\n")
    assert dep_bootstrap._main([str(guide), "--needs-agent"]) == 0
    assert dep_bootstrap._main([str(pinned), "--needs-agent"]) == 2
    assert dep_bootstrap._main([str(generic), "--needs-agent"]) == 2


def test_cli_print_dep_hash(tmp_path: Path, capsys):
    from bobi.tool_library import dependency_list_hash, resolve_team_dependencies

    team = _team_at(tmp_path, "g",
                    "agent: g\ntool_library:\n  - name: x\n    guide: g\n    success: s\n")
    assert dep_bootstrap._main([str(team), "--print-dep-hash"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == dependency_list_hash(resolve_team_dependencies(team, tmp_path))


def test_cli_render_writes_generic_team_writes_nothing(tmp_path: Path):
    generic = _team_at(tmp_path, "plain", "agent: plain\n")
    out = tmp_path / "out.sh"
    assert dep_bootstrap._main([str(generic), "--render", str(out)]) == 0
    assert not out.exists()  # generic team → no script


def test_resolve_team_dependencies_merges_from_chain(tmp_path: Path):
    (tmp_path / "agents").mkdir()
    _write(tmp_path / "agents" / "base" / "agent.yaml",
           "agent: base\ntool_library:\n  - name: alpha\n    success: check-alpha\n")
    _write(tmp_path / "agents" / "leaf" / "agent.yaml",
           "agent: leaf\nfrom: base\ntool_library:\n"
           "  - name: beta\n    success: check-beta\n")
    deps = tool_library.resolve_team_dependencies(
        tmp_path / "agents" / "leaf", tmp_path)
    names = {d.name for d in deps}
    assert names == {"alpha", "beta"}  # inherited + own, unioned across the chain
