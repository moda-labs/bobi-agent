"""Tests for launch lineage - the chain of runs that led to a launch.

The regression test at the bottom is the reported incident's own shape: an
``adhoc`` chain, a fresh run key every generation, nothing but the spend
governor between one trigger and 50 runs.
"""

import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from bobi import paths
from bobi.launch_lineage import (
    ADHOC_WORKFLOW,
    DEFAULT_MAX_LAUNCH_DEPTH,
    LINEAGE_ENV_VAR,
    MAX_DEPTH_ENV_VAR,
    LaunchBlockedError,
    LineageLink,
    current_lineage,
    decode,
    encode,
    extend,
    max_launch_depth,
    render,
    stamp,
    strip,
)


def _link(session, workflow=ADHOC_WORKFLOW, run_key="rk"):
    return LineageLink(session=session, workflow=workflow, run_key=run_key)


@contextmanager
def _process_env(env):
    """Run the block with *env* as the whole process environment.

    Lineage rides in the environment, so a chain is only reproducible by
    swapping the environment wholesale between generations.
    """
    with patch.dict(os.environ, env, clear=True):
        yield


@pytest.fixture(autouse=True)
def _reset_dropped_dedup():
    """``_DROPPED_SEEN`` is process-global, so one test's junk value would
    otherwise silence the next test that uses the same one."""
    import bobi.launch_lineage as mod
    mod._DROPPED_SEEN.clear()
    yield
    mod._DROPPED_SEEN.clear()


@pytest.fixture
def project(tmp_path):
    """A minimal runtime root with package/agent.yaml and state/."""
    paths.package_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    paths.agent_yaml_path(tmp_path).write_text("entry_point: x\n")
    paths.state_dir(tmp_path)
    paths.bind_root(tmp_path)
    yield tmp_path
    paths.bind_root(None)


class TestEncoding:
    def test_round_trip(self):
        chain = (_link("director", workflow=""), _link("adhoc-1"))
        assert decode(encode(chain)) == chain

    def test_absent_is_root(self):
        assert decode(None) == ()
        assert decode("") == ()

    @pytest.mark.parametrize("raw", [
        "not json",
        "{}",
        '{"chain": 3}',
        '[{"session": "a"}',
        '["bare string"]',
        '[{"workflow": "adhoc"}]',      # no session - the registry's key
        json.dumps([{"session": "", "workflow": "adhoc", "run_key": "r"}]),
    ])
    def test_junk_is_tolerated_as_root(self, raw):
        assert decode(raw) == ()

    def test_unparseable_emits_dropped_event(self, project):
        with patch("bobi.events.publish.post_event") as post:
            decode("not json", project_path=project)
        topics = [call.args[0] for call in post.call_args_list]
        assert "system/launch.lineage.dropped" in topics

    def test_absent_does_not_emit(self, project):
        with patch("bobi.events.publish.post_event") as post:
            decode(None, project_path=project)
        post.assert_not_called()

    def test_dropped_event_is_emitted_once_per_bad_value(self, project):
        """A corrupted chain is inherited fleet-wide and read on every launch
        and every session start; emitting per read would flood the bus during
        the very incident where the guard is failing open."""
        with patch("bobi.events.publish.post_event") as post:
            for _ in range(5):
                decode("not json", project_path=project)
        dropped = [c for c in post.call_args_list
                   if c.args[0] == "system/launch.lineage.dropped"]
        assert len(dropped) == 1

    def test_a_different_bad_value_still_reports(self, project):
        with patch("bobi.events.publish.post_event") as post:
            decode("not json", project_path=project)
            decode('{"other": "junk"}', project_path=project)
        dropped = [c for c in post.call_args_list
                   if c.args[0] == "system/launch.lineage.dropped"]
        assert len(dropped) == 2

    def test_reads_from_environment(self):
        chain = (_link("director"),)
        with _process_env({LINEAGE_ENV_VAR: encode(chain)}):
            assert current_lineage() == chain

    def test_stamp_and_strip(self):
        env: dict[str, str] = {}
        stamp(env, (_link("a"),))
        assert env[LINEAGE_ENV_VAR]
        strip(env)
        assert LINEAGE_ENV_VAR not in env

    def test_strip_is_idempotent(self):
        env: dict[str, str] = {}
        strip(env)
        assert LINEAGE_ENV_VAR not in env


