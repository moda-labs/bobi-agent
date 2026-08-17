"""Unit tests for the bubble mint/join/re-mint seam (no live server).

Covers ensure_bubble's lock-protected mint, the compare-and-swap re-mint on
server restart (force_remint_of), concurrent convergence on one bubble,
cleartext-remote refusal, BubbleRejected on 403, and the --fresh wipe in
service.clear_manager_session. Also home to the bobi.events.state
persistence tests: the bubble file's 0600 contract and the per-session
deployment-state records. The live round-trips are covered by
tests/integration/test_event_server.py::TestBubbleIsolation.
"""

import json
import stat
import threading
import time
from itertools import count
from unittest.mock import patch

import httpx
import pytest

from bobi.events.state import (
    bubble_state_path,
    load_bubble_state,
    load_deployment_state,
    save_bubble_state,
    save_deployment_state,
    session_cursor_path,
)
from bobi.events import server as es


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".bobi").mkdir()
    return tmp_path


def _fake_mint(_base, _name, _subs, bubble_id="", bubble_key=""):
    """Stand-in for _post_register's MINT branch — unique bubble per call."""
    n = next(_fake_mint.counter)
    return {"deployment_id": f"dep{n}", "api_key": f"moda_{n}",
            "bubble_id": f"bub_{n}", "bubble_key": f"bkey_{n}"}


_fake_mint.counter = count(1)


def test_ensure_bubble_mints_once_and_persists(project):
    _fake_mint.counter = count(1)
    with patch.object(es, "_post_register", side_effect=_fake_mint):
        b1 = es.ensure_bubble("http://localhost:8080", project)
        b2 = es.ensure_bubble("http://localhost:8080", project)
    assert b1["bubble_id"] == "bub_1"
    assert b2 == b1                      # second call loads, never re-mints
    assert load_bubble_state(project)["bubble_id"] == "bub_1"


def test_ensure_bubble_refuses_cleartext_remote_mint(project):
    with patch.object(es, "_post_register", side_effect=_fake_mint):
        with pytest.raises(RuntimeError, match="cleartext"):
            es.ensure_bubble("http://remote.example.com:8080", project)
    assert load_bubble_state(project) == {}   # nothing minted


def test_force_remint_replaces_stale_bubble(project):
    _fake_mint.counter = count(1)
    with patch.object(es, "_post_register", side_effect=_fake_mint):
        first = es.ensure_bubble("http://localhost:8080", project)
        # Server forgot the bubble → caller flags it stale → re-mint.
        second = es.ensure_bubble("http://localhost:8080", project,
                                  force_remint_of=first["bubble_id"])
    assert second["bubble_id"] != first["bubble_id"]
    assert load_bubble_state(project)["bubble_id"] == second["bubble_id"]


def test_force_remint_is_noop_when_bubble_already_rotated(project):
    """CAS guard: if another session already re-minted, don't mint a third."""
    _fake_mint.counter = count(1)
    with patch.object(es, "_post_register", side_effect=_fake_mint):
        es.ensure_bubble("http://localhost:8080", project)          # bub_1
        # Simulate a concurrent session having already rotated to a new bubble.
        save_bubble_state(project, "bub_rotated", "bkey_rotated")
        # We ask to re-mint the OLD id; on-disk is already different → no mint.
        result = es.ensure_bubble("http://localhost:8080", project,
                                  force_remint_of="bub_1")
    assert result["bubble_id"] == "bub_rotated"


def test_concurrent_ensure_bubble_converges_on_one(project):
    _fake_mint.counter = count(1)

    def _slow_mint(*a, **k):
        time.sleep(0.05)            # widen the race window
        return _fake_mint(*a, **k)

    results: list[dict] = []
    with patch.object(es, "_post_register", side_effect=_slow_mint):
        threads = [threading.Thread(
            target=lambda: results.append(
                es.ensure_bubble("http://localhost:8080", project)))
            for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    ids = {r["bubble_id"] for r in results}
    assert len(ids) == 1, f"sessions split across bubbles: {ids}"
    assert not bubble_state_path(project).with_suffix(".lock").exists()


def test_post_register_raises_bubble_rejected_on_403(project):
    transport = httpx.MockTransport(lambda req: httpx.Response(403, json={"error": "forbidden"}))
    from bobi import http as pooled
    with patch.object(pooled, "_client", httpx.Client(transport=transport)):
        with pytest.raises(es.BubbleRejected):
            es._post_register("http://localhost:8080", "s", ["inbox/s"],
                              bubble_id="bub_x", bubble_key="bkey_x")


def test_clear_manager_session_wipes_bubble_and_state(project):
    from bobi.service import clear_manager_session

    # Seed bubble + per-session deployment + cursor state.
    save_bubble_state(project, "bub_1", "bkey_1")
    save_deployment_state(project, "manager", "dep1", "moda_1")
    cur = session_cursor_path(project, "manager")
    cur.parent.mkdir(parents=True, exist_ok=True)
    cur.write_text('{"last_seen": 5}')

    # save_session_id resolves the bound process root (CLI binds it); not under
    # test here — patch it so we exercise only the wipe.
    with patch("bobi.sdk.save_session_id"):
        clear_manager_session(project)

    assert load_bubble_state(project) == {}
    assert not (bubble_state_path(project).parent / "deployments").exists()
    assert not (bubble_state_path(project).parent / "cursors").exists()


def test_save_bubble_state_creates_and_keeps_mode_0600(tmp_path):
    """The bubble key is a signing secret: the file must be BORN 0600 — not
    created loose and tightened after, since chmod cannot revoke a descriptor
    a racing reader already holds — an existing looser-mode file must be
    re-tightened on overwrite, and the overwrite must truncate so no tail of
    a longer previous key survives. This is the reason save_bubble_state
    deliberately stays off fsutil's atomic helper, whose rename-over drops
    the target's mode (see bobi/fsutil.py)."""
    # Neutralize the trailing chmod so the asserted mode can only come from
    # the os.open call itself.
    with patch("os.chmod", lambda *a, **k: None):
        save_bubble_state(tmp_path, "b-1", "a-deliberately-long-first-key")
    p = bubble_state_path(tmp_path)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600

    p.chmod(0o644)  # simulate a loosened file from an older/broken writer
    save_bubble_state(tmp_path, "b-2", "k2")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # Byte-equality proves O_TRUNC: the shorter payload fully replaced the
    # longer one, leaving no stale key tail on disk.
    assert p.read_text() == json.dumps({"bubble_id": "b-2", "bubble_key": "k2"})


# --- per-session deployment state -----------------------------------------


def test_deployment_state_roundtrip(tmp_path):
    save_deployment_state(tmp_path, "sess-a", "dep-123", "moda_key456")
    state = load_deployment_state(tmp_path, "sess-a")

    assert state["deployment_id"] == "dep-123"
    assert state["api_key"] == "moda_key456"


def test_deployment_state_missing_returns_empty(tmp_path):
    state = load_deployment_state(tmp_path, "sess-a")
    assert state == {}


def test_deployment_state_is_per_session(tmp_path):
    """Sessions must never share a deployment — the shared-deployment bug
    delivered every agent the union of all sessions' subscriptions."""
    save_deployment_state(tmp_path, "director", "dep-1", "key-1")
    save_deployment_state(tmp_path, "lead", "dep-2", "key-2")

    assert load_deployment_state(tmp_path, "director")["deployment_id"] == "dep-1"
    assert load_deployment_state(tmp_path, "lead")["deployment_id"] == "dep-2"
