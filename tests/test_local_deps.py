"""Unit tests for local dependency materialization (#428 Stage 5).

Covers the `--with-deps` orchestration with injected runners (no real brain, no
host mutation): the plan/idempotency partition, runtime-tier verification, sudo
detection, the host-adapting install prompt, and the re-verify-is-truth rule in
`materialize_dependency`. The live end-to-end acceptance (a real brain installing
a real guide-only dep on the host) is `tests/integration/test_local_deps_e2e.py`.
"""

from __future__ import annotations

from bobi import local_deps
from bobi.local_deps import (
    DepPlan,
    build_install_prompt,
    install_dependencies,
    materialize_dependency,
    plan_dependencies,
)
from bobi.tool_library import Dependency


# --- helpers ----------------------------------------------------------------


def _dep(name="widget", success="command -v widget", guide="", install=None):
    return Dependency(name=name, success=success, guide=guide,
                      install=install or {})


def _shell(rc_by_dep, *, sink=None):
    """Fake shell runner. `rc_by_dep` is a flat rc or a map keyed on a substring
    of the success command (the dep's check), so different deps report different
    verdicts. Records the phase each invocation carries."""
    def _run(cmd, env, timeout):
        if sink is not None:
            sink.append((cmd, env.get("BOBI_BRAIN"), env.get("BOBI_VERIFY_PHASE")))
        if isinstance(rc_by_dep, int):
            return rc_by_dep, "", ""
        for key, rc in rc_by_dep.items():
            if key in cmd:
                return rc, "", "" if rc == 0 else f"exit {rc}"
        return 1, "", "unmatched"
    return _run


def _agent(payload="", *, sink=None, raises=None):
    def _run(prompt, brain):
        if sink is not None:
            sink.append((prompt, brain))
        if raises is not None:
            raise raises
        return payload
    return _run


# --- _install_needs_sudo ----------------------------------------------------


def test_apt_install_needs_sudo():
    assert local_deps._install_needs_sudo(_dep(install={"apt": ["cowsay"]}))


def test_run_root_install_needs_sudo():
    assert local_deps._install_needs_sudo(
        _dep(install={"run_root": ["mkdir /opt/x"]}))


def test_npm_only_install_does_not_need_sudo():
    assert not local_deps._install_needs_sudo(_dep(install={"npm": ["pkg@1"]}))


def test_guide_only_dep_does_not_need_sudo():
    assert not local_deps._install_needs_sudo(_dep(guide="do the thing"))


# --- plan_dependencies ------------------------------------------------------


def test_satisfied_dep_is_skipped_not_queued():
    dep = _dep(name="present", success="command -v present")
    plan = plan_dependencies([dep], brain="claude", shell_runner=_shell(0))
    assert [p.dep.name for p in plan.satisfied] == ["present"]
    assert plan.todo == []
    assert plan.nothing_to_do


def test_unsatisfied_materializable_dep_is_queued():
    dep = _dep(name="missing", success="command -v missing",
               install={"npm": ["missing@1"]})
    plan = plan_dependencies([dep], brain="claude", shell_runner=_shell(1))
    assert plan.satisfied == []
    assert [p.dep.name for p in plan.todo] == ["missing"]
    assert not plan.needs_sudo  # npm-only


def test_unsatisfied_dep_with_no_recipe_is_unmaterializable():
    dep = _dep(name="orphan", success="command -v orphan")  # no install, no guide
    plan = plan_dependencies([dep], brain="claude", shell_runner=_shell(1))
    assert [p.dep.name for p in plan.unmaterializable] == ["orphan"]
    assert plan.todo == []
    assert not plan.nothing_to_do  # a declaration error must be surfaced


def test_plan_flags_sudo_when_any_todo_needs_it():
    deps = [
        _dep(name="a", success="command -v a", install={"npm": ["a@1"]}),
        _dep(name="b", success="command -v b", install={"apt": ["b"]}),
    ]
    plan = plan_dependencies(deps, brain="claude", shell_runner=_shell(1))
    assert plan.needs_sudo