class TestRender:
    def test_renders_session_names_root_first(self):
        chain = (_link("director"), _link("wf-x-1042"), _link("adhoc-3f9c"))
        assert render(chain) == "director > wf-x-1042 > adhoc-3f9c"

    def test_empty_chain_renders_as_root(self):
        assert render(()) == "(root)"


class TestExtendDepth:
    def test_root_launch_allowed(self):
        assert extend((), _link("a"), max_depth=8) == (_link("a"),)

    def test_allowed_one_below_the_cap(self):
        chain = tuple(_link(f"g{i}") for i in range(7))
        assert len(extend(chain, _link("g7"), max_depth=8)) == 8

    def test_refused_at_the_boundary(self):
        chain = tuple(_link(f"g{i}") for i in range(8))
        with pytest.raises(LaunchBlockedError) as exc:
            extend(chain, _link("g8"), max_depth=8)
        assert exc.value.rule == "depth"

    def test_depth_approaching_fires_one_below_the_cap(self, project):
        chain = tuple(_link(f"g{i}") for i in range(6))
        with patch("bobi.events.publish.post_event") as post:
            extend(chain, _link("g6"), max_depth=8, project_path=project)
        topics = [call.args[0] for call in post.call_args_list]
        assert "system/launch.depth.approaching" in topics

    def test_depth_approaching_silent_well_below_the_cap(self, project):
        with patch("bobi.events.publish.post_event") as post:
            extend((), _link("a"), max_depth=8, project_path=project)
        post.assert_not_called()

    def test_blocked_launch_emits_blocked_event_with_rule(self, project):
        chain = tuple(_link(f"g{i}") for i in range(8))
        with patch("bobi.events.publish.post_event") as post:
            with pytest.raises(LaunchBlockedError):
                extend(chain, _link("g8"), max_depth=8, project_path=project)
        blocked = [c for c in post.call_args_list
                   if c.args[0] == "system/launch.blocked"]
        assert blocked, "a refusal the operator cannot see is this ticket's bug"
        payload = blocked[0].args[1]
        assert payload["rule"] == "depth"
        assert payload["lineage"] == render(chain)
        assert payload["text"]


class TestExtendSelfRecursion:
    def test_named_workflow_already_in_chain_is_refused(self):
        chain = (_link("director", workflow=""),
                 _link("wf-issue-lifecycle-1042", workflow="issue-lifecycle"))
        with pytest.raises(LaunchBlockedError) as exc:
            extend(chain, _link("wf-issue-lifecycle-1099",
                                workflow="issue-lifecycle"), max_depth=8)
        assert exc.value.rule == "recursion"

    def test_adhoc_nesting_is_allowed(self):
        """The role prompts teach ``-w adhoc --wait`` delegation; depth governs it."""
        chain = (_link("adhoc-1"), _link("adhoc-2"))
        assert len(extend(chain, _link("adhoc-3"), max_depth=8)) == 3

    def test_different_named_workflows_nest(self):
        chain = (_link("wf-a", workflow="a"),)
        assert extend(chain, _link("wf-b", workflow="b"), max_depth=8)

    def test_bare_persistent_session_does_not_match(self):
        """A manager's empty workflow must not collide with another manager's."""
        chain = (_link("director", workflow=""),)
        assert extend(chain, _link("wf-x", workflow=""), max_depth=8)

    def test_recursion_beats_depth_when_both_apply(self):
        chain = tuple(_link(f"g{i}", workflow="loop") for i in range(9))
        with pytest.raises(LaunchBlockedError) as exc:
            extend(chain, _link("g9", workflow="loop"), max_depth=8)
        assert exc.value.rule == "recursion"


