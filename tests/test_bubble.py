"""Unit tests for the bubble mint/join/re-mint seam (no live server).

Covers ensure_bubble's lock-protected mint, the compare-and-swap re-mint on
server restart (force_remint_of), concurrent convergence on one bubble,
cleartext-remote refusal, BubbleRejected on 403, and the --fresh wipe in
_clear_manager_session. The live round-trips are covered by
tests/integration/test_event_server.py::TestBubbleIsolation.
"""

import threading
import time
from itertools import count
from unittest.mock import patch

import httpx
import pytest

from bobi.config import (
    bubble_state_path,
    load_bubble_state,
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
    from bobi.cli import _clear_manager_session

    # Seed bubble + per-session deployment + cursor state.
    save_bubble_state(project, "bub_1", "bkey_1")
    save_deployment_state(project, "manager", "dep1", "moda_1")
    cur = session_cursor_path(project, "manager")
    cur.parent.mkdir(parents=True, exist_ok=True)
    cur.write_text('{"last_seen": 5}')

    # save_session_id resolves the bound process root (CLI binds it); not under
    # test here — patch it so we exercise only the wipe.
    with patch("bobi.sdk.save_session_id"):
        _clear_manager_session(project)

    assert load_bubble_state(project) == {}
    assert not (bubble_state_path(project).parent / "deployments").exists()
    assert not (bubble_state_path(project).parent / "cursors").exists()
