"""Tests for slot identity on disk (`bobi.webapp.slots`).

`finalize_slot` was a closure inside the /api/setup/open route, reachable only
through a full HTTP request plus a live SetupState. These exercise the rename
rules directly — which is the point of it having a name (Q107).
"""

import pytest

from bobi import paths
from bobi.webapp.slots import finalize_slot


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _slot(name, *, src=True, run=True) -> None:
    """Build an `agents/<name>/` slot on disk."""
    if src:
        paths.agent_source_dir(name).mkdir(parents=True, exist_ok=True)
        (paths.agent_source_dir(name) / "agent.yaml").write_text(f"agent: {name}\n")
    if run:
        paths.agent_run_root(name).mkdir(parents=True, exist_ok=True)
        (paths.agent_run_root(name) / "marker").write_text(name)


class TestFreeTarget:
    def test_the_whole_slot_moves_to_the_final_name(self, home):
        _slot("new-agent")
        finalize_slot("new-agent", "invoicer", str(paths.agent_source_dir("new-agent")))

        assert not paths.agent_dir("new-agent").exists()
        assert paths.agent_source_dir("invoicer").is_dir()
        assert (paths.agent_run_root("invoicer") / "marker").read_text() == "new-agent"


class TestOccupiedTarget:
    def test_same_team_absorbs_the_run_dir_and_drops_the_placeholder(self, home):
        # Editing an installed team: the target already exists and the setup
        # session's source IS the target's source.
        _slot("invoicer", run=False)
        _slot("new-agent", src=False)
        source = str(paths.agent_source_dir("invoicer"))

        finalize_slot("new-agent", "invoicer", source)

        assert not paths.agent_dir("new-agent").exists()
        assert (paths.agent_run_root("invoicer") / "marker").read_text() == "new-agent"

    def test_an_existing_run_dir_is_never_clobbered(self, home):
        _slot("invoicer")
        (paths.agent_run_root("invoicer") / "marker").write_text("original")
        _slot("new-agent", src=False)

        finalize_slot("new-agent", "invoicer",
                      str(paths.agent_source_dir("invoicer")))

        assert (paths.agent_run_root("invoicer") / "marker").read_text() == "original"
        # The placeholder still holds the run/ that was not absorbed, so
        # nothing is silently lost.
        assert (paths.agent_run_root("new-agent") / "marker").exists()

    def test_a_different_team_holding_the_name_is_left_alone(self, home):
        # `invoicer` exists but belongs to someone else: the setup source is
        # the placeholder's own, not the target's. Neither slot may change.
        _slot("invoicer")
        (paths.agent_run_root("invoicer") / "marker").write_text("theirs")
        _slot("new-agent")

        finalize_slot("new-agent", "invoicer",
                      str(paths.agent_source_dir("new-agent")))

        assert (paths.agent_run_root("invoicer") / "marker").read_text() == "theirs"
        assert (paths.agent_run_root("new-agent") / "marker").read_text() == "new-agent"

    def test_a_different_teams_slot_never_absorbs_this_run_dir(self, home):
        """The source check is what stops a cross-team move, not luck.

        `invoicer` belongs to someone else and has no run/ of its own yet, so
        the salvage branch is unobstructed: without the source check this
        slot's run/ — its workspace and its .env — lands in their team.
        """
        _slot("invoicer", run=False)
        _slot("new-agent")

        finalize_slot("new-agent", "invoicer",
                      str(paths.agent_source_dir("new-agent")))

        assert not paths.agent_run_root("invoicer").exists()
        assert (paths.agent_run_root("new-agent") / "marker").read_text() == "new-agent"


class TestRefusals:
    @pytest.mark.parametrize("final", ["", "   ", "new-agent", "../escape", "a/b"])
    def test_an_unusable_final_name_changes_nothing(self, home, final):
        _slot("new-agent")
        finalize_slot("new-agent", final, str(paths.agent_source_dir("new-agent")))
        assert (paths.agent_run_root("new-agent") / "marker").exists()

    def test_a_missing_placeholder_slot_is_not_an_error(self, home):
        paths.agents_root().mkdir(parents=True, exist_ok=True)
        finalize_slot("new-agent", "invoicer", "")
        assert not paths.agent_dir("invoicer").exists()

    def test_an_empty_source_dir_never_matches_a_real_target(self, home):
        # Path("").resolve() is the cwd — it must not be read as "the target's
        # source", which would fold an unrelated slot into an occupied name.
        _slot("invoicer")
        (paths.agent_run_root("invoicer") / "marker").write_text("theirs")
        _slot("new-agent")

        finalize_slot("new-agent", "invoicer", "")

        assert (paths.agent_run_root("invoicer") / "marker").read_text() == "theirs"
        assert paths.agent_dir("new-agent").is_dir()