class TestRefusalMessage:
    """The reader is an LLM. A bare refusal becomes a launch storm."""

    def _blocked(self, chain, link, max_depth=8):
        with pytest.raises(LaunchBlockedError) as exc:
            extend(chain, link, max_depth=max_depth)
        return str(exc.value)

    @pytest.fixture
    def depth_message(self):
        chain = tuple(_link(f"g{i}") for i in range(8))
        return self._blocked(chain, _link("g8"))

    @pytest.fixture
    def recursion_message(self):
        chain = (_link("director", workflow=""),
                 _link("wf-issue-lifecycle-1042", workflow="issue-lifecycle"))
        return self._blocked(
            chain, _link("wf-issue-lifecycle-1099", workflow="issue-lifecycle"))

    def test_depth_names_the_rule_and_the_chain(self, depth_message):
        assert "depth" in depth_message
        assert "g0 > g1" in depth_message
        assert "max_launch_depth" in depth_message

    def test_recursion_names_the_rule_and_the_workflow(self, recursion_message):
        assert "self-recursion" in recursion_message
        assert "issue-lifecycle" in recursion_message
        assert "director > wf-issue-lifecycle-1042" in recursion_message

    @pytest.mark.parametrize("fixture_name", ["depth_message", "recursion_message"])
    def test_says_the_block_is_deterministic(self, fixture_name, request):
        message = request.getfixturevalue(fixture_name)
        assert "deterministic" in message
        assert "not a rate limit" in message

    @pytest.mark.parametrize("fixture_name", ["depth_message", "recursion_message"])
    @pytest.mark.parametrize("vector", ["new run key", "new task text",
                                        "-w adhoc", "waiting"])
    def test_forbids_each_retry_vector_by_name(self, fixture_name, vector, request):
        message = request.getfixturevalue(fixture_name)
        assert vector in message

    @pytest.mark.parametrize("fixture_name", ["depth_message", "recursion_message"])
    def test_gives_an_escalation_path(self, fixture_name, request):
        message = request.getfixturevalue(fixture_name)
        assert "report this message" in message

    @pytest.mark.parametrize("fixture_name", ["depth_message", "recursion_message"])
    @pytest.mark.parametrize("bypass", [LINEAGE_ENV_VAR, MAX_DEPTH_ENV_VAR])
    def test_never_hands_the_agent_the_bypass(self, fixture_name, bypass, request):
        """The docs state the guard's limits; the agent-facing text must not.

        Both variables are a bypass: clearing the chain resets it to root, and
        raising the cap lifts the ceiling. Naming either one to the agent that
        just hit the block is handing it the way around.
        """
        message = request.getfixturevalue(fixture_name)
        assert bypass not in message

    def test_depth_escalation_is_the_config_knob(self, depth_message):
        assert "agent.yaml" in depth_message

    def test_recursion_says_config_cannot_raise_it(self, recursion_message):
        assert "not raisable by config" in recursion_message


class TestMaxLaunchDepthConfig:
    def test_default_when_unset(self, project):
        assert max_launch_depth(project) == DEFAULT_MAX_LAUNCH_DEPTH

    def test_honours_agent_yaml(self, project):
        paths.agent_yaml_path(project).write_text(
            "entry_point: x\nmax_launch_depth: 3\n")
        assert max_launch_depth(project) == 3

    def test_zero_means_default(self, project):
        paths.agent_yaml_path(project).write_text(
            "entry_point: x\nmax_launch_depth: 0\n")
        assert max_launch_depth(project) == DEFAULT_MAX_LAUNCH_DEPTH

    def test_unreadable_config_falls_back_to_default(self, tmp_path):
        assert max_launch_depth(tmp_path / "nope") == DEFAULT_MAX_LAUNCH_DEPTH

    def test_env_override_is_honoured(self, project, monkeypatch):
        monkeypatch.setenv(MAX_DEPTH_ENV_VAR, "3")
        assert max_launch_depth(project) == 3

    def test_env_override_beats_agent_yaml(self, project, monkeypatch):
        """The operator's lever on a running fleet outranks the baked default."""
        paths.agent_yaml_path(project).write_text(
            "entry_point: x\nmax_launch_depth: 5\n")
        monkeypatch.setenv(MAX_DEPTH_ENV_VAR, "2")
        assert max_launch_depth(project) == 2

    @pytest.mark.parametrize("raw", ["", "   ", "eight", "0", "-1"])
    def test_unusable_env_override_falls_back(self, project, monkeypatch, raw):
        """A typo in a fleet-wide variable must not block the deployment."""
        paths.agent_yaml_path(project).write_text(
            "entry_point: x\nmax_launch_depth: 5\n")
        monkeypatch.setenv(MAX_DEPTH_ENV_VAR, raw)
        assert max_launch_depth(project) == 5

    def test_env_override_bounds_a_real_chain(self, project, monkeypatch):
        """End to end through extend(), not just the resolver."""
        monkeypatch.setenv(MAX_DEPTH_ENV_VAR, "2")
        chain = (_link("root"), _link("child"))
        with pytest.raises(LaunchBlockedError) as exc:
            extend(chain, _link("grandchild"),
                   max_depth=max_launch_depth(project))
        assert exc.value.rule == "depth"


