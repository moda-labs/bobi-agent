"""Tests for Linear conversation (question/reply threading)."""

from dispatch.state import StateStore, Status


def test_blocked_status_is_tracked(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-1", "/tmp/repo", "Task")
    store.update_status("PROJ-1", Status.BLOCKED)

    assert store.is_tracked("PROJ-1")


def test_blocked_appears_in_flight(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-1", "/tmp/repo", "Task")
    store.update_status("PROJ-1", Status.BLOCKED)

    items = store.get_in_flight()
    assert len(items) == 1
    assert items[0].status == Status.BLOCKED


def test_blocked_with_question_id(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-1", "/tmp/repo", "Task")
    store.update_status(
        "PROJ-1", Status.BLOCKED,
        linear_issue_id="issue-uuid-123",
        pending_question_id="comment-uuid-456",
    )

    items = store.get_in_flight()
    assert items[0].linear_issue_id == "issue-uuid-123"
    assert items[0].pending_question_id == "comment-uuid-456"


def test_unblock_clears_question(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-1", "/tmp/repo", "Task")
    store.update_status(
        "PROJ-1", Status.BLOCKED,
        linear_issue_id="issue-uuid-123",
        pending_question_id="comment-uuid-456",
    )

    # Simulate unblocking after reply
    store.update_status(
        "PROJ-1", Status.WORKING,
        pending_question_id=None,
        last_reply="Yes, go ahead with approach A.",
    )

    items = store.get_in_flight()
    assert items[0].status == Status.WORKING
    assert items[0].pending_question_id is None
    assert items[0].last_reply == "Yes, go ahead with approach A."


def test_blocked_does_not_prevent_new_dispatch(tmp_path):
    """Blocked items still count as tracked — can't double-dispatch."""
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-1", "/tmp/repo", "Task")
    store.update_status("PROJ-1", Status.BLOCKED)

    result = store.dispatch("PROJ-1", "/tmp/repo", "Same task again")
    assert result is False


def test_persistence_with_blocked_fields(tmp_path):
    state_path = tmp_path / "state.json"

    store1 = StateStore(path=state_path)
    store1.dispatch("PROJ-1", "/tmp/repo", "Task")
    store1.update_status(
        "PROJ-1", Status.BLOCKED,
        linear_issue_id="issue-123",
        pending_question_id="comment-456",
    )

    # Reload from disk
    store2 = StateStore(path=state_path)
    items = store2.get_in_flight()
    assert items[0].status == Status.BLOCKED
    assert items[0].linear_issue_id == "issue-123"
    assert items[0].pending_question_id == "comment-456"