def test_plan_verifies_in_runtime_phase():
    """Idempotency check must use the runtime tier, not the build tier (so a
    phase-gated contract like `codex` verifies against a real check)."""
    sink = []
    plan_dependencies([_dep()], brain="codex", shell_runner=_shell(0, sink=sink))
    assert sink and sink[0][2] == "runtime"
    assert sink[0][1] == "codex"


# --- build_install_prompt ---------------------------------------------------


def test_prompt_forbids_sudo_when_not_allowed():
    p = build_install_prompt(_dep(install={"apt": ["x"]}), host_os="macOS",
                             allow_sudo=False)
    assert "Do NOT use sudo" in p
    assert "macOS" in p


def test_prompt_permits_sudo_when_allowed():
    p = build_install_prompt(_dep(install={"apt": ["x"]}), host_os="Linux",
                             allow_sudo=True)
    assert "MAY use sudo" in p
    assert "Do NOT use sudo" not in p


def test_prompt_includes_install_pins_and_guide():
    dep = _dep(install={"npm": ["pkg@1.2.3"]}, guide="see the README")
    p = build_install_prompt(dep, host_os="Linux", allow_sudo=False)
    assert "pkg@1.2.3" in p
    assert "see the README" in p
    assert "adapt" in p.lower()  # the recipe-is-a-pin-not-verbatim instruction


# --- materialize_dependency: re-verify is the source of truth ---------------


def _plan(dep, needs_sudo=False):
    return DepPlan(dep=dep, satisfied=False, needs_sudo=needs_sudo)


def test_agent_success_but_failing_contract_is_a_failure():
    """An agent claiming victory on a half-install is caught by the re-verify."""
    dep = _dep(name="half", success="command -v half")
    r = materialize_dependency(
        _plan(dep), brain="claude", allow_sudo=False,
        agent_runner=_agent('{"ok": true, "ran": ["fake"], "notes": "did it"}'),
        shell_runner=_shell(1))  # contract still fails
    assert r.ok is False
    assert r.transcript == ["fake"]


def test_contract_passing_after_install_is_success():
    dep = _dep(name="good", success="command -v good")
    r = materialize_dependency(
        _plan(dep), brain="claude", allow_sudo=False,
        agent_runner=_agent('{"ok": true, "ran": ["brew install good"]}'),
        shell_runner=_shell(0))  # contract now passes
    assert r.ok is True
    assert r.transcript == ["brew install good"]


def test_agent_crash_is_nonfatal_failure():
    dep = _dep(name="boom", success="command -v boom")
    r = materialize_dependency(
        _plan(dep), brain="claude", allow_sudo=False,
        agent_runner=_agent(raises=RuntimeError("kaboom")),
        shell_runner=_shell(1))
    assert r.ok is False
    assert "crashed" in r.detail


def test_materialize_verifies_in_runtime_phase():
    sink = []
    dep = _dep(name="rt", success="command -v rt")
    materialize_dependency(
        _plan(dep), brain="codex", allow_sudo=False,
        agent_runner=_agent('{"ok": true, "ran": []}'),
        shell_runner=_shell(0, sink=sink))
    assert sink and sink[-1][2] == "runtime"


# --- install_dependencies ---------------------------------------------------


def test_install_dependencies_never_raises_and_reports_each():
    deps = [
        _plan(_dep(name="ok1", success="command -v ok1")),
        _plan(_dep(name="bad", success="command -v bad")),
    ]
    results = install_dependencies(
        deps, brain="claude", allow_sudo=False,
        agent_runner=_agent('{"ok": true, "ran": []}'),
        shell_runner=_shell({"ok1": 0, "bad": 1}))
    by_name = {r.dep: r.ok for r in results}
    assert by_name == {"ok1": True, "bad": False}