class TestNoPersistence:
    """Option B carries lineage in the environment and nowhere else.

    ``Session.start()`` re-registers with a full ``asdict`` overwrite that
    zeroes 17 fields on any pre-existing entry. Not persisting lineage is what
    makes that irrelevant here - so assert the registry stays out of it.
    """

    def test_session_entry_has_no_lineage_field(self):
        from bobi.sdk import SessionEntry
        fields = set(SessionEntry.__dataclass_fields__)
        assert not fields & {"ancestry", "lineage", "parent", "root_run_key",
                             "parent_run_key", "depth"}

    def test_module_never_reads_the_registry(self):
        import bobi.launch_lineage as mod
        source = open(mod.__file__).read()
        assert "get_registry" not in source
        assert "SessionEntry" not in source


class TestSessionStartedCarriesTheChain:
    """Forensics ride this event instead of the registry, so BOTH emitters
    must carry the chain.

    There are two: ``subagent._emit_session_started`` (adhoc/persistent) and
    the workflow orchestrator's own. A detached non-persistent launch - the
    shape the incident was made of - takes the orchestrator's. Covering only
    one would show chains on adhoc runs and roots on workflow runs, and an
    operator would read the workflow runs as unrelated.
    """

    @pytest.fixture
    def chain_env(self):
        return {LINEAGE_ENV_VAR: encode((_link("director"), _link("child")))}

    def test_adhoc_emitter_carries_the_chain(self, chain_env):
        from bobi.subagent import _emit_session_started
        with patch("bobi.subagent._emit_lifecycle_event") as emit:
            with _process_env(chain_env):
                _emit_session_started("rk", "proj", "task", "rk", phase="adhoc")
        payload = emit.call_args[0][1]
        assert payload["lineage"] == "director > child"
        assert payload["depth"] == 2

    @pytest.mark.parametrize("module_name", [
        "bobi.subagent", "bobi.workflow.orchestrator",
    ])
    def test_every_session_started_emitter_splices_the_helper(self, module_name):
        """Both emitters must splice ``**lineage_fields()`` into the payload.

        Asserted on the AST of the actual ``agent/session.started`` call rather
        than on the file's text: a substring check would pass on a comment, a
        dead branch, or a call whose result is dropped, and the workflow half of
        the forensics could go dark with the suite green.
        """
        import ast
        import importlib

        module = importlib.import_module(module_name)
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())

        emitters = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "agent/session.started"
        ]
        assert emitters, f"{module_name} no longer emits agent/session.started"

        for call in emitters:
            payload = call.args[1]
            assert isinstance(payload, ast.Dict)
            spliced = [
                value for key, value in zip(payload.keys, payload.values)
                if key is None and isinstance(value, ast.Call)
                and getattr(value.func, "id", "") == "lineage_fields"
            ]
            assert spliced, (
                f"{module_name}'s agent/session.started payload does not splice "
                "**lineage_fields(); forensics ride this event, so an emitter "
                "that omits the chain blinds the runs it covers"
            )

    def test_root_session_reports_depth_zero(self):
        from bobi.subagent import _emit_session_started
        with patch("bobi.subagent._emit_lifecycle_event") as emit:
            with _process_env({}):
                _emit_session_started("rk", "proj", "task", "rk")
        payload = emit.call_args[0][1]
        assert payload["depth"] == 0


class TestChildEnvStripsLineage:
    """``child_agent_env`` has four callers and only one is a launch.

    If it stamped lineage, an agent running ``bobi agent <x> restart`` would
    copy its own chain into the new manager daemon, which then lives for days
    at depth N and refuses legitimate work team-wide.
    """

    def test_inherited_lineage_is_stripped(self, project):
        from bobi.env import child_agent_env
        chain = encode((_link("a"), _link("b")))
        with _process_env({LINEAGE_ENV_VAR: chain, "PATH": "/usr/bin"}):
            env = child_agent_env(project)
        assert LINEAGE_ENV_VAR not in env

    def test_manager_spawn_becomes_a_root(self, project):
        """The restart path: a deep chain in, a root out."""
        from bobi.env import child_agent_env
        deep = encode(tuple(_link(f"g{i}") for i in range(8)))
        with _process_env({LINEAGE_ENV_VAR: deep, "PATH": "/usr/bin"}):
            env = child_agent_env(project)
        assert current_lineage(env) == ()


