import pytest

from bobi.sdk import load_session_route, save_session_id, save_session_route

def test_route_storage_is_root_scoped_and_cleared_with_session(tmp_path, monkeypatch):
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    first = tmp_path / "first"
    second = tmp_path / "second"
    record = {"experiment_id": "exp", "config_fingerprint": "hash", "model_selected": "cheap"}
    save_session_route("agent", record, root=first)
    assert load_session_route("agent", root=first) == record
    assert load_session_route("agent", root=second) is None
    save_session_id("agent", "provider-session", model="cheap", root=first)
    assert load_session_route("agent", root=first) == record
    save_session_id("agent", "", root=first)
    assert load_session_route("agent", root=first) is None

def test_invalid_and_corrupt_route_records_are_not_reused(tmp_path):
    from bobi.sdk import _sessions_dir

    with pytest.raises(ValueError):
        save_session_route("../escape", {}, root=tmp_path)
    assert load_session_route("../escape", root=tmp_path) is None
    path = _sessions_dir(tmp_path) / "agent.route.json"
    for encoded in ("not-json", "[]", "x" * 65537):
        path.write_text(encoded)
        assert load_session_route("agent", root=tmp_path) is None

def test_failed_atomic_write_preserves_previous_route(tmp_path, monkeypatch):
    save_session_route("agent", {"model_selected": "control"}, root=tmp_path)
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr("bobi.sdk.atomic_write_json", fail)
    with pytest.raises(OSError):
        save_session_route("agent", {"model_selected": "cheap"}, root=tmp_path)
    assert load_session_route("agent", root=tmp_path) == {"model_selected": "control"}


def test_recovery_keeps_route_but_explicit_clear_removes_it(tmp_path, monkeypatch):
    from bobi.sdk import load_session_id

    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    record = {"model_selected": "cheap"}
    save_session_route("agent", record, root=tmp_path)
    save_session_id("agent", "stale-id", model="cheap", root=tmp_path)
    save_session_id("agent", "", root=tmp_path, preserve_route=True)
    assert load_session_id("agent", root=tmp_path) == ""
    assert load_session_route("agent", root=tmp_path) == record
    save_session_id("agent", "recovered-id", model="cheap", root=tmp_path)
    assert load_session_route("agent", root=tmp_path) == record
    save_session_id("agent", "", root=tmp_path)
    assert load_session_route("agent", root=tmp_path) is None