@pytest.fixture
def launcher(project):
    """``launch_agent`` with everything but the lineage guard stubbed out.

    The spend governor is stubbed deliberately: this ticket is about the chain
    being bounded by a FIRST resort, so the last resort must not be what stops
    it. Yields a callable returning the environment the child would inherit.
    """
    registry = MagicMock(get=MagicMock(return_value=None))
    captured: dict = {}

    def _detached(script, args, log_file, env=None):
        captured["env"] = dict(env or {})
        return 4242

    with patch("bobi.subagent.check_requires", return_value=[]), \
         patch("bobi.subagent.get_registry", return_value=registry), \
         patch("bobi.subagent._check_spend_governor"), \
         patch("bobi.subagent._check_concurrency_semaphore"), \
         patch("bobi.subagent._launch_detached", side_effect=_detached), \
         patch("bobi.sdk.check_image_rotation"):
        from bobi.subagent import launch_agent

        def _launch(task, workflow="adhoc", **kwargs):
            captured.clear()
            name = launch_agent(task=task, cwd=str(project),
                                workflow_name=workflow, **kwargs)
            return name, captured.get("env", {})

        yield _launch, registry


class TestLaunchAgentPropagation:
    def test_child_inherits_a_chain_ending_with_itself(self, launcher):
        launch, _ = launcher
        with _process_env({}):
            name, child_env = launch("do a thing")
        chain = current_lineage(child_env)
        assert len(chain) == 1
        assert chain[-1].session == name

    def test_stamped_session_name_is_the_name_the_child_registers(self, launcher):
        """The lineage invariant: the stamped link names a session that exists.

        A link naming a session that never registers makes every chain that
        contains it unreadable, which is the whole point of carrying it.
        """
        launch, registry = launcher
        with _process_env({}):
            name, child_env = launch("do a thing")
        registered = registry.register.call_args.args[0].name
        assert current_lineage(child_env)[-1].session == registered == name

    def test_chain_grows_by_one_link_per_generation(self, launcher):
        launch, _ = launcher
        env: dict = {}
        for expected_depth in (1, 2, 3):
            with _process_env(env):
                _, env = launch(f"gen {expected_depth}")
            assert len(current_lineage(env)) == expected_depth

    def test_blocked_launch_spawns_nothing_and_registers_nothing(self, launcher):
        launch, registry = launcher
        deep = encode(tuple(_link(f"g{i}") for i in range(8)))
        with _process_env({LINEAGE_ENV_VAR: deep}):
            with pytest.raises(LaunchBlockedError):
                launch("one too deep")
        registry.register.assert_not_called()


class TestUnboundedAdhocChain:
    """Regression for #849.

    On 2026-07-25 one scheduled trigger became 50 workflow runs in 44 minutes:
    every generation's step-1 agent launched a fresh copy of its own workflow.
    Each launch carried a fresh ``adhoc-<uuid4[:8]>`` run key, so it looked
    like an unrelated first-time request and the existing active-run dedupe
    never fired. Only the spend governor stopped it, at 50.
    """

    def test_chain_is_refused_before_the_spend_governor_would_fire(self, launcher):
        from bobi.spend_governor import DEFAULT_CAP

        launch, _ = launcher
        env: dict = {}
        generations = 0
        blocked = None

        for generation in range(1, DEFAULT_CAP + 10):
            try:
                with _process_env(env):
                    _, env = launch(f"generation {generation}")
            except LaunchBlockedError as exc:
                blocked = exc
                break
            generations += 1
        else:
            pytest.fail(
                f"{generations} generations launched unbounded - this is the "
                "incident: nothing but the spend governor bounds the chain."
            )

        assert blocked.rule == "depth"
        assert generations == DEFAULT_MAX_LAUNCH_DEPTH
        assert generations < DEFAULT_CAP, (
            "the guard must be a first resort, ahead of the backstop"
        )

    def test_refusal_names_the_whole_chain(self, launcher):
        launch, _ = launcher
        env: dict = {}
        names = []
        for generation in range(1, DEFAULT_MAX_LAUNCH_DEPTH + 2):
            try:
                with _process_env(env):
                    name, env = launch(f"generation {generation}")
            except LaunchBlockedError as exc:
                for name in names:
                    assert name in str(exc), (
                        "diagnosing the incident meant reconstructing the "
                        "chain by hand across 50 session dirs"
                    )
                return
            names.append(name)
        pytest.fail("chain was never refused")


class TestConfiguredCapIsUsable:
    """A cap that cannot serve as a cap must fall back, not refuse everything.

    ``max_launch_depth: -1`` reads like the common "-1 means unlimited" idiom.
    Taken literally it makes ``len(child) > -1`` true for every launch, so the
    whole deployment is refused - and each refusal tells the blocked agent that
    the fix is an operator edit to the key that caused it.
    """

    @pytest.mark.parametrize("raw", ["-1", "0", "", "not-a-number"])
    def test_unusable_agent_yaml_value_falls_back(self, project, raw):
        paths.agent_yaml_path(project).write_text(
            f"entry_point: x\nmax_launch_depth: {raw}\n")
        assert max_launch_depth(project) == DEFAULT_MAX_LAUNCH_DEPTH

    def test_negative_cap_does_not_refuse_a_root_launch(self, project):
        paths.agent_yaml_path(project).write_text(
            "entry_point: x\nmax_launch_depth: -1\n")
        assert extend((), _link("a"), max_depth=max_launch_depth(project))

    def test_empty_key_does_not_break_config_load(self, project):
        """The bug is wider than the guard: a bare int(None) raises out of
        Config.load, so a half-finished edit stops the team booting."""
        from bobi.config import Config
        paths.agent_yaml_path(project).write_text(
            "entry_point: x\nmax_launch_depth:\n")
        assert Config.load(project).max_launch_depth == 0


class TestEventPayloads:
    """Every payload carries ``text``; the chat surface renders that, not JSON.

    Asserted per event because a topic-name-only assertion passes on an empty
    payload - verified: blanking both payloads left the suite green.
    """

    def test_depth_approaching_payload(self, project):
        chain = tuple(_link(f"g{i}") for i in range(6))
        with patch("bobi.events.publish.post_event") as post:
            extend(chain, _link("g6"), max_depth=8, project_path=project)
        payload = next(c.args[1] for c in post.call_args_list
                       if c.args[0] == "system/launch.depth.approaching")
        assert payload["depth"] == 7
        assert payload["max_launch_depth"] == 8
        assert payload["lineage"].endswith("g6")
        assert "approaching" in payload["text"]

    def test_lineage_dropped_payload(self, project):
        with patch("bobi.events.publish.post_event") as post:
            decode("not json", project_path=project)
        payload = next(c.args[1] for c in post.call_args_list
                       if c.args[0] == "system/launch.lineage.dropped")
        assert payload["reason"]
        assert payload["raw"] == "not json"
        assert "chain root" in payload["text"]

    def test_spend_cap_alert_carries_the_chain(self, project):
        """The non-default branch of the changed spend-governor signature.

        Its whole point is telling an operator the cap caught a CHAIN rather
        than 50 unrelated launches; only the empty branch was covered.
        """
        from bobi.spend_governor import emit_spend_cap_alert
        with patch("bobi.events.publish.post_event") as post:
            emit_spend_cap_alert(project, count=50, cap=50,
                                 lineage="director > adhoc-1 > adhoc-2")
        payload = post.call_args.args[1]
        assert payload["lineage"] == "director > adhoc-1 > adhoc-2"
        assert "director > adhoc-1 > adhoc-2" in payload["text"]


class TestGuardOrderingAndPlacement:
    """Pins the two orderings the code documents as load-bearing.

    Both were unpinned: swapping the guard past the spend governor, or moving
    the manager's root stamp below the monitor scheduler, left the suite green.
    """

    def test_lineage_is_admitted_before_the_spend_governor(self, project):
        """The governor is the classification-free backstop; this is the first
        resort. Reversed, a runaway chain surfaces as spend.cap.breached with
        the wrong cause instead of launch.blocked with the chain."""
        calls: list[str] = []
        registry = MagicMock(get=MagicMock(return_value=None))
        with patch("bobi.launch_lineage.admit",
                   side_effect=lambda *a, **k: calls.append("lineage") or ()), \
             patch("bobi.subagent._check_spend_governor",
                   side_effect=lambda *a: calls.append("spend")), \
             patch("bobi.subagent.check_requires", return_value=[]), \
             patch("bobi.subagent.get_registry", return_value=registry), \
             patch("bobi.subagent._check_concurrency_semaphore"), \
             patch("bobi.subagent._launch_detached", return_value=1), \
             patch("bobi.sdk.check_image_rotation"):
            from bobi.subagent import launch_agent
            with _process_env({}):
                launch_agent(task="t", cwd=str(project), workflow_name="adhoc")
        assert calls == ["lineage", "spend"]

    def test_manager_roots_its_chain_before_the_scheduler_starts(self):
        """service.py's comment calls this ordering load-bearing: the monitor
        scheduler's checks spawn from this process, so a stamp after it would
        let those launches read as roots of their own."""
        import ast
        import bobi.service as service

        tree = ast.parse(open(service.__file__, encoding="utf-8").read())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "run_manager_from_config")
        stamp_lines = [n.lineno for n in ast.walk(func)
                       if isinstance(n, ast.Call)
                       and getattr(n.func, "id", "") == "stamp_root"]
        sched_lines = [n.lineno for n in ast.walk(func)
                       if isinstance(n, ast.Call)
                       and getattr(n.func, "id", "") == "MonitorScheduler"]
        assert stamp_lines, "the manager no longer roots its launch chain"
        assert sched_lines, "test is stale: MonitorScheduler is gone"
        assert max(stamp_lines) < min(sched_lines)


class TestWaitPathIsGuarded:
    """The second launch site, and the one most likely to run away.

    ``-w adhoc --wait`` is the delegation idiom the role prompts teach, and
    ``adhoc`` is exempt from the self-recursion rule, so depth is the ONLY
    thing bounding it. It had no coverage: deleting its admit+stamp block left
    the whole suite green while chains through it became unbounded.

    Since #1057 the wait path IS ``launch_agent`` (wait=True): one admit for
    every launch, and the wait branch stamps the chain into ``os.environ``
    before running the workflow in-process. The executor is ``run_workflow``,
    so that is the capture point.
    """

    def _run_wait(self, project, capture=None, **kwargs):
        from bobi.cli import _run_agent_wait
        defaults = dict(
            cwd=str(project), task="delegate this", workflow="adhoc",
            role="engineer", run_key=None, timeout=60, requested_by=None,
            interactive=True, persistent=False, subscribe=[],
        )
        defaults.update(kwargs)

        def _fake_run_workflow(*, collect=None, **kw):
            if capture is not None:
                capture["chain"] = current_lineage()
                capture["kwargs"] = kw
                # The environment a child generation would inherit: launches
                # from the agent's shell happen DURING the run - the stamp is
                # restored once the run returns (#1057).
                capture["env"] = dict(os.environ)
            if collect is not None:
                collect["final_text"] = ""
            return True

        with patch("bobi.cli._detect_project_root", return_value=project), \
             patch("bobi.workflow.orchestrator.run_workflow",
                   side_effect=_fake_run_workflow) as run_wf, \
             patch("bobi.subagent.check_requires", return_value=[]), \
             patch("bobi.subagent._check_spend_governor"), \
             patch("bobi.subagent._check_concurrency_semaphore"), \
             patch("bobi.sdk.check_image_rotation"):
            _run_agent_wait(**defaults)
        return run_wf

    def test_stamps_the_chain_into_its_own_environment(self, project):
        """--wait runs the agent IN THIS PROCESS, so the chain must land in
        os.environ - that is what a `bobi` call from the agent's shell reads."""
        seen: dict = {}
        with _process_env({}):
            self._run_wait(project, capture=seen)
        assert len(seen["chain"]) == 1

    def test_stamped_name_is_the_name_the_run_registers(self, project):
        """The lineage invariant on this path: a link can never name a session
        that never exists.

        launch_agent resolves the key and the session name ONCE and both the
        stamp and the executor use them, so the two cannot drift (#850).
        Asserting the stamped link against run_workflow's kwargs alone would
        compare a variable to itself; it is checked against the real
        production derivation as well, so a wait branch that stamped a
        differently-derived name is caught."""
        from bobi.subagent import _resolve_project_name, _resolve_run_key
        from bobi.workflow.orchestrator import make_session_name
        seen: dict = {}
        with _process_env({}):
            self._run_wait(project, capture=seen, task="delegate this")
        # Every dial the CLI passes has to be passed here too, or this asserts
        # against a name production never produces.
        proj = _resolve_project_name(str(project))
        expected_key, derived = _resolve_run_key(
            "adhoc", "delegate this", None, False, project=proj,
            role="engineer")
        assert derived is True
        assert seen["kwargs"]["run_key"] == expected_key
        assert seen["chain"][-1].session == make_session_name(
            "adhoc", proj, expected_key)

    def test_stamped_name_holds_for_a_random_key(self, project):
        """The case a recomputed name could not survive: --id-random mints a
        key that cannot be derived twice, so a stamp site that recomputed it
        would name a session that never registers."""
        from bobi.subagent import _resolve_project_name
        from bobi.workflow.orchestrator import make_session_name
        seen: dict = {}
        with _process_env({}):
            self._run_wait(project, capture=seen, random_key=True)
        key = seen["kwargs"]["run_key"]
        assert key.startswith("rand-")
        assert seen["chain"][-1].session == make_session_name(
            "adhoc", _resolve_project_name(str(project)), key)

    def test_a_deep_chain_is_refused_and_nothing_spawns(self, project):
        deep = encode(tuple(_link(f"g{i}") for i in range(8)))
        seen: dict = {}
        with _process_env({LINEAGE_ENV_VAR: deep}):
            with pytest.raises(LaunchBlockedError) as exc:
                self._run_wait(project, capture=seen)
        assert exc.value.rule == "depth"
        # The capture only fills when run_workflow runs; a refused launch
        # must never reach the executor.
        assert seen == {}

    def test_chain_grows_across_wait_generations(self, project):
        """The incident shape through the second site: each generation
        inherits the environment of the RUNNING previous one (a child is
        launched from the agent's shell mid-run; after the run the stamp is
        restored, so sampling post-return would test nothing)."""
        depths = []

        env: dict = {}
        for generation in range(3):
            seen: dict = {}
            with _process_env(dict(env)):
                self._run_wait(project, capture=seen,
                               task=f"gen {generation}")
                env = seen["env"]
            depths.append(len(seen["chain"]))
        assert depths == [1, 2, 3]

    def test_the_stamp_is_restored_after_the_run(self, project):
        """A long-lived caller must not accumulate depth across wait runs -
        that would end in the depth guard refusing it everything."""
        seen: dict = {}
        with _process_env({}):
            self._run_wait(project, capture=seen)
            after = os.environ.get(LINEAGE_ENV_VAR)
        assert seen["chain"], "stamp was never visible to the run"
        assert after is None


class TestRefusalReachesTheAgent:
    """The refusal CONTENT is asserted 13 ways; that is dead weight unless the
    message is actually delivered.

    Verified gap: replacing the handler with a bare ``raise`` left the suite
    green, restoring the traceback the wrapper exists to prevent.
    """

    def _launch(self, install, chain):
        from click.testing import CliRunner
        from bobi.cli import main
        with patch.dict(os.environ, {LINEAGE_ENV_VAR: chain}):
            return CliRunner().invoke(main, [
                "agent", install.agent_name, "subagents", "launch",
                "-w", "adhoc", "--role", "engineer", "--task", "go",
            ])

    def test_blocked_launch_exits_1_with_one_readable_line(self, bobi_install):
        deep = encode(tuple(_link(f"g{i}") for i in range(8)))
        result = self._launch(bobi_install, deep)

        assert result.exit_code == 1
        assert "Launch blocked: depth." in result.output
        assert "deterministic" in result.output
        assert "Traceback" not in result.output
        assert not isinstance(result.exception, LaunchBlockedError), (
            "the refusal escaped as an exception; an LLM reads a traceback as "
            "a transient crash and retries, which is the launch storm"
        )

    def test_refusal_does_not_name_the_bypass(self, bobi_install):
        deep = encode(tuple(_link(f"g{i}") for i in range(8)))
        result = self._launch(bobi_install, deep)
        assert LINEAGE_ENV_VAR not in result.output
        assert MAX_DEPTH_ENV_VAR not in result.output


class TestSpendAlertChainOnlyWhenThereIsOne:
    """``render(())`` is the literal ``"(root)"``, never ``""``.

    Passing it through unconditionally would claim a chain for every rootless
    launch, inverting the chain-vs-50-unrelated-launches distinction the field
    was added to draw.
    """

    def _alert_kwargs(self, project, env):
        from bobi.spend_governor import DEFAULT_CAP, _save_state, _state_path
        import time
        _save_state(_state_path(project), [time.time() for _ in range(DEFAULT_CAP)])
        with patch("bobi.spend_governor.emit_spend_cap_alert") as alert:
            from bobi.subagent import _check_spend_governor
            with _process_env(env):
                with pytest.raises(RuntimeError, match="Spend governor"):
                    _check_spend_governor(project)
        return alert.call_args.kwargs

    def test_rootless_launch_reports_no_chain(self, project):
        assert self._alert_kwargs(project, {})["lineage"] == ""

    def test_chained_launch_reports_the_chain(self, project):
        chain = (_link("director"), _link("adhoc-1"))
        kwargs = self._alert_kwargs(project, {LINEAGE_ENV_VAR: encode(chain)})
        assert kwargs["lineage"] == "director > adhoc-1"
