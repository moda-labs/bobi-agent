"""Tests for the unified web app server — security, the dashboard snapshot,
and the per-agent lifecycle endpoints. Most classes monkeypatch service-core
calls and read a real (temp) BOBI_HOME via the bobi_install fixture;
TestMultiAgentRealService runs the real service layer on purpose (#706)."""

import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from bobi import service
from bobi.webapp import server
from tests.metrics.helpers import seed_dashboard

TOKEN = "test-token-123"


def _testclient():
    # The Host guard only allows loopback; TestClient defaults to "testserver".
    app = server.build_app(token=TOKEN)
    return TestClient(app, base_url="http://127.0.0.1")


def _client():
    c = _testclient()
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


def _first_workflow_prompt(monkeypatch, task: str, instruction: str) -> str:
    """The first prompt a real workflow run sends its brain, so a search test follows the
    orchestrator's own templates rather than a copy of them."""
    from unittest.mock import MagicMock, patch

    from bobi.workflow.orchestrator import run_workflow
    from bobi.workflow.schema import StepDef, Workflow
    from tests.test_orchestrator import _recording_brain

    monkeypatch.setenv("BOBI_BRAIN", "claude")
    brain, _, clients = _recording_brain()
    workflow = Workflow(name="deploy", steps=[StepDef(name="implement", prompt=instruction),
                                              StepDef(name="review", prompt="Review it.")])
    with patch("bobi.brain.get_brain", lambda kind=None: brain), \
            patch("bobi.workflow.orchestrator.get_registry", return_value=MagicMock()), \
            patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
            patch("bobi.workflow.orchestrator._setup_worktree", return_value="/tmp"), \
            patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
            patch("bobi.workflow.orchestrator.save_session_id"), \
            patch("bobi.brain.turns.save_session_id"), \
            patch("bobi.workflow.orchestrator.log_activity"), \
            patch("bobi.brain.turns.log_activity"):
        assert run_workflow(workflow, task=task, repo="r", cwd="/tmp", run_key="1") is True
    return clients[0].queries[0]


class TestMetrics:
    RANGE = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z"}

    def test_metrics_endpoints_and_collector_privacy(self, bobi_install):
        seed_dashboard(bobi_install.repo_path, 1_000_000)
        health = bobi_install.state_dir / "metrics/collector.state.json"
        health.write_text(json.dumps({"status": "running", "db_ready": True,
                                     "reconciliation_errors": 1, "last_error": "private-task"}))
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        summary = _client().get(base + "/summary", params=self.RANGE)
        assert summary.status_code == 200
        assert summary.headers["cache-control"] == "no-store"
        assert summary.json()["totals"]["input_tokens"] == 720
        assert summary.json()["collector"]["reconciliation_errors"] == 1
        assert "private-task" not in summary.text
        summary_s1 = _client().get(base + "/summary", params={**self.RANGE, "session_id": "s1"})
        assert summary_s1.status_code == 200
        for endpoint in ("/turns", "/turns/t-pro", "/sessions/s1"):
            result = _client().get(base + endpoint, params=self.RANGE)
            assert result.status_code == 200
            assert "private-task" not in result.text
        assert _client().get(base + "/turns/t-pro").json()["router_decisions"][0]["confidence"] == 0.96
        assert _client().get(base + "/turns/absent").status_code == 404
        assert _client().get(base + "/sessions/absent").status_code == 404
        assert _client().get("/api/agents/absent/metrics/summary", params=self.RANGE).status_code == 404

    def test_metrics_summary_says_whether_the_collector_is_live(self, bobi_install):
        seed_dashboard(bobi_install.repo_path, 1_000_000)
        health = bobi_install.state_dir / "metrics/collector.state.json"
        url = f"/api/agents/{bobi_install.agent_name}/metrics/summary"
        now_us = time.time_ns() // 1000

        def live():
            return _client().get(url, params=self.RANGE).json()["collector"]["live"]

        assert live() is False
        health.write_text(json.dumps({"status": "running", "last_success_at_us": now_us}))
        assert live() is True
        health.write_text(json.dumps({"status": "degraded", "last_success_at_us": now_us}))
        assert live() is False
        # A collector that died mid-run leaves "running" behind with an old heartbeat.
        health.write_text(json.dumps({"status": "running", "last_success_at_us": now_us - 120_000_000}))
        assert live() is False
        # A clock stepped back must not keep a dead collector live.
        health.write_text(json.dumps({"status": "running", "last_success_at_us": now_us + 120_000_000}))
        assert live() is False
        for garbage in ('{"status": "running", "last_success_at_us": "soon"}', "{not json"):
            health.write_text(garbage)
            assert live() is False

    def test_metrics_unavailable_validation_and_security(self, bobi_install):
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        response = _client().get(base + "/summary", params=self.RANGE)
        assert response.status_code == 503
        assert response.json()["code"] == "metrics_not_ready"
        assert response.headers["retry-after"] == "1"
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()
        assert _testclient().get(base + "/summary", params=self.RANGE).status_code == 403
        hostile = TestClient(server.build_app(token=TOKEN), base_url="http://evil.example")
        hostile.headers.update({"x-bobi-webui-token": TOKEN})
        assert hostile.get(base + "/summary", params=self.RANGE).status_code == 403
        assert _client().get(base + "/summary").status_code == 422
        assert _client().get(base + "/summary", params={**self.RANGE, "from": "bad"}).status_code == 400
        assert _client().get(base + "/turns", params={**self.RANGE, "limit": 201}).status_code == 422

    def test_a_query_too_slow_is_a_client_error_without_retry(self, bobi_install, monkeypatch):
        from bobi.metrics.query import MetricsQueryError
        from bobi.webapp.runtime import LocalRuntime

        def too_slow(self, name, view, args):
            raise MetricsQueryError("metrics query exceeded its one-second deadline", "query_too_slow")

        monkeypatch.setattr(LocalRuntime, "metrics", too_slow)
        response = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/summary",
                                 params=self.RANGE)
        assert response.status_code == 400
        assert response.json()["code"] == "query_too_slow"
        assert "retry-after" not in response.headers

    def test_metrics_reader_saturation_release_and_unsupported_runtime(self, bobi_install):
        from bobi.metrics.query import MetricsQueryError
        from bobi.webapp.runtime import LocalRuntime, TeamRuntime

        runtime = LocalRuntime()
        client = TestClient(server.build_app(token=TOKEN, runtime=runtime), base_url="http://127.0.0.1")
        client.headers.update({"x-bobi-webui-token": TOKEN})
        url = f"/api/agents/{bobi_install.agent_name}/metrics/summary"
        for _ in range(4):
            assert runtime._metrics_reads.acquire(blocking=False)
        assert client.get(url, params=self.RANGE).json()["code"] == "metrics_busy"
        for _ in range(4):
            runtime._metrics_reads.release()
        for _ in range(5):
            assert client.get(url, params=self.RANGE).json()["code"] == "metrics_not_ready"
        with pytest.raises(MetricsQueryError) as error:
            TeamRuntime.metrics(runtime, bobi_install.agent_name, "summary", self.RANGE)
        assert error.value.code == "metrics_unsupported"

    def test_metrics_turn_io_is_lifecycle_scoped_and_web_only(self, bobi_install, monkeypatch, tmp_path):
        from bobi.metrics.query import MetricsQueries
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        prompt = "Event: slack/message\n  Please explain **WAL**.\n  conversation: slack:T123:channel:C123:thread:1791360056.16\n  channel_name: eng-team"
        transcript = tmp_path / "old-provider.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at,
            "message": {"content": text}}) for kind, at, text in [
                ("user", "1970-01-01T00:00:01.100Z", prompt),
                ("assistant", "1970-01-01T00:00:01.800Z", "A **write-ahead log**."),
                ("user", "1970-01-01T00:00:02.100Z", "Different turn"),
                ("assistant", "1970-01-01T00:00:02.800Z", "Different answer"),
        ]))
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        result = _client().get(base + "/turns/t-routine")
        assert result.status_code == 200
        io = result.json()["conversation"]
        assert io["input"] == prompt
        assert io["response"] == "A **write-ahead log**."
        assert io["status"] == "matched"
        assert io["origin"] == {"source": "slack", "channel_id": "C123", "channel_name": "eng-team",
                                "thread_id": "1791360056.16", "url": "https://app.slack.com/client/T123/C123/thread/C123-1791360056.16"}
        assert "Different" not in json.dumps(io)
        turns = _client().get(base + "/turns", params=self.RANGE).json()["turns"]
        routine = next(turn for turn in turns if turn["turn_id"] == "t-routine")
        assert routine["conversation"]["prompt_snippet"] == "Please explain **WAL**."
        assert routine["semantic_title"] == io["semantic_title"] == "Please explain **WAL**."
        assert routine["conversation"]["response_snippet"] == "A **write-ahead log**."
        assert "entries" not in routine["conversation"]
        assert "conversation" not in MetricsQueries(bobi_install.repo_path).turn({"turn_id": "t-routine"})
        assert _client().get(base + "/turns/t-unknown").json()["conversation"]["status"] == "unavailable"
        sessions = _client().get(base + "/sessions", params=self.RANGE)
        assert sessions.status_code == 200
        assert {session["session_id"] for session in sessions.json()["sessions"]} == {"s1", "s2"}

    def test_metrics_resumed_lifecycle_reads_its_own_part_of_a_shared_transcript(self, bobi_install, monkeypatch, tmp_path):
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='shared'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        transcript = tmp_path / "shared.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at, "message": {"content": text}})
                                        for kind, at, text in [
            ("user", "1970-01-01T00:00:01.100Z", "First lifecycle question"),
            ("assistant", "1970-01-01T00:00:01.800Z", "First answer"),
            ("user", "1970-01-01T00:00:04.100Z", "Resumed lifecycle question"),
            ("assistant", "1970-01-01T00:00:04.800Z", "Resumed answer"),
        ]))
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: transcript if session_id == "shared" else None)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        turns = {turn["turn_id"]: turn for turn in _client().get(base + "/turns", params=self.RANGE).json()["turns"]}
        assert turns["t-routine"]["conversation"]["prompt_snippet"] == "First lifecycle question"
        assert turns["t-unknown"]["conversation"]["prompt_snippet"] == "Resumed lifecycle question"

    def test_metrics_turn_list_carries_a_short_prompt_and_long_text_fits(self, bobi_install, monkeypatch, tmp_path):
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        prompt = "Giải thích nhật ký ghi trước. " * 1000
        transcript = tmp_path / "old-provider.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at, "message": {"content": text}})
                                        for kind, at, text in [("user", "1970-01-01T00:00:01.100Z", prompt),
                                                               ("assistant", "1970-01-01T00:00:01.800Z", "Xong.")]))
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        turns = _client().get(base + "/turns", params=self.RANGE).json()["turns"]
        routine = next(turn for turn in turns if turn["turn_id"] == "t-routine")
        assert len(routine["conversation"]["user_message"]) <= 512
        assert routine["conversation"]["user_message_truncated"] is True
        detail = _client().get(base + "/turns/t-routine")
        assert detail.status_code == 200
        assert detail.json()["conversation"]["user_message"].startswith("Giải thích")

    def _long_prompt_transcript(self, bobi_install, monkeypatch, tmp_path):
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        transcript = tmp_path / "old-provider.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at, "message": {"content": text}})
                                        for kind, at, text in [("user", "1970-01-01T00:00:01.100Z", "x" * 626 + " zebracrossing"),
                                                               ("assistant", "1970-01-01T00:00:01.800Z", "Done.")]) + "\n")
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        return transcript

    def test_metrics_turn_search_says_how_many_turns_it_could_read(self, bobi_install, monkeypatch, tmp_path):
        self._long_prompt_transcript(bobi_install, monkeypatch, tmp_path)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        client = _client()
        assert "search" not in client.get(base + "/turns", params=self.RANGE).json()
        full = client.get(base + "/turns", params={**self.RANGE, "q": "zebracrossing"}).json()
        assert full["search"] == {"loaded": 4, "searched": 4}

        # Out of time, s1's three turns go unread: the answer says so instead of claiming no match.
        monkeypatch.setattr("bobi.webapp.runtime.SEARCH_TRANSCRIPT_BUDGET_S", 0.0)
        fresh = _client()
        cut = fresh.get(base + "/turns", params={**self.RANGE, "q": "zebracrossing"}).json()
        assert cut["turns"] == []
        assert cut["search"] == {"loaded": 4, "searched": 1}
        # Fields the row already carries still match without the transcript.
        by_model = fresh.get(base + "/turns", params={**self.RANGE, "q": "sonnet"}).json()
        assert {turn["turn_id"] for turn in by_model["turns"]} == {"t-pro", "t-reused"}

    def test_metrics_turn_search_does_not_count_a_prompt_out_of_reach(self, bobi_install, monkeypatch, tmp_path):
        transcript = self._long_prompt_transcript(bobi_install, monkeypatch, tmp_path)
        with transcript.open("a") as handle:
            for _ in range(50):
                handle.write(json.dumps({"type": "assistant", "timestamp": "1970-01-01T00:10:00Z",
                                         "message": {"content": "y" * 100}}) + "\n")
        # The prompt sits before the last bytes the reader reaches.
        monkeypatch.setattr("bobi.chat_history._WINDOW_READ_BYTES", 2000)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        cut = _client().get(base + "/turns", params={**self.RANGE, "q": "zebracrossing"}).json()
        assert cut["turns"] == []
        assert cut["search"] == {"loaded": 4, "searched": 1}

    def test_metrics_turn_listing_finds_each_transcript_once_and_within_its_budget(
            self, bobi_install, monkeypatch, tmp_path):
        import bobi.chat_history

        self._long_prompt_transcript(bobi_install, monkeypatch, tmp_path)
        lookups = []
        find = bobi.chat_history.find_claude_transcript
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: lookups.append(session_id) or find(session_id))
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        client = _client()
        client.get(base + "/turns", params=self.RANGE)
        first = len(lookups)
        assert set(lookups) == {"old-provider"}
        # The found file is remembered; a refresh only checks it is still there.
        client.get(base + "/turns", params=self.RANGE)
        client.get(base + "/turns", params={**self.RANGE, "q": "zebracrossing"})
        assert len(lookups) == first
        # Out of time, a listing does not go looking for files.
        monkeypatch.setattr("bobi.webapp.runtime.LIST_TRANSCRIPT_BUDGET_S", 0.0)
        assert _client().get(base + "/turns", params=self.RANGE).status_code == 200
        assert len(lookups) == first

    def test_metrics_turn_reads_reuse_an_unchanged_transcript(self, bobi_install, monkeypatch, tmp_path):
        import bobi.chat_history

        transcript = self._long_prompt_transcript(bobi_install, monkeypatch, tmp_path)
        reads = []
        real = bobi.chat_history.read_transcript_detail
        monkeypatch.setattr("bobi.chat_history.read_transcript_detail",
                            lambda *args, **kwargs: reads.append(args) or real(*args, **kwargs))
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        client = _client()
        search = {**self.RANGE, "q": "zebracrossing"}
        assert len(client.get(base + "/turns", params=search).json()["turns"]) == 1
        first = len(reads)
        assert first == 1
        assert len(client.get(base + "/turns", params=search).json()["turns"]) == 1
        assert len(client.get(base + "/turns", params=self.RANGE).json()["turns"]) == 4
        assert len(reads) == first
        # A transcript that grew is read again.
        with transcript.open("a") as handle:
            handle.write(json.dumps({"type": "assistant", "timestamp": "1970-01-01T00:00:01.850Z",
                                     "message": {"content": "Also done."}}) + "\n")
        client.get(base + "/turns", params=search)
        assert len(reads) == first + 1
        # A read as slow as the reader's own time limit may be cut short, so it is not kept.
        monkeypatch.setattr("bobi.chat_history._WINDOW_READ_SECONDS", 0.0)
        slow = _client()
        slow.get(base + "/turns", params=search)
        slow.get(base + "/turns", params=search)
        assert len(reads) == first + 3

    def test_metrics_turn_search_matches_the_whole_prompt(self, bobi_install, monkeypatch, tmp_path):
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        prompt = "x" * 626 + " zebracrossing"
        assert len(prompt) == 640
        transcript = tmp_path / "old-provider.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at, "message": {"content": text}})
                                        for kind, at, text in [("user", "1970-01-01T00:00:01.100Z", prompt),
                                                               ("assistant", "1970-01-01T00:00:01.800Z", "Done.")]))
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"

        everything = _client().get(base + "/turns", params=self.RANGE).json()
        # An empty search is the plain listing.
        assert _client().get(base + "/turns", params={**self.RANGE, "q": "  "}).json() == everything
        matched = _client().get(base + "/turns", params={**self.RANGE, "q": "ZebraCrossing"}).json()
        assert [turn["turn_id"] for turn in matched["turns"]] == ["t-routine"]
        # The row stays bounded: only the match used the full prompt.
        assert matched["turns"][0]["conversation"]["user_message_truncated"] is True
        assert "zebracrossing" not in matched["turns"][0]["conversation"]["user_message"]
        assert matched["next_cursor"] == everything["next_cursor"]
        # The rest of the page comes back apart, so a thread can still be walked turn by turn.
        assert "unmatched_turns" not in everything
        assert [turn["turn_id"] for turn in matched["unmatched_turns"]] == ["t-unknown", "t-reused", "t-pro"]
        assert matched["unmatched_turns"][0] == next(
            turn for turn in everything["turns"] if turn["turn_id"] == "t-unknown")
        # The other listed fields still match, as they did in the browser.
        by_model = _client().get(base + "/turns", params={**self.RANGE, "q": "SONNET"}).json()["turns"]
        assert {turn["turn_id"] for turn in by_model} == {"t-pro", "t-reused"}
        assert _client().get(base + "/turns", params={**self.RANGE, "q": "no-such-text"}).json()["turns"] == []

        # Under load the listing skips transcripts it has no time for; a search reads them all.
        monkeypatch.setattr("bobi.webapp.runtime.LIST_TRANSCRIPT_BUDGET_S", 0.0)
        assert _client().get(base + "/turns", params=self.RANGE).json()["turns"][0]["conversation"]["user_message"] is None
        matched = _client().get(base + "/turns", params={**self.RANGE, "q": "zebracrossing"}).json()
        assert [turn["turn_id"] for turn in matched["turns"]] == ["t-routine"]

        # A word past the drawer's 32 KiB view of the prompt still matches.
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at, "message": {"content": text}})
                                        for kind, at, text in [("user", "1970-01-01T00:00:01.100Z",
                                                                "x" * 40000 + " farpastword"),
                                                               ("assistant", "1970-01-01T00:00:01.800Z", "Done.")]))
        far = _client().get(base + "/turns", params={**self.RANGE, "q": "farpastword"}).json()
        assert [turn["turn_id"] for turn in far["turns"]] == ["t-routine"]
        assert far["search"] == {"loaded": 4, "searched": 4}

    # A bootstrap prompt carrying a Slack event, as the manager receives it. The workflow
    # prompt (`"workflow"` below) is captured from a real run of the orchestrator.
    EVENT_PROMPT = (
        "You are an agent in a bobi deployment. How you receive events: one at a time.\n"
        "Event: slack/message\nconversation: slack:T1:channel:C1:thread:1.2\nchannel_name: ops-room\n"
        "please check zebraevent logs")

    @pytest.mark.parametrize("prompt,word,found", [
        # A workflow turn's title is its step instruction; the launch task is still searched.
        ("workflow", "zebracredential", True),
        ("workflow", "implement the change", True),
        # An event's title stops at its metadata; the lines after it are still searched.
        (EVENT_PROMPT, "zebraevent", True),
        # Text bobi wraps around the operator's input is not.
        ("workflow", "repeat steps", False),
        ("workflow", "background for run", False),
        ("workflow", "yaml", False),
        (EVENT_PROMPT, "bobi deployment", False),
        (EVENT_PROMPT, "channel_name", False),
        (EVENT_PROMPT, "slack/message", False),
        # A workflow whose task forwards an event is still a workflow prompt: its task is kept whole.
        ("forwarded-event", "acme/api", True),
        ("forwarded-event", "zebraforward", True),
        ("forwarded-event", "repeat steps", False),
        ("forwarded-event", "background for run", False),
    ], ids=["workflow-task", "workflow-instruction", "after-event-metadata", "step-header", "background-header",
            "yaml-fence", "bootstrap", "event-metadata", "event-line", "forwarded-task-metadata",
            "forwarded-task-content", "forwarded-step-header", "forwarded-background-header"])
    def test_metrics_turn_search_matches_operator_text_not_framework_text(
            self, bobi_install, monkeypatch, tmp_path, prompt, word, found):
        from bobi.metrics.store import connect

        if prompt == "workflow":
            prompt = _first_workflow_prompt(monkeypatch, "rotate the zebracredential on staging",
                                            "Implement the change.")
            assert "zebracredential" in prompt and "repeat steps" in prompt and "```yaml" in prompt
        elif prompt == "forwarded-event":
            prompt = _first_workflow_prompt(
                monkeypatch, "Event: github/push\nrepo: acme/api\nplease deploy zebraforward", "Deploy it.")
            assert "Event: github/push" in prompt and "repeat steps" in prompt

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        transcript = tmp_path / "old-provider.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at, "message": {"content": text}})
                                        for kind, at, text in [("user", "1970-01-01T00:00:01.100Z", prompt),
                                                               ("assistant", "1970-01-01T00:00:01.800Z", "Done.")]))
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        listed = _client().get(base + "/turns", params={**self.RANGE, "q": word}).json()
        assert [turn["turn_id"] for turn in listed["turns"]] == (["t-routine"] if found else [])

    @pytest.mark.parametrize("brain", ["claude", "codex"])
    def test_metrics_old_turn_uses_timestamp_window_before_tail_limit(self, bobi_install, monkeypatch, tmp_path, brain):
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider',brain=? WHERE session_id='s1'", (brain,))
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        transcript = tmp_path / "old-provider.jsonl"
        entries = [("user", "1970-01-01T00:00:01.100Z", "Original prompt"),
                   ("assistant", "1970-01-01T00:00:01.900Z", "Original answer")]
        entries.extend(("assistant", "1970-01-01T00:00:03.000Z", "Later answer") for _ in range(250))
        entries.append(("user", "", "Missing timestamp must not match"))
        if brain == "claude":
            rows = [{"type": role, "timestamp": at, "message": {"content": text}} for role, at, text in entries]
        else:
            rows = [{"type": "response_item", "timestamp": at, "payload": {"type": "message", "role": role,
                     "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
                    for role, at, text in entries]
        transcript.write_text("\n".join(map(json.dumps, rows)))
        monkeypatch.setattr(f"bobi.chat_history.find_{brain}_{'transcript' if brain == 'claude' else 'rollout'}",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        io = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/turns/t-routine").json()["conversation"]
        assert io["input"] == "Original prompt"
        assert io["response"] == "Original answer"
        assert io["origin"] == {"source": "user"}
        assert "Later answer" not in json.dumps(io)
        assert "Missing timestamp" not in json.dumps(io)


def _add_design_slot(agents_dir, name, description="An idea, not installed."):
    src = agents_dir / name / "src"
    src.mkdir(parents=True)
    (src / "agent.yaml").write_text(yaml.dump({"agent": name}))
    (src / "agent.md").write_text(f"# {name}\n\n{description}\n")


# --- security ------------------------------------------------------------

class TestSecurity:
    def test_api_requires_token(self, bobi_install):
        c = _testclient()
        assert c.get("/api/dashboard").status_code == 403
        c.headers.update({"x-bobi-webui-token": "wrong"})
        assert c.get("/api/dashboard").status_code == 403
        c.headers.update({"x-bobi-webui-token": TOKEN})
        assert c.get("/api/dashboard").status_code == 200

    def test_host_guard(self, bobi_install):
        app = server.build_app(token=TOKEN)
        c = TestClient(app, base_url="http://evil.example")
        c.headers.update({"x-bobi-webui-token": TOKEN})
        assert c.get("/api/dashboard").status_code == 403

    def test_page_is_open_and_embeds_token(self, bobi_install):
        r = _testclient().get("/")   # no token header
        assert r.status_code == 200
        assert TOKEN in r.text


# --- dashboard -----------------------------------------------------------

class TestDashboard:
    def test_lists_installed_agent(self, bobi_install):
        r = _client().get("/api/dashboard")
        assert r.status_code == 200
        agents = r.json()["agents"]
        names = [a["name"] for a in agents]
        assert bobi_install.agent_name in names
        card = agents[names.index(bobi_install.agent_name)]
        assert card["installed"] is True
        assert card["running"] is False
        assert card["pid"] == 0

    def test_lists_design_only_slot(self, bobi_install):
        _add_design_slot(bobi_install.agents_dir, "ideas")
        agents = _client().get("/api/dashboard").json()["agents"]
        card = next(a for a in agents if a["name"] == "ideas")
        assert card["installed"] is False
        assert card["running"] is False
        assert "not installed" in card["description"]

    def test_running_agent_shows_pid(self, bobi_install):
        import os

        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))   # a live pid: this test process
        agents = _client().get("/api/dashboard").json()["agents"]
        card = next(a for a in agents if a["name"] == bobi_install.agent_name)
        assert card["running"] is True
        assert card["pid"] == os.getpid()

    def test_stale_pid_reads_stopped(self, bobi_install):
        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("999999999")
        agents = _client().get("/api/dashboard").json()["agents"]
        card = next(a for a in agents if a["name"] == bobi_install.agent_name)
        assert card["running"] is False


# --- per-agent status ------------------------------------------------------

class TestStatus:
    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/status").status_code == 404

    def test_known_agent(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/status")
        assert r.status_code == 200
        assert r.json()["installed"] is True


# --- spend (observability #733) --------------------------------------------

def _seed_session(sessions_dir, name, *, cost, role="engineer",
                  model_usage=None):
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "role": role, "total_cost_usd": cost}
    if model_usage is not None:
        data["model_usage"] = model_usage
    (d / "state.json").write_text(json.dumps(data))


class TestSpend:
    def test_team_spend_folds_sessions(self, bobi_install):
        sd = bobi_install.sessions_dir
        _seed_session(sd, "director", cost=0.60, role="director",
                      model_usage={"anthropic:opus": {"cost_usd": 0.60}})
        _seed_session(sd, "eng", cost=0.40, role="engineer",
                      model_usage={"anthropic:sonnet": {"cost_usd": 0.40}})
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/spend")
        assert r.status_code == 200
        body = r.json()
        assert body["total_cost_usd"] == 1.0
        assert body["sessions_counted"] == 2
        assert body["by_role"] == {"director": 0.6, "engineer": 0.4}
        # by_model ranked highest-first
        assert list(body["by_model"]) == ["anthropic:opus", "anthropic:sonnet"]

    def test_team_spend_empty(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/spend")
        assert r.status_code == 200
        assert r.json()["total_cost_usd"] == 0.0
        assert r.json()["sessions_counted"] == 0

    def test_team_spend_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/spend").status_code == 404

    def test_team_spend_estimates_codex_tokens(self, bobi_install):
        # A codex-brained session records tokens with the cached split but
        # cost 0 (#760): the wire carries a fold-time estimate + raw token
        # volume on the additive fields, and recorded dollars stay 0.
        _seed_session(bobi_install.sessions_dir, "dev", role="dev", cost=0.0,
                      model_usage={"openai:gpt-5.6": {
                          "cost_usd": 0.0, "input_tokens": 1_000_000,
                          "cached_input_tokens": 900_000,
                          "output_tokens": 10_000}})
        body = _client().get(
            f"/api/agents/{bobi_install.agent_name}/spend").json()
        assert body["total_cost_usd"] == 0.0
        assert body["estimated_cost_usd"] == 1.25
        assert body["estimated_by_model"] == {"openai:gpt-5.6": 1.25}
        assert body["tokens_by_model"]["openai:gpt-5.6"] == {
            "input_tokens": 1_000_000, "cached_input_tokens": 900_000,
            "output_tokens": 10_000}

    def test_fleet_spend_totals(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "director", cost=1.25)
        _seed_session(bobi_install.sessions_dir, "dev", role="dev", cost=0.0,
                      model_usage={"openai:gpt-5.6": {
                          "cost_usd": 0.0, "input_tokens": 1_000_000,
                          "cached_input_tokens": 900_000,
                          "output_tokens": 10_000}})
        r = _client().get("/api/fleet/spend")
        assert r.status_code == 200
        body = r.json()
        assert body["total_cost_usd"] == 1.25
        assert body["estimated_cost_usd"] == 1.25
        assert body["sessions_counted"] == 2
        team = next(t for t in body["teams"]
                    if t["name"] == bobi_install.agent_name)
        assert team["total_cost_usd"] == 1.25
        assert team["estimated_cost_usd"] == 1.25
        assert team["sessions_counted"] == 2

    def test_spend_read_does_not_create_sessions_dir(self, bobi_install):
        import shutil
        # A read endpoint must not mutate disk: remove the sessions dir the
        # fixture pre-creates and confirm a spend GET leaves it absent.
        shutil.rmtree(bobi_install.sessions_dir)
        c = _client()
        assert c.get(f"/api/agents/{bobi_install.agent_name}/spend").status_code == 200
        assert c.get("/api/fleet/spend").status_code == 200
        assert not bobi_install.sessions_dir.exists()

    def test_null_cost_session_does_not_500(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "broken", cost=None,
                      model_usage={"anthropic:opus": {"cost_usd": None}})
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/spend")
        assert r.status_code == 200
        assert r.json()["total_cost_usd"] == 0.0

    def test_fleet_spend_empty_install(self, bobi_install):
        body = _client().get("/api/fleet/spend").json()
        assert body["total_cost_usd"] == 0.0
        # the installed test agent is listed even with no spend
        names = [t["name"] for t in body["teams"]]
        assert bobi_install.agent_name in names


# --- system health (observability #733) --------------------------------------

def _seed_active_session(sessions_dir, name, *, status, role="engineer"):
    """A registry entry list_active() keeps: an active status and pid 0
    (no liveness check), so the health fold sees it."""
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(
        {"name": name, "role": role, "status": status, "pid": 0}))


class TestHealth:
    # manager_session_name: bobi-<agent>-<entry_role>; the fixture's
    # agent.yaml declares entry_point "director".
    MGR = "bobi-test-agent-director"

    def _get(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/health")
        assert r.status_code == 200
        return r.json()

    def test_stopped_team(self, bobi_install):
        body = self._get(bobi_install)
        # Local teams share the webapp's host: live by construction, no
        # heartbeats, no supervisor trail.
        assert body["reachability"] == "live"
        assert body["last_heartbeat_at"] is None
        assert body["lifecycle"] == []
        mgr = body["manager"]
        assert mgr["status"] == "stopped"
        assert mgr["running"] is False
        assert mgr["healthy"] is False
        assert mgr["pid"] == 0
        assert mgr["restart_count"] is None
        assert body["sessions"] == []

    def test_running_manager_reports_registry_status(self, bobi_install):
        import os

        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))   # a live pid: this process
        _seed_active_session(bobi_install.sessions_dir, self.MGR,
                             status="idle", role="director")
        _seed_active_session(bobi_install.sessions_dir, "eng",
                             status="running")
        body = self._get(bobi_install)
        mgr = body["manager"]
        assert mgr["running"] is True
        assert mgr["pid"] == os.getpid()
        assert mgr["status"] == "idle"     # the registry's word, not just "up"
        assert mgr["healthy"] is True
        # manager-first ordering, roles and statuses carried through
        sessions = body["sessions"]
        assert sessions[0]["name"] == self.MGR
        assert {"name": "eng", "role": "engineer",
                "status": "running"} in sessions

    def test_boot_window_reads_starting(self, bobi_install):
        import os

        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
        # pid alive but no registered manager session yet: fail open to
        # "starting" (the same verdict the hosted sidecar reports pre-spawn).
        body = self._get(bobi_install)
        assert body["manager"]["status"] == "starting"
        assert body["manager"]["running"] is True

    def test_stale_pid_reads_stopped(self, bobi_install):
        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("999999999")
        _seed_active_session(bobi_install.sessions_dir, self.MGR,
                             status="idle", role="director")
        body = self._get(bobi_install)
        assert body["manager"]["status"] == "stopped"
        assert body["manager"]["running"] is False

    def test_terminal_sessions_not_listed(self, bobi_install):
        d = bobi_install.sessions_dir / "done-run"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps(
            {"name": "done-run", "role": "engineer", "status": "completed"}))
        body = self._get(bobi_install)
        assert body["sessions"] == []

    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/health").status_code == 404


# --- session logs (observability #733 vertical 3) ---------------------------

def _seed_history_session(sessions_dir, name, *, status, role="engineer",
                          error="", terminal_at=0.0, last_activity=0.0,
                          pid=0, cost=0.0):
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "name": name, "role": role, "status": status, "pid": pid,
        "error": error, "terminal_at": terminal_at,
        "last_activity": last_activity, "total_cost_usd": cost,
        "session_id": f"sid-{name}",
    }))


class TestSessionLog:
    MGR = "bobi-test-agent-director"

    def _get(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/sessions")
        assert r.status_code == 200
        return r.json()

    def test_empty_history(self, bobi_install):
        assert self._get(bobi_install) == {
            "sessions": [],
            "counts": {"active": 0, "completed": 0, "failed": 0, "crashed": 0},
            "truncated": False,
        }

    def test_outcomes_listed_newest_first(self, bobi_install):
        import os

        sd = bobi_install.sessions_dir
        _seed_history_session(sd, "old-done", status="completed",
                              terminal_at=100.0, last_activity=100.0)
        _seed_history_session(sd, "boom", status="failed",
                              error="turn errored", terminal_at=200.0,
                              last_activity=200.0, cost=0.25)
        _seed_history_session(sd, "live", status="running", pid=os.getpid(),
                              last_activity=300.0)
        body = self._get(bobi_install)
        assert [s["name"] for s in body["sessions"]] == \
            ["live", "boom", "old-done"]
        boom = body["sessions"][1]
        assert boom["status"] == "failed"
        assert boom["error"] == "turn errored"
        assert boom["terminal_at"] == 200.0
        assert boom["session_id"] == "sid-boom"
        assert boom["total_cost_usd"] == 0.25
        assert boom["ended"] is True
        live = body["sessions"][0]
        assert live["error"] == ""
        assert live["terminal_at"] is None   # null, never omitted
        assert live["ended"] is False
        assert body["counts"] == {"active": 1, "completed": 1,
                                  "failed": 1, "crashed": 0}
        assert body["truncated"] is False

    def test_dead_pid_reads_crashed_not_running(self, bobi_install):
        _seed_history_session(bobi_install.sessions_dir, "zombie",
                              status="running", pid=999999999,
                              last_activity=100.0)
        body = self._get(bobi_install)
        [z] = body["sessions"]
        assert z["status"] == "crashed"
        assert z["error"]                     # the honest-status message
        assert z["terminal_at"] is not None
        assert z["ended"] is True
        assert body["counts"] == {"active": 0, "completed": 0,
                                  "failed": 0, "crashed": 1}

    def test_manager_flagged(self, bobi_install):
        _seed_history_session(bobi_install.sessions_dir, self.MGR,
                              status="completed", role="director",
                              terminal_at=1.0, last_activity=1.0)
        [row] = self._get(bobi_install)["sessions"]
        assert row["is_manager"] is True

    def test_legacy_done_counts_completed(self, bobi_install):
        _seed_history_session(bobi_install.sessions_dir, "old",
                              status="done", last_activity=1.0)
        assert self._get(bobi_install)["counts"]["completed"] == 1

    def test_error_status_counts_failed(self, bobi_install):
        # "error" is still written for turn-level failures (rotation death,
        # monitor timeouts) - the log counts it as a failure.
        _seed_history_session(bobi_install.sessions_dir, "curator",
                              status="error", last_activity=1.0)
        assert self._get(bobi_install)["counts"]["failed"] == 1

    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/sessions").status_code == 404


# --- lifecycle actions -----------------------------------------------------

class _FakeStartup:
    pid = 4242


class _FakeSpawn:
    startup = _FakeStartup()


# --- hosted onboarding (the setup app mounted under /setup) ----------------

class TestClaudeAvailable:
    def test_absolute_resolution_counts(self, tmp_path, monkeypatch):
        cli = tmp_path / "claude"
        cli.write_text("#!/bin/sh\n")
        monkeypatch.setattr("bobi.brain.claude.shutil.which",
                            lambda name: str(cli))
        assert server._claude_available() is True

    def test_cwd_relative_fallback_is_not_availability(self, tmp_path,
                                                       monkeypatch):
        # On Linux with no CLI on PATH the resolver falls back to the bare
        # name "claude" (correct for exec). A stray file of that name in the
        # server's CWD must not read as an installed CLI.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "claude").mkdir()
        monkeypatch.setattr("bobi.brain.claude.shutil.which", lambda name: None)
        monkeypatch.setattr("bobi.brain.claude.platform.system", lambda: "Linux")
        assert server._claude_available() is False


class TestSetupHosting:
    def _open(self, client, monkeypatch, name="new-team"):
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        return client.post("/api/setup/open", json={"name": name})

    def test_no_session_redirects_to_shell(self, bobi_install):
        c = _client()
        r = c.get("/setup/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/#/setup"

    def test_open_creates_session(self, bobi_install, monkeypatch):
        c = _client()
        r = self._open(c, monkeypatch)
        assert r.status_code == 200
        assert r.json() == {"url": "/setup/new-team/", "name": "new-team",
                            "resumed": False}
        cur = c.get("/api/setup/current").json()
        assert cur == {"active": True, "name": "new-team",
                       "sessions": ["new-team"]}
        # setup state persisted under the slot's run root
        from bobi.setup.state import SetupState
        from bobi import paths
        state = SetupState.load(paths.agent_run_root("new-team"))
        assert state is not None
        assert state.team_name == "new-team"

    def test_open_resumes_unfinished_session(self, bobi_install, monkeypatch):
        c = _client()
        assert self._open(c, monkeypatch).json()["resumed"] is False
        assert self._open(c, monkeypatch).json()["resumed"] is True

    def test_open_passes_model_to_hosted_setup_app(self, bobi_install,
                                                   monkeypatch):
        from fastapi import FastAPI

        seen = {}

        def fake_build_app(*args, **kwargs):
            seen["model"] = kwargs.get("model")
            return FastAPI()

        monkeypatch.setattr(server, "_claude_available", lambda: True)
        monkeypatch.setattr("bobi.setup.webui.server.build_app", fake_build_app)

        c = _client()
        r = c.post("/api/setup/open",
                   json={"name": "new-team", "model": "sonnet"})

        assert r.status_code == 200
        assert seen["model"] == "sonnet"

    def test_hosted_page_serves_with_base_and_token(self, bobi_install,
                                                    monkeypatch):
        c = _client()
        self._open(c, monkeypatch)
        r = c.get("/setup/new-team/")
        assert r.status_code == 200
        assert TOKEN in r.text
        assert '"/setup/new-team/static/app.js"' in r.text

    def test_hosted_setup_supports_concurrent_sessions(
            self, bobi_install, monkeypatch):
        c = _client()
        assert self._open(c, monkeypatch, name="alpha").json()["url"] \
            == "/setup/alpha/"
        assert self._open(c, monkeypatch, name="beta").json()["url"] \
            == "/setup/beta/"
        cur = c.get("/api/setup/current").json()
        assert cur == {"active": True, "name": "alpha",
                       "sessions": ["alpha", "beta"]}
        assert c.get("/setup/alpha/api/state").json()["team_name"] == "alpha"
        assert c.get("/setup/beta/api/state").json()["team_name"] == "beta"

    def test_hosted_api_requires_token(self, bobi_install, monkeypatch):
        # The mounted sub-app must still enforce its /api token check even
        # though its url.path carries the /setup prefix (the root_path fix
        # in webui_common.security). Both clients share one app so they see
        # the same active onboarding session.
        app = server.build_app(token=TOKEN)
        c = TestClient(app, base_url="http://127.0.0.1")
        c.headers.update({"x-bobi-webui-token": TOKEN})
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        assert c.post("/api/setup/open",
                      json={"name": "new-team"}).status_code == 200
        bare = TestClient(app, base_url="http://127.0.0.1")
        assert bare.get("/setup/new-team/api/state").status_code == 403
        assert c.get("/setup/new-team/api/state").status_code == 200

    def test_hosted_finish_returns_home_without_launching(self, bobi_install,
                                                          monkeypatch):
        # Finish returns to the dashboard; launching stays a deliberate
        # action on the agent's card/dashboard (never automatic).
        def fail_start(root, **kw):
            raise AssertionError("finish must not launch")

        monkeypatch.setattr(service, "start_team", fail_start)
        monkeypatch.setattr(service, "spawn_team", fail_start)
        c = _client()
        self._open(c, monkeypatch)
        body = c.post("/setup/new-team/api/finish").json()
        assert body["finished"] is True
        assert body["redirect"] == "/#/"
        # The onboarding slot is released: current reports inactive and
        # /setup/ redirects back to the shell.
        assert c.get("/api/setup/current").json()["active"] is False
        assert c.get("/setup/",
                     follow_redirects=False).status_code == 307

    def test_open_mode_deep_links_the_editor(self, bobi_install, monkeypatch):
        import yaml as _yaml

        from bobi import paths
        src = paths.agent_source_dir(bobi_install.agent_name)
        src.mkdir(parents=True, exist_ok=True)
        (src / "agent.yaml").write_text(_yaml.dump(
            {"agent": bobi_install.agent_name}))
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open",
                           json={"name": bobi_install.agent_name,
                                 "mode": "open"})
        assert r.status_code == 200
        assert r.json()["url"].startswith(
            f"/setup/{bobi_install.agent_name}/?open=")
        assert str(src) in r.json()["url"]

    def test_open_mode_requires_source(self, bobi_install, monkeypatch):
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open",
                           json={"name": "no-src-slot", "mode": "open"})
        assert r.status_code == 404

    def test_open_mode_resolves_nested_source(self, bobi_install, monkeypatch):
        # An older flow could land a template in a src/ SUBFOLDER
        # (src/eng-team/); a single team child still resolves as the source.
        import yaml as _yaml

        from bobi import paths
        nested = paths.agent_source_dir("legacy-slot") / "eng-team"
        nested.mkdir(parents=True)
        (nested / "agent.yaml").write_text(_yaml.dump({"agent": "eng-team"}))
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open",
                           json={"name": "legacy-slot", "mode": "open"})
        assert r.status_code == 200
        assert str(nested) in r.json()["url"]

    def test_finish_renames_slot_to_team_name(self, bobi_install, monkeypatch):
        # The slot opens under a placeholder name; the team gets its real
        # name during setup (template pick / auto-name). Finish moves the
        # whole slot dir to match (#526: a slot IS its team).
        from bobi import paths

        c = _client()
        self._open(c, monkeypatch)   # slot "new-team"
        # Name the team through the real flow (mutates the parked state).
        r = c.post("/setup/new-team/api/rename", json={"name": "eng-team"})
        assert r.status_code == 200
        body = c.post("/setup/new-team/api/finish").json()
        assert body["redirect"] == "/#/"
        assert not paths.agent_dir("new-team").exists()
        assert paths.agent_run_root("eng-team").is_dir()

    def test_create_rename_moves_placeholder_source_and_run_slot(
            self, bobi_install, monkeypatch):
        # The local web app starts a from-scratch team in a placeholder slot
        # (usually new-agent). Renaming during setup should move the editable
        # source out of that slot immediately, and Finish should move the
        # run/ state beside it so the final folder is the chosen name.
        from bobi import paths

        c = _client()
        self._open(c, monkeypatch, name="new-agent")
        old_src = paths.agent_source_dir("new-agent")
        r = c.post("/setup/new-agent/api/start",
                   json={"mode": "create", "location": str(old_src)})
        assert r.status_code == 200
        old_src.mkdir(parents=True, exist_ok=True)
        (old_src / "agent.yaml").write_text("agent: new-agent\n")
        r = c.post("/setup/new-agent/api/rename",
                   json={"name": "Field Ops"})
        assert r.status_code == 200
        assert not old_src.exists()
        assert paths.agent_source_dir("field-ops").is_dir()

        body = c.post("/setup/new-agent/api/finish").json()
        assert body["redirect"] == "/#/"
        assert not paths.agent_dir("new-agent").exists()
        assert paths.agent_source_dir("field-ops").is_dir()
        assert paths.agent_run_root("field-ops").is_dir()

    def test_finish_does_not_merge_custom_source_into_existing_slot(
            self, bobi_install, monkeypatch, tmp_path):
        from bobi import paths

        c = _client()
        self._open(c, monkeypatch, name="new-agent")
        custom_src = tmp_path / "field-ops-src"
        existing_src = paths.agent_source_dir("field-ops")
        existing_src.mkdir(parents=True)
        (existing_src / "agent.yaml").write_text("agent: field-ops\n")
        r = c.post("/setup/new-agent/api/start",
                   json={"mode": "create", "location": str(custom_src)})
        assert r.status_code == 200
        r = c.post("/setup/new-agent/api/rename",
                   json={"name": "Field Ops"})
        assert r.status_code == 200

        body = c.post("/setup/new-agent/api/finish").json()
        assert body["redirect"] == "/#/"
        assert paths.agent_run_root("new-agent").is_dir()
        assert not paths.agent_run_root("field-ops").exists()

    def test_open_requires_claude(self, bobi_install, monkeypatch):
        monkeypatch.setattr(server, "_claude_available", lambda: False)
        r = _client().post("/api/setup/open", json={"name": "x"})
        assert r.status_code == 409

    def test_open_rejects_bad_name(self, bobi_install, monkeypatch):
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open", json={"name": "../evil"})
        assert r.status_code == 400


# --- subagents + chat ------------------------------------------------------

def _entry(name, role="engineer", **kw):
    from bobi.sdk import SessionEntry
    return SessionEntry(name=name, role=role, **kw)


class TestSubagents:
    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/subagents").status_code == 404

    def test_roster_with_manager_badge(self, bobi_install, monkeypatch):
        mgr = f"bobi-{bobi_install.agent_name}-director"
        monkeypatch.setattr(
            service, "list_agents",
            lambda root: [_entry("bobi-worker-1"), _entry(mgr, role="director")])
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/subagents")
        assert r.status_code == 200
        subs = r.json()["subagents"]
        # The manager orders first and carries the badge.
        assert subs[0]["name"] == mgr
        assert subs[0]["is_manager"] is True
        assert subs[1]["is_manager"] is False

    def test_messages_from_chat_log(self, bobi_install):
        from bobi.chat_history import append_chat
        append_chat(bobi_install.repo_path, "bobi-worker-1", "user", "hi")
        append_chat(bobi_install.repo_path, "bobi-worker-1", "agent", "hello!")
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}"
            "/subagents/bobi-worker-1/messages")
        assert r.status_code == 200
        assert r.json()["messages"] == [
            {"role": "user", "text": "hi"},
            {"role": "agent", "text": "hello!"},
        ]

    def test_messages_bad_session_name_404(self, bobi_install):
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}"
            "/subagents/..%2Fetc/messages")
        assert r.status_code == 404

    def test_messages_from_codex_rollout(self, bobi_install, monkeypatch,
                                         tmp_path):
        """A codex-brained session renders its rollout, not a blank panel —
        the fleet-UI regression this fixes. The runtime dispatches on the
        recorded ``.brain`` to the Codex reader instead of the Claude one."""
        import json

        from bobi import paths

        sessions = paths.sessions_dir(bobi_install.repo_path)
        (sessions / "bobi-worker-1.id").write_text("codex-thread-42")
        (sessions / "bobi-worker-1.brain").write_text("codex")

        codex_home = tmp_path / "codex"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        rollout = (codex_home / "sessions" / "2026" / "07" / "09"
                   / "rollout-2026-07-09T05-12-25-codex-thread-42.jsonl")
        rollout.parent.mkdir(parents=True)
        rollout.write_text("\n".join(json.dumps(r) for r in [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "are you alive?"}]}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Yes, standing by."}]}},
        ]) + "\n")

        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}"
            "/subagents/bobi-worker-1/messages")
        assert r.status_code == 200
        assert r.json()["messages"] == [
            {"role": "user", "text": "are you alive?"},
            {"role": "agent", "text": "Yes, standing by."},
        ]


class TestChat:
    """Chat is submit-then-poll: POST returns a message id right away; the
    deliver runs in a background thread and its outcome lands on the job
    status endpoint (the reply itself reaches the transcript)."""

    def _await_job(self, client, agent, message_id, tries=100):
        import time
        for _ in range(tries):
            r = client.get(f"/api/agents/{agent}/chat/{message_id}")
            assert r.status_code == 200
            job = r.json()
            if job["status"] != "pending":
                return job
            time.sleep(0.02)
        raise AssertionError("chat job never resolved")

    def test_chat_submits_and_resolves(self, bobi_install, monkeypatch):
        seen = {}

        def fake_ask(root, agent, text, **kw):
            seen.update(root=root, agent=agent, text=text)
            return service.MessageResult(address=agent, response="done!")

        monkeypatch.setattr(service, "ask", fake_ask)
        c = _client()
        r = c.post(
            f"/api/agents/{bobi_install.agent_name}/chat",
            json={"subagent": "bobi-worker-1", "text": "go"})
        assert r.status_code == 200
        mid = r.json()["message_id"]
        assert mid
        job = self._await_job(c, bobi_install.agent_name, mid)
        assert job == {"status": "done"}
        assert seen["root"] == bobi_install.repo_path
        assert seen["agent"] == "bobi-worker-1"

    def test_an_empty_subagent_means_the_manager_on_this_runtime_too(
            self, bobi_install, monkeypatch):
        """#987 - one route, two runtimes, one meaning.

        The route documents `subagent` as optional (only `text` is required),
        and the hosted runtime has always read an empty one as "the team
        manager" (`supervisor/admin.py`). `LocalRuntime` passed the empty
        string straight to `service.ask`, which rejected it on its membership
        guard as `unknown agent ''`, so the same request behaved differently
        on `bobi app` than on the fleet.
        """
        seen = {}

        def fake_ask(root, agent, text, **kw):
            seen.update(root=root, agent=agent, text=text)
            return service.MessageResult(address=agent, response="ack")

        monkeypatch.setattr(service, "ask", fake_ask)
        c = _client()
        r = c.post(f"/api/agents/{bobi_install.agent_name}/chat",
                   json={"subagent": "", "text": "continue this"})
        assert r.status_code == 200
        job = self._await_job(c, bobi_install.agent_name, r.json()["message_id"])
        assert job == {"status": "done"}
        # The same name the supervisor resolves, not a hand-built string.
        assert seen["agent"] == service.manager_session_name(
            bobi_install.repo_path)

    def test_an_absent_subagent_key_resolves_the_same_way(self, bobi_install,
                                                          monkeypatch):
        seen = {}
        monkeypatch.setattr(service, "ask", lambda root, agent, text, **kw: (
            seen.update(agent=agent),
            service.MessageResult(address=agent, response="ack"))[1])
        c = _client()
        r = c.post(f"/api/agents/{bobi_install.agent_name}/chat",
                   json={"text": "continue this"})
        assert r.status_code == 200
        assert self._await_job(c, bobi_install.agent_name,
                               r.json()["message_id"]) == {"status": "done"}
        assert seen["agent"] == service.manager_session_name(
            bobi_install.repo_path)

    def test_chat_empty_message_400(self, bobi_install):
        r = _client().post(
            f"/api/agents/{bobi_install.agent_name}/chat",
            json={"subagent": "x", "text": "  "})
        assert r.status_code == 400

    def test_chat_delivery_failure_lands_on_job(self, bobi_install,
                                                monkeypatch):
        def fake_ask(root, agent, text, **kw):
            raise service.MessageDeliveryError("session 'x' process is dead")

        monkeypatch.setattr(service, "ask", fake_ask)
        c = _client()
        r = c.post(
            f"/api/agents/{bobi_install.agent_name}/chat",
            json={"subagent": "x", "text": "hi"})
        assert r.status_code == 200
        job = self._await_job(c, bobi_install.agent_name, r.json()["message_id"])
        assert job["status"] == "error"
        assert "dead" in job["error"]

    def test_chat_unknown_agent_404(self, bobi_install):
        r = _client().post("/api/agents/nope/chat",
                           json={"subagent": "x", "text": "hi"})
        assert r.status_code == 404

    def test_chat_status_unknown_message_404(self, bobi_install):
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}/chat/deadbeef")
        assert r.status_code == 404


class TestLifecycle:
    def test_start_spawns(self, bobi_install, monkeypatch):
        seen = {}

        def fake_spawn(root, **kw):
            seen["root"] = root
            return _FakeSpawn()

        monkeypatch.setattr(service, "spawn_team", fake_spawn)
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/start")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "pid": 4242}
        assert seen["root"] == bobi_install.repo_path

    def test_start_unknown_404(self, bobi_install):
        assert _client().post("/api/agents/nope/start").status_code == 404

    def test_start_already_running(self, bobi_install, monkeypatch):
        def fake_spawn(root, **kw):
            raise service.AlreadyRunning(77)

        monkeypatch.setattr(service, "spawn_team", fake_spawn)
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/start")
        assert r.status_code == 409
        assert r.json()["pid"] == 77

    def test_start_preflight_failed(self, bobi_install, monkeypatch):
        class FakeValidation:
            def format(self):
                return "missing SLACK_BOT_TOKEN"

        def fake_spawn(root, **kw):
            raise service.PreflightFailed(FakeValidation())

        monkeypatch.setattr(service, "spawn_team", fake_spawn)
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/start")
        assert r.status_code == 409
        assert "SLACK_BOT_TOKEN" in r.json()["report"]

    def test_stop(self, bobi_install, monkeypatch):
        monkeypatch.setattr(
            service, "stop_team",
            lambda root, **kw: service.StopResult(pid=42, stopped=True))
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/stop")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["stopped"] is True

    def test_stop_not_running_is_ok(self, bobi_install, monkeypatch):
        monkeypatch.setattr(
            service, "stop_team", lambda root, **kw: service.StopResult(pid=0))
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/stop")
        assert r.json()["ok"] is True

    def test_restart(self, bobi_install, monkeypatch):
        calls = []
        monkeypatch.setattr(
            service, "stop_team",
            lambda root, **kw: calls.append("stop")
            or service.StopResult(pid=42, stopped=True))
        monkeypatch.setattr(
            service, "spawn_team",
            lambda root, **kw: calls.append("spawn") or _FakeSpawn())
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/restart")
        assert r.status_code == 200
        assert calls == ["stop", "spawn"]


class TestMultiAgentRealService:
    """Regression tests for #706: one webapp process serves service-backed
    endpoints for MULTIPLE agents.

    No service monkeypatching here on purpose. The bug was the process-global
    runtime bind (`service._bind` -> `paths.bind_root`, which refuses to rebind
    to a second root), and only real service calls exercise it - the mocked
    tests above never see the bind."""

    @pytest.fixture
    def two_agents(self, tmp_path, monkeypatch):
        from bobi import paths

        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        # The webapp daemon process never binds a runtime root; start unbound.
        paths.bind_root(None)
        names = ["alpha-team", "beta-team"]
        for name in names:
            pkg = home / "agents" / name / "run" / "package"
            pkg.mkdir(parents=True)
            (pkg / "agent.yaml").write_text(yaml.dump({
                "version": "0.0.1",
                "agent": name,
                "entry_point": "director",
            }))
        yield names
        paths.bind_root(None)

    def test_roster_serves_both_agents(self, two_agents):
        c = _client()
        for name in two_agents:
            r = c.get(f"/api/agents/{name}/subagents")
            assert r.status_code == 200, f"{name}: {r.text}"
            assert r.json()["subagents"] == []

    def test_stop_serves_both_agents(self, two_agents):
        c = _client()
        for name in two_agents:
            r = c.post(f"/api/agents/{name}/stop")
            assert r.status_code == 200, f"{name}: {r.text}"
            assert r.json()["ok"] is True

    def test_webapp_process_stays_unbound(self, two_agents):
        from bobi import paths

        c = _client()
        for name in two_agents:
            assert c.get(f"/api/agents/{name}/subagents").status_code == 200
        assert paths.bound_root() is None


class TestRoutingConfig:

    @staticmethod
    def _agent_runs(bobi_install, brain, *, gateway=""):
        """Install the agent's brain the way an operator does: in its own ``run/.env``."""
        from bobi import paths

        env_file = paths.env_path(bobi_install.repo_path)
        kept = [line for line in (env_file.read_text().splitlines() if env_file.exists() else [])
                if not line.startswith(("BOBI_BRAIN=", "BOBI_GATEWAY_BASE_URL="))]
        extra = [f"BOBI_BRAIN={brain}"] + ([f"BOBI_GATEWAY_BASE_URL={gateway}"] if gateway else [])
        env_file.write_text("\n".join(kept + extra) + "\n")

    def test_jev_config_without_experiment_is_disabled_not_missing(self, bobi_install):
        route = "metrics/jev-config"
        response = _client().get(f"/api/agents/{bobi_install.agent_name}/{route}")
        assert response.status_code == 200
        assert response.json()["enabled"] is False and response.json()["mode"] == "off"
        assert response.headers["cache-control"] == "no-store"
        assert _client().get(f"/api/agents/missing-agent/{route}").status_code == 404

    def test_jev_config_offers_native_models_and_names_unusable_policy(self, bobi_install):
        from bobi import paths
        from bobi.costs import model_prices
        from tests.metrics.test_routing import concise_config

        config = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config").json()
        # The picker lists the agent's own brain models, unprefixed; every one has a list price.
        assert config["agent_brain"] == "claude"
        assert {"opus", "claude-haiku-4-5", "claude-3-7-sonnet"} <= set(config["native_models"])
        assert not any("/" in model or model.startswith("gpt-") for model in config["native_models"])
        assert all(model_prices(model) for model in config["native_models"])
        assert not {"gateway", "priced_models", "unpriced_models"} & set(config)
        assert config["config_error"] == ""
        broken = {**concise_config(), "min_confidence": 7}
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_DISABLED_JSON=" + json.dumps(broken) + "\n")
        config = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config").json()
        # A saved policy that fails validation is named instead of silently shown as defaults.
        assert config["enabled"] is False and "min_confidence" in config["config_error"]
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_DISABLED_JSON={not json\n")
        config = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config").json()
        assert config["config_error"] == "BOBI_METRICS_EXPERIMENT_DISABLED_JSON is not valid JSON"

    def test_jev_config_does_not_hide_runtime_failure(self, bobi_install, monkeypatch):
        def unavailable(env):
            raise OSError("configuration unavailable")

        monkeypatch.setattr("bobi.metrics.router.load_experiment", unavailable)
        with pytest.raises(OSError, match="configuration unavailable"):
            _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config")

    def test_jev_config_bounds_security_and_hosted_refusal(self, bobi_install):
        from bobi.metrics.query import MetricsQueryError
        from bobi.webapp.runtime import LocalRuntime, TeamRuntime, UnknownTeam

        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        client = _client()
        for content in ("[]", "{", '{"instructions":"' + "x" * 131072 + '"}', '{"min_confidence":NaN}'):
            response = client.post(url, content=content, headers={"content-type": "application/json"})
            assert response.status_code == 400 and response.json()["code"] == "invalid_jev_config"
        for method in ("get", "post", "delete"):
            assert getattr(_testclient(), method)(url).status_code == 403
        hostile = TestClient(server.build_app(token=TOKEN), base_url="http://evil.example")
        assert hostile.delete(url, headers={"x-bobi-webui-token": TOKEN}).status_code == 403
        runtime = LocalRuntime()
        for call in (TeamRuntime.get_routing_config, TeamRuntime.update_routing_config):
            with pytest.raises(MetricsQueryError) as error:
                if call == TeamRuntime.get_routing_config:
                    call(runtime, bobi_install.agent_name)
                else:
                    call(runtime, bobi_install.agent_name, {"mode": "off"})
            assert error.value.code == "metrics_unsupported"
        with pytest.raises(UnknownTeam):
            runtime.get_routing_config("../outside")
        with pytest.raises(UnknownTeam):
            runtime.update_routing_config("../outside", {"mode": "off"})

    @pytest.mark.parametrize("stored", [{"policy": {"scope": "invalid"}}, {"api_key": "do-not-expose"}, []])
    def test_invalid_saved_config_is_safe_to_read(self, bobi_install, stored):
        from bobi import paths

        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(stored) + "\n")
        response = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config")
        assert response.status_code == 200
        assert response.json()["enabled"] is False and response.json()["mode"] == "off"
        assert response.json()["raw_json"] == "" and "do-not-expose" not in response.text

    def test_jev_config_roundtrip_reset_and_runtime_environment(self, bobi_install):
        import os
        from bobi import paths
        from bobi.config import parse_env_file, project_env
        from bobi.metrics.router import load_experiment
        from bobi.env import child_agent_env

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("# keep this comment\nOTHER_TOKEN=synthetic-private\n")
        before = dict(os.environ)
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        payload = {"mode": "shadow", "control_model": "haiku", "candidate_models": ["haiku", "sonnet"],
                   "roles": ["engineer"], "instructions": "Choose sonnet for complex work.\nOtherwise choose haiku."}
        client = _client()
        saved = client.post(url, json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["enabled"] is True
        assert saved.json()["instructions"] == payload["instructions"]
        assert saved.json()["restart_required"] is True and saved.json()["applies_on"] == "agent_restart"
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert env_file.read_text().startswith("# keep this comment\nOTHER_TOKEN=synthetic-private\n")
        assert "synthetic-private" not in saved.text
        assert dict(os.environ) == before
        config = load_experiment(project_env(bobi_install.repo_path))
        assert load_experiment(child_agent_env(bobi_install.repo_path, base={})) == config
        assert config.policy.mode == "shadow"
        # Routing needs no assignment secret; the editor never writes one.
        assert "BOBI_METRICS_ASSIGNMENT_SECRET" not in env_file.read_text()
        assert json.loads(config.policy.options_json)["instructions"] == payload["instructions"]
        assert client.get(url).json() == saved.json()
        deleted = client.delete(url)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["enabled"] is False and deleted.json()["mode"] == "off"
        assert deleted.json()["raw_json"] == ""
        assert parse_env_file(env_file)["BOBI_METRICS_EXPERIMENT_JSON"] == ""
        assert "BOBI_METRICS_EXPERIMENT_DISABLED_JSON=" not in env_file.read_text()
        assert load_experiment(project_env(bobi_install.repo_path)) is None
        assert dict(os.environ) == before
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()
        assert client.post(url, json={"mode": "off"}).json()["mode"] == "off"

    @pytest.mark.parametrize("invalid,field", [
        ({"mode": "invalid"}, "mode"), ({"mode": True}, "mode"), ({"enabled": "false"}, "enabled"),
        ({"enabled": True, "mode": "off"}, "mode"), ({"instructions": ""}, "instructions"),
        ({"instructions": 5}, "instructions"), ({"candidate_models": []}, "candidate_models"),
        ({"candidate_models": "model-a"}, "candidate_models"),
        ({"candidate_models": ["model-a", "model-a"]}, "candidate_models"),
        ({"candidate_models": [None]}, "candidate_models"), ({"control_model": "not-a-candidate"}, "control_model"),
        ({"roles": []}, "roles"), ({"roles": ["unknown-role"]}, "roles"), ({"roles": ["engineer", "engineer"]}, "roles"),
        ({"roles": "engineer"}, "roles"), ({"min_confidence": True}, "min_confidence"),
        ({"min_confidence": 1.5}, "min_confidence"), ({"api_key": "never-echo-this"}, "api_key"),
        ({"candidate_models": ["haiku", "opus", "claude-opus-5-5"]}, "candidate_models"),
    ])
    def test_jev_config_rejects_invalid_input_without_mutation(self, bobi_install, invalid, field):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        env_file = paths.env_path(bobi_install.repo_path)
        before = "BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n"
        env_file.write_text(before)
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config", json=invalid)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_jev_config"
        assert "never-echo-this" not in response.text
        assert env_file.read_text() == before
        # The message names the offending field so the editor can say what to fix.
        assert field in response.json()["error"]

    @pytest.mark.parametrize("gateway", [False, True])
    @pytest.mark.parametrize("brain,candidates,problem", [
        ("claude", ["haiku", "claude-opus-5-5"], None),
        ("claude", ["haiku", "gpt-5.4"], "gpt-5.4 is not an Anthropic model this agent's claude brain can run"),
        ("claude", ["haiku", "cx/gpt-5"], "cx/gpt-5 is not an Anthropic model this agent's claude brain can run"),
        ("codex", ["gpt-5.6-luna", "gpt-5.6-sol"], None),
        ("codex", ["gpt-5.6-luna", "opus"], "opus is not an OpenAI model this agent's codex brain can run"),
    ])
    def test_jev_candidates_must_run_on_the_agent_brain(self, bobi_install, monkeypatch, brain, candidates, problem,
                                                         gateway):
        from bobi import paths
        from bobi.config import parse_env_file

        # A gateway does not widen the roster: JEV routes only between the brain's own models.
        self._agent_runs(bobi_install, brain, gateway="https://gateway.invalid/v1" if gateway else "")
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        config = _client().get(url).json()
        assert config["agent_brain"] == brain and "gateway" not in config
        # A new policy sends no task text until the operator opts in.
        assert config["brain"] == "auto" and config["prompt_egress"] == "none"
        response = _client().post(url, json={"mode": "shadow", "control_model": candidates[0],
            "candidate_models": candidates, "roles": ["engineer"], "instructions": "Route by difficulty."})
        if problem:
            assert response.status_code == 400 and problem in response.json()["error"]
            assert "pick one from the model list" in response.json()["error"]
            return
        assert response.status_code == 200, response.text
        saved = json.loads(parse_env_file(paths.env_path(bobi_install.repo_path))["BOBI_METRICS_EXPERIMENT_JSON"])
        # The policy follows whichever brain the agent runs (the default, so not
        # written) and sends metadata only.
        assert "brain" not in saved and saved["prompt_egress"] == "none"
        assert response.json()["config_warnings"] == []

    def test_jev_pinned_brain_must_match_the_agent(self, bobi_install, monkeypatch):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        rejected = _client().post(url, json={**{k: concise_config()[k] for k in ("control_model", "candidate_models", "roles", "instructions")},
                                             "mode": "enforce", "brain": "codex", "prompt_egress": "none"})
        assert rejected.status_code == 400 and "does not match this agent's claude brain" in rejected.json()["error"]
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(
            {**concise_config(), "brain": "codex"}) + "\n")
        # A saved policy that can never route this agent is flagged on read.
        assert "does not match" in _client().get(url).json()["config_warnings"][0]

    def test_jev_follows_the_agent_brain_not_the_web_app_process(self, bobi_install, monkeypatch):
        import yaml
        from bobi import paths

        # Starting a codex team from the UI pins the brain in this process.
        monkeypatch.setenv("BOBI_BRAIN", "codex")
        monkeypatch.setenv("BOBI_GATEWAY_BASE_URL", "https://gateway.invalid/v1")
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        config = _client().get(url).json()
        assert config["agent_brain"] == "claude"
        assert not any(model.startswith("gpt-") for model in config["native_models"])
        policy = {"mode": "shadow", "roles": ["engineer"], "instructions": "Route by difficulty."}
        assert _client().post(url, json={**policy, "control_model": "haiku",
                                         "candidate_models": ["haiku", "sonnet"]}).status_code == 200
        rejected = _client().post(url, json={**policy, "control_model": "gpt-5.6-luna",
                                             "candidate_models": ["gpt-5.6-luna", "gpt-5.6-sol"]})
        assert rejected.status_code == 400 and "claude brain" in rejected.json()["error"]
        # The sandbox's native-brain gate reads the same source.
        response, _ = self._sandbox_run(bobi_install, monkeypatch, brain="claude")
        assert response.status_code == 200, response.text
        # agent.yaml's brain.kind wins over run/.env, as when the agent starts.
        agent_yaml = paths.agent_yaml_path(bobi_install.repo_path)
        agent_yaml.write_text(yaml.safe_dump({**yaml.safe_load(agent_yaml.read_text()), "brain": {"kind": "codex"}}))
        self._agent_runs(bobi_install, "claude")
        assert _client().get(url).json()["agent_brain"] == "codex"

    def test_declared_brain_ignores_a_gateway_left_in_run_env(self, bobi_install):
        import yaml
        from bobi import paths
        from bobi.webapp.runtime import _agent_brain

        agent_yaml = paths.agent_yaml_path(bobi_install.repo_path)
        agent_yaml.write_text(yaml.safe_dump({**yaml.safe_load(agent_yaml.read_text()), "brain": {"kind": "claude"}}))
        self._agent_runs(bobi_install, "claude", gateway="https://gateway.invalid/v1")
        # The agent starts on the native endpoint agent.yaml declares, not the stale gateway.
        assert _agent_brain(bobi_install.repo_path) == ("claude", False)
        agent_yaml.write_text(yaml.safe_dump({**yaml.safe_load(agent_yaml.read_text()),
                                              "brain": {"kind": "claude", "base_url": "https://gw.invalid/v1"}}))
        assert _agent_brain(bobi_install.repo_path) == ("claude", True)

    def test_jev_config_reports_whether_the_policy_key_is_set(self, bobi_install, monkeypatch):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        env_file = paths.env_path(bobi_install.repo_path)
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        env_file.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        assert _client().get(url).json()["credential_configured"] is False
        env_file.write_text(env_file.read_text() + "TYPESAFE_API_KEY=synthetic-sandbox-key\n")
        response = _client().get(url)
        # The flag says the key exists; the response never carries it.
        assert response.json()["credential_configured"] is True
        assert "synthetic-sandbox-key" not in response.text

    def _sandbox_run(self, bobi_install, monkeypatch, *, brain="claude", final=None, payload=None):
        from bobi import paths
        from bobi.brain import BrainUsage, StreamDelta, TurnResult

        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps({
            "control_model": "haiku", "candidate_models": ["haiku", "sonnet"],
            "instructions": "Route by difficulty."}) + "\n")
        self._agent_runs(bobi_install, brain)
        calls = []

        class FakeBrain:
            async def stream_once(self, **kwargs):
                calls.append(kwargs)
                yield StreamDelta(text="Use a lock ")
                yield StreamDelta(text="around resume.")
                yield final or TurnResult(usage=[BrainUsage(model="sonnet", input_tokens=1200, output_tokens=80)])

        monkeypatch.setattr("bobi.brain.get_brain", lambda kind=None: FakeBrain())
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-run",
                                  json=payload or {"prompt": "Find the deadlock", "model": "sonnet"})
        return response, calls

    def test_sandbox_runs_the_prompt_on_the_chosen_model_without_tools_or_metrics(self, bobi_install, monkeypatch):
        from pathlib import Path

        response, calls = self._sandbox_run(bobi_install, monkeypatch)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["model"] == "sonnet" and data["output"] == "Use a lock around resume."
        assert (data["input_tokens"], data["output_tokens"]) == (1200, 80) and data["list_price_usd"] > 0
        [call] = calls
        assert call["model"] == "sonnet" and call["user_prompt"] == "Find the deadlock"
        # Nothing is allowed: no built-in tools, no MCP servers, no settings, no bypass.
        assert call["options"]["max_turns"] == 1 and call["options"]["tools"] == []
        assert call["options"]["mcp_servers"] == {} and call["options"]["strict_mcp_config"] is True
        assert call["options"]["setting_sources"] == [] and call["options"]["permission_mode"] == "default"
        # The agent's own environment and login, from an empty directory outside the repo.
        assert call["options"]["env"]["BOBI_ROOT"] == str(bobi_install.repo_path.resolve())
        assert not Path(call["cwd"]).is_relative_to(bobi_install.repo_path)
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    @pytest.mark.parametrize("case,status,code", [
        ("not_a_candidate", 400, "invalid_simulation"),
        ("codex_brain", 400, "model_run_unsupported"),
        ("model_error", 502, "model_run_failed"),
    ])
    def test_sandbox_run_refuses_or_reports_failures(self, bobi_install, monkeypatch, case, status, code):
        from bobi.brain import TurnResult

        response, calls = self._sandbox_run(
            bobi_install, monkeypatch, brain="codex" if case == "codex_brain" else "claude",
            final=TurnResult(is_error=True, error_kind="api_error", error_message="overloaded")
            if case == "model_error" else None,
            payload={"prompt": "Find the deadlock", "model": "opus"} if case == "not_a_candidate" else None)
        assert response.status_code == status and response.json()["code"] == code
        assert len(calls) == (1 if case == "model_error" else 0)

    def test_jev_mode_off_keeps_the_policy_for_re_enabling(self, bobi_install):
        from bobi import paths
        from bobi.config import parse_env_file
        from tests.metrics.test_routing import concise_config

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        off = _client().post(url, json={"mode": "off"}).json()
        assert off["enabled"] is False and off["configured_mode"] == "enforce"
        assert off["instructions"] == concise_config()["instructions"]
        assert parse_env_file(env_file)["BOBI_METRICS_EXPERIMENT_JSON"] == ""
        restored = _client().post(url, json={"mode": "enforce"}).json()
        assert restored["enabled"] is True and restored["instructions"] == concise_config()["instructions"]

    def test_disabled_policy_survives_a_setup_env_rewrite(self, bobi_install):
        from bobi import paths
        from bobi.setup.actions import read_env, write_env
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text(
            "BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        assert _client().post(url, json={"mode": "off"}).status_code == 200
        # Setup saving a credential rewrites the whole .env through the one serializer.
        write_env(bobi_install.repo_path, {**read_env(bobi_install.repo_path), "SLACK_BOT_TOKEN": "xoxb-test"})
        config = _client().get(url).json()
        assert config["enabled"] is False and config["configured_mode"] == "enforce"
        assert config["candidate_models"] == concise_config()["candidate_models"]
        restored = _client().post(url, json={"mode": "enforce"}).json()
        assert restored["enabled"] is True and restored["instructions"] == concise_config()["instructions"]
        assert "BOBI_METRICS_EXPERIMENT_DISABLED_JSON" not in read_env(bobi_install.repo_path)

    def test_jev_edit_preserves_advanced_policy_and_updates_criteria(self, bobi_install, monkeypatch):
        from bobi import paths
        from bobi.config import parse_env_file
        from bobi.metrics.router import ExperimentConfig
        from tests.metrics.test_routing import concise_config

        raw = {**concise_config(), "brain": "codex", "deadline_ms": 1750,
               "control_model": "gpt-5.6-luna", "candidate_models": ["gpt-5.6-luna", "gpt-5.6-sol"],
               "endpoint": "https://example.invalid/systemone", "prompt_egress": "redacted",
               "max_prompt_bytes": 4096, "entry_points": ["subagent_phase"],
               "criteria": {"gpt-5.6-luna": "Retain this criterion", "gpt-5.6-sol": "Removed criterion"}}
        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(raw) + "\n")
        self._agent_runs(bobi_install, "codex")
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config", json={
            "candidate_models": ["gpt-5.6-luna", "gpt-5.4"], "instructions": "Edited instructions"})
        assert response.status_code == 200, response.text
        saved = json.loads(parse_env_file(env_file)["BOBI_METRICS_EXPERIMENT_JSON"])
        policy = ExperimentConfig.from_mapping(saved).policy
        assert policy.brain == "codex" and policy.deadline_ms == 1750
        assert policy.prompt_egress == "redacted" and policy.max_prompt_bytes == 4096
        assert policy.entry_points == ("subagent_phase",)
        options = json.loads(policy.options_json)
        assert options["endpoint"] == "https://example.invalid/systemone"
        assert options["instructions"] == "Edited instructions"
        assert options["criteria"]["gpt-5.6-luna"] == "Retain this criterion"
        assert set(options["criteria"]) == {"gpt-5.6-luna", "gpt-5.4"}

    def test_long_form_config_is_reported_and_rewritten_by_the_next_save(self, bobi_install):
        from bobi import paths
        from bobi.config import parse_env_file
        from bobi.metrics.router import LONG_FORM_ERROR
        from tests.metrics.test_routing import concise_config

        long_form = {"experiment_id": "old", "control_model": "haiku",
                     "variants": [{"variant_id": "control", "weight": 0.0, "model": "haiku"},
                                  {"variant_id": "treatment_jev", "weight": 1.0, "policy": "typesafe-jev"}],
                     "policy": {"name": "typesafe-jev", "mode": "shadow", "candidate_models": ["haiku", "sonnet"]}}
        env_file = paths.env_path(bobi_install.repo_path)
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        for line in ("BOBI_METRICS_EXPERIMENT_JSON=", "BOBI_METRICS_EXPERIMENT_DISABLED_JSON="):
            env_file.write_text(line + json.dumps(long_form) + "\nOTHER_TOKEN=synthetic-private\n")
            response = _client().get(url)
            assert response.status_code == 200, response.text
            config = response.json()
            assert config["enabled"] is False and config["mode"] == "off" and config["raw_json"] == ""
            assert config["config_error"] == LONG_FORM_ERROR
        # The editor sends the full policy; saving replaces the old line with the short form.
        payload = {key: concise_config()[key] for key in ("mode", "control_model", "candidate_models", "roles", "instructions")}
        saved = _client().post(url, json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["enabled"] is True and "config_error" not in saved.json()
        written = env_file.read_text()
        assert "variants" not in written and written.count("BOBI_METRICS_EXPERIMENT_JSON=") == 1
        assert parse_env_file(env_file)["OTHER_TOKEN"] == "synthetic-private"
        assert json.loads(parse_env_file(env_file)["BOBI_METRICS_EXPERIMENT_JSON"])["candidate_models"] == ["haiku", "sonnet"]

    def test_jev_config_preserves_file_on_read_and_write_failure(self, bobi_install, monkeypatch):
        from pathlib import Path
        from bobi import paths

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("OTHER_TOKEN=synthetic-private\n")
        read_text = Path.read_text
        def fail_read(target, *args, **kwargs):
            if target == env_file:
                raise OSError("private-error-do-not-echo")
            return read_text(target, *args, **kwargs)
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        with monkeypatch.context() as context:
            context.setattr(Path, "read_text", fail_read)
            response = _client().post(url, json={"mode": "off"})
        assert response.status_code == 500 and response.json()["code"] == "config_write_failed"
        assert "private-error" not in response.text
        assert env_file.read_text() == "OTHER_TOKEN=synthetic-private\n"
        def fail_write(*args, **kwargs):
            raise OSError("private-error-do-not-echo")
        monkeypatch.setattr("bobi.fsutil.atomic_write_text", fail_write)
        response = _client().delete(url)
        assert response.status_code == 500 and response.json()["code"] == "config_write_failed"
        assert env_file.read_text() == "OTHER_TOKEN=synthetic-private\n"

    def test_jev_config_process_override_rejects_false_success(self, bobi_install, monkeypatch):
        from bobi import paths

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("OTHER_TOKEN=synthetic-private\n")
        monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "{}")
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        response = _client().post(url, json={"mode": "off"})
        assert response.status_code == 409 and response.json()["code"] == "config_environment_override"
        assert env_file.read_text() == "OTHER_TOKEN=synthetic-private\n"

    def test_jev_concurrent_edits_keep_both_changes(self, bobi_install):
        from concurrent.futures import ThreadPoolExecutor
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        runtime = LocalRuntime()
        runtime.update_routing_config(bobi_install.agent_name, concise_config())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(runtime.update_routing_config, bobi_install.agent_name, payload) for payload in (
                {"instructions": "Concurrent instructions"}, {"roles": ["engineer"]})]
            for future in futures:
                assert future.result()["enabled"] is True
        saved = runtime.get_routing_config(bobi_install.agent_name)
        assert saved["instructions"] == "Concurrent instructions" and saved["roles"] == ["engineer"]

    def test_jev_edit_does_not_leak_into_another_agent(self, bobi_install, monkeypatch):
        import os
        import shutil
        from bobi import paths
        from bobi.config import _DOTENV_LOADED, project_env
        from bobi.env import child_agent_env
        from bobi.metrics.router import load_experiment
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        other_root = bobi_install.agents_dir / "second-agent" / "run"
        shutil.copytree(bobi_install.repo_path / "package", other_root / "package")
        raw = json.dumps(concise_config())
        paths.env_path(other_root).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + raw + "\n")
        monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", raw)
        monkeypatch.setitem(_DOTENV_LOADED, "BOBI_METRICS_EXPERIMENT_JSON", raw)
        process_before = dict(os.environ)
        other_before = paths.env_path(other_root).read_text()
        runtime = LocalRuntime()
        runtime.update_routing_config(bobi_install.agent_name, {**concise_config(), "instructions": "Changed for first agent"})
        assert runtime.get_routing_config("second-agent")["instructions"] == concise_config()["instructions"]
        assert paths.env_path(other_root).read_text() == other_before and dict(os.environ) == process_before
        config = load_experiment(child_agent_env(bobi_install.repo_path))
        assert json.loads(config.policy.options_json)["instructions"] == "Changed for first agent"
        other = load_experiment(project_env(other_root))
        assert json.loads(other.policy.options_json)["instructions"] == concise_config()["instructions"]

    @pytest.mark.parametrize("trigger,prompt,title", [
        ("startup", "You are a bobi director.\n# Bobi Agent\nHow you receive events", "Agent Startup & Initialization"),
        ("inbox", "You are the **sleep cycle** for this agent team. Internal transcript delta", "Sleep Cycle & Memory Compaction"),
        ("inbox", "Compaction required: reduce the session context.", "Sleep Cycle & Memory Compaction"),
        ("inbox", "No new events. Wait for delivery.", "Idle Standby (No new events)"),
        ("inbox", "Process pending inbox messages.", "Inbox Event Processing"),
        ("direct", "Fix sleep cycle regression", "Fix sleep cycle regression"),
        ("chat", "Process inbox", "Process inbox"),
        ("user", "Explain the # Bobi Agent header", "Explain the # Bobi Agent header"),
    ])
    def test_semantic_titles_do_not_show_internal_triggers(self, trigger, prompt, title):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": trigger}
        entries = [{"kind": "message", "role": "user", "text": prompt, "at": "1970-01-01T00:00:01.1Z"}]
        data = _turn_conversation(turn, entries)
        assert data["semantic_title"] == title
        if title != prompt:
            assert data["user_message"] is None and data["prompt_snippet"] is None
        else:
            assert data["user_message"] == prompt

    def test_semantic_title_user_first_and_curator_session_fallback(self):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "inbox"}
        entries = [{"kind": "message", "role": "user", "text": text, "at": "1970-01-01T00:00:01.1Z"} for text in (
            "You are the **sleep cycle** for this agent team. Internal machinery",
            "Event: slack/message\nFix sleep cycle regression\nconversation: slack:T123:channel:C123:thread:1791360056.16")]
        session = {"role": "curator", "session_name": "curator-1"}
        data = _turn_conversation(turn, entries, session=session)
        assert data["semantic_title"] == "Fix sleep cycle regression"
        assert data["user_message"] == "Fix sleep cycle regression"
        assert data["origin"]["source"] == "slack"
        assert _turn_conversation(turn, [], session=session)["semantic_title"] == "Sleep Cycle & Memory Compaction"
        wrapped = [{"kind": "message", "role": "user", "at": "1970-01-01T00:00:01.1Z",
                    "text": "You are an agent in a bobi deployment. You are the **sleep cycle** for this agent team."}]
        assert _turn_conversation(turn, wrapped, session=session)["semantic_title"] == "Sleep Cycle & Memory Compaction"
        startup = {**turn, "trigger_kind": "startup"}
        idle = [{"kind": "message", "role": "agent", "text": "No new events.", "at": "1970-01-01T00:00:01.1Z"}]
        assert _turn_conversation(startup, idle)["semantic_title"] == "Idle Standby (No new events)"
        assert _turn_conversation(turn, idle, session=session)["semantic_title"] == "Idle Standby (No new events)"
        inbox = [{"kind": "message", "role": "user", "text": "Process pending inbox messages.", "at": "1970-01-01T00:00:01.1Z"}]
        assert _turn_conversation(turn, inbox, session=session)["semantic_title"] == "Inbox Event Processing"

    def test_runtime_preserves_tool_preview_bytes_and_marks_additional_clipping(self):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "inbox"}
        entries = [{"kind": "tool_result", "role": "user", "text": "é" * 4000,
                    "total_bytes": 12000, "truncated": True, "at": "1970-01-01T00:00:01.1Z"},
                   {"kind": "message", "role": "agent", "text": "é" * 5000,
                    "truncated": False, "at": "1970-01-01T00:00:01.2Z"}]
        data = _turn_conversation(turn, entries)
        assert data["entries"][0]["total_bytes"] == 12000 and data["entries"][0]["truncated"] is True
        assert len(data["entries"][0]["text"]) == 4000
        assert data["entries"][1]["total_bytes"] == 10000 and data["entries"][1]["truncated"] is False
        assert data["entries"][1]["text"] == entries[1]["text"]
        assert data["truncated"] is True

    def test_runtime_tool_preview_has_no_secondary_cap_and_preserves_metadata(self):
        from bobi.webapp.runtime import _turn_conversation

        text = "hello " * 1000
        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "user"}
        entry = {"kind": "tool_result", "role": "user", "text": text, "at": "1970-01-01T00:00:01.1Z"}
        data = _turn_conversation(turn, [entry])
        assert data["entries"][0]["text"] == "hello " * 666
        assert data["entries"][0]["total_bytes"] == len(text.encode("utf-8"))
        assert data["entries"][0]["truncated"] is True
        assert entry["text"] == text

    def test_runtime_preserves_complete_messages_until_aggregate_budget(self):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "user"}
        entry = {"kind": "message", "role": "user", "text": "é" * 10000,
                 "truncated": False, "at": "1970-01-01T00:00:01.1Z"}
        data = _turn_conversation(turn, [entry])
        assert data["entries"][0]["text"] == data["input"] == data["user_message"] == entry["text"]
        assert data["truncated"] is False
        oversized = {**entry, "text": "界" * 100000}
        bounded = _turn_conversation(turn, [oversized])
        assert len(bounded["input"].encode("utf-8")) <= 32768
        assert len(bounded["user_message"].encode("utf-8")) <= 32768
        assert len(bounded["entries"][0]["text"].encode("utf-8")) <= 32768
        assert bounded["entries"][0]["total_bytes"] == 300000
        assert bounded["entries"][0]["truncated"] is True and bounded["truncated"] is True

    def test_sandbox_classifies_without_writing_state_or_changing_credentials(self, bobi_install, monkeypatch):
        import httpx
        import os
        from bobi import paths
        from bobi.metrics.store import connect
        from tests.metrics.test_routing import concise_config

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        env_file = paths.env_path(bobi_install.repo_path)
        minimal = {field: concise_config()[field] for field in ("control_model", "candidate_models", "instructions")}
        minimal["prompt_egress"] = "redacted"
        env_file.write_text("BOBI_METRICS_EXPERIMENT_DISABLED_JSON=" + json.dumps(minimal) +
                            "\nTYPESAFE_API_KEY=configured-test-key\n")
        database = bobi_install.state_dir / "metrics/metrics.db"
        connection = connect(database)
        before = "\n".join(connection.iterdump())
        connection.close()
        files_before = {path: path.read_bytes() for path in bobi_install.state_dir.rglob("*") if path.is_file()}
        env_before = dict(os.environ)
        wire_calls = []
        client_class = httpx.AsyncClient

        def handler(request):
            wire_calls.append(request)
            payload = json.loads(request.content)
            assert payload["state"]["task"] == "Write Raft. API_KEY=[redacted] [redacted]"
            assert payload["state"]["role"] == "director"
            assert payload["state"]["entry_point"] == "subagent_phase"
            assert request.headers["authorization"] == "Bearer override-test-key"
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "sonnet", "confidence": 0.94,
                "probabilities": {"haiku": 0.03, "sonnet": 0.97}}},
                "usage": {"input_tokens": 12, "output_tokens": 4}, "debug": "override-test-key",
                "saved": "configured-test-key", "access_token": "opaque-vendor-token"})

        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        payload = {"prompt": "Write Raft. API_KEY=private configured-test-key", "api_key": "override-test-key",
                   "features": {"role": "director", "entry_point": "subagent_phase", "brain": "claude"}}
        url = f"/api/agents/{bobi_install.agent_name}/metrics/test-route"
        result = _client().post(url, json=payload)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["selected_model"] == "sonnet" and data["confidence"] == 0.94
        assert data["probabilities"]["sonnet"] == 0.97 and data["latency_ms"] >= 0
        assert data["outgoing_payload"]["state"] == data["features"]
        assert data["raw_response"]["debug"] == "[redacted]"
        assert data["raw_response"]["saved"] == "[redacted]"
        assert data["raw_response"]["access_token"] == "[redacted]"
        assert data["raw_response"]["usage"] == {"input_tokens": 12, "output_tokens": 4}
        assert data["features"]["prompt_bytes"] == len(payload["prompt"].encode())
        assert result.headers["cache-control"] == "no-store"
        assert "override-test-key" not in result.text and "private" not in result.text
        assert len(wire_calls) == 1 and dict(os.environ) == env_before
        connection = connect(database)
        assert "\n".join(connection.iterdump()) == before
        connection.close()
        assert {path: path.read_bytes() for path in files_before} == files_before
        assert env_file.read_text().startswith("BOBI_METRICS_EXPERIMENT_DISABLED_JSON=")

    @pytest.mark.parametrize("body", [b'{"api_key":"private-canary",', b'[]',
        b'{"prompt":"test","api_key":"private-canary","extra":"' + b'x' * 131072 + b'"}',
        b'[' * 2000 + b'0' + b']' * 2000])
    @pytest.mark.parametrize("endpoint", ["test-route", "test-run"])
    def test_sandbox_rejects_bad_bodies_without_echoing_secrets(self, bobi_install, monkeypatch, body, endpoint):
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
                            lambda **kwargs: pytest.fail("invalid body reached network"))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/{endpoint}",
                                  content=body, headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_simulation"
        assert "private-canary" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    def test_sandbox_concurrent_keys_remain_request_local(self, bobi_install, monkeypatch):
        import asyncio
        import os
        from bobi import paths
        from bobi.metrics.policy import PolicyResult
        from bobi.metrics.query import MetricsQueryError
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text(
            "BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        before = dict(os.environ)
        calls = []
        async def decide(policy, request, *, timeout_s):
            key = policy.api_key
            calls.append(key)
            await asyncio.sleep(0.01)
            assert policy.api_key == key
            return PolicyResult("haiku", 0.94, None, "jev-1.13.0", None, None,
                {"haiku": 0.97, "sonnet": 0.03}, {"debug": key}, {})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.TypeSafePolicy.decide", decide)
        runtime = LocalRuntime()
        async def exercise():
            return await asyncio.gather(*(runtime.test_route(bobi_install.agent_name,
                {"prompt": "test", "api_key": f"request-{index}-key"}) for index in range(6)),
                return_exceptions=True)
        results = asyncio.run(exercise())
        assert calls == [f"request-{index}-key" for index in range(4)]
        assert all(result["raw_response"]["debug"] == "[redacted]" for result in results[:4])
        assert all(isinstance(result, MetricsQueryError) and result.code == "metrics_busy" for result in results[4:])
        assert dict(os.environ) == before
        for _ in range(4):
            assert runtime._routing_tests.acquire(blocking=False)
        for _ in range(4):
            runtime._routing_tests.release()
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    @pytest.mark.parametrize("payload", [
        {}, {"prompt": " "}, {"prompt": "é" * 32769}, {"prompt": "test", "api_key": ["bad"]},
        {"prompt": "test", "api_key": False}, {"prompt": "test", "api_key": None},
        {"prompt": "test", "endpoint": False}, {"prompt": "test", "endpoint": None},
        {"prompt": "test", "features": {"task": "injected"}}, {"prompt": "test", "features": None},
        {"prompt": "test", "features": {"role": "unknown"}},
        {"prompt": "test", "features": {"entry_point": "unknown"}},
        {"prompt": "test", "endpoint": "https://127.0.0.1/private"},
        {"prompt": "test", "endpoint": "https://evil.example"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai:444/v1/systemone"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai:bad/v1/systemone"},
        {"prompt": "test", "endpoint": "http://api.typesafe.ai/v1/systemone"},
        {"prompt": "test", "endpoint": "https://user:pass@api.typesafe.ai/v1/systemone"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai:0/v1/systemone"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai/v1/systemone\n"},
        {"prompt": "test", "api_key": "bad\nkey"}, {"prompt": "test", "api_key": "x" * 4097},
    ])
    def test_sandbox_rejects_invalid_inputs_without_network_or_database(self, bobi_install, monkeypatch, payload):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text(
            "BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
                            lambda **kwargs: pytest.fail("invalid sandbox input reached network"))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route", json=payload)
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_simulation"
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    def test_sandbox_security_and_missing_configuration(self, bobi_install):
        url = f"/api/agents/{bobi_install.agent_name}/metrics/test-route"
        assert _testclient().post(url, json={"prompt": "test"}).status_code == 403
        hostile = TestClient(server.build_app(token=TOKEN), base_url="http://evil.example")
        hostile.headers.update({"x-bobi-webui-token": TOKEN})
        assert hostile.post(url, json={"prompt": "test"}).status_code == 403
        assert _client().post("/api/agents/absent/metrics/test-route", json={"prompt": "test"}).status_code == 404
        assert _client().post(url, json={"prompt": "test"}).status_code == 400

    @pytest.mark.parametrize("fault", ["unexpected", "nested", "expanded", "trusted_override"])
    def test_sandbox_bounds_and_sanitizes_vendor_results(self, bobi_install, monkeypatch, fault):
        import httpx
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        config = {**concise_config(), "max_prompt_bytes": 65536, "prompt_egress": "redacted",
                  "endpoint": "https://router.example:8443/v1/systemone"}
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(config) +
                                                       "\nTYPESAFE_API_KEY=private-canary\n")
        client_class = httpx.AsyncClient
        def handler(request):
            assert str(request.url) == "https://router.example:8443/alternate"
            if fault == "unexpected":
                raise OSError("private-canary sensitive-vendor-error")
            raw = {"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "haiku", "confidence": 0.94,
                "probabilities": {"haiku": 0.97, "sonnet": 0.03}}},
                "usage": {"input_tokens": 12, "output_tokens": 4}}
            if fault == "nested":
                nested = {}
                for _ in range(40):
                    nested = {"child": nested}
                raw["extra"] = nested
            if fault == "expanded":
                raw["extra"] = "é" * 300000
            return httpx.Response(200, content=json.dumps(raw, ensure_ascii=False).encode())
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route",
            json={"prompt": "é" * 30000 if fault == "expanded" else "test",
                  "endpoint": "https://router.example:8443/alternate"})
        assert response.status_code == (200 if fault == "trusted_override" else 502)
        assert "private-canary" not in response.text and "sensitive-vendor-error" not in response.text
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    def test_sandbox_deadline_and_saturation_release_without_affecting_production(self, bobi_install, monkeypatch):
        import asyncio
        from bobi import paths
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" +
            json.dumps({**concise_config(), "deadline_ms": 50}) + "\nTYPESAFE_API_KEY=test-only-key\n")
        async def slow_policy(self, request, *, timeout_s):
            await asyncio.sleep(1)
        monkeypatch.setattr("bobi.metrics.policies.typesafe.TypeSafePolicy.decide", slow_policy)
        runtime = LocalRuntime()
        client = TestClient(server.build_app(token=TOKEN, runtime=runtime), base_url="http://127.0.0.1")
        client.headers.update({"x-bobi-webui-token": TOKEN})
        url = f"/api/agents/{bobi_install.agent_name}/metrics/test-route"
        for _ in range(4):
            assert runtime._routing_tests.acquire(blocking=False)
        response = client.post(url, json={"prompt": "test"})
        assert response.status_code == 503 and response.json()["code"] == "metrics_busy"
        for _ in range(4):
            runtime._routing_tests.release()
        for _ in range(2):
            response = client.post(url, json={"prompt": "test"})
            assert response.status_code == 504 and response.json()["code"] == "policy_timeout"
        for _ in range(4):
            assert runtime._routing_tests.acquire(blocking=False)
        for _ in range(4):
            runtime._routing_tests.release()
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    @pytest.mark.parametrize("mode,confidence,status,version", [
        ("shadow", 0.94, 200, "jev-1.13.0"), ("enforce", 0.5, 200, "jev-1.13.0"),
        ("enforce", 0.94, 401, "jev-1.13.0"), ("enforce", 0.94, 200, "jev-other"),
        ("enforce", 0.94, 200, "jev-1.13.4"), ("enforce", 0.94, 200, "jev-1.14.0"),
    ])
    def test_sandbox_uses_saved_key_and_previews_guarded_decisions(self, bobi_install, monkeypatch, mode, confidence, status, version):
        import httpx
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        config = {**concise_config(), "mode": mode}
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(config) +
                                                       "\nTYPESAFE_API_KEY=saved-test-key\n")
        client_class = httpx.AsyncClient
        def handler(request):
            assert request.headers["authorization"] == "Bearer saved-test-key"
            return httpx.Response(status, json={"model": version, "answers": {"route": {
                "type": "choice", "choice": "sonnet", "confidence": confidence,
                "probabilities": {"haiku": 0.03, "sonnet": 0.97}}},
                "usage": {"input_tokens": 12, "output_tokens": 4}})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route", json={"prompt": "test"})
        if status != 200:
            assert response.status_code == 502
            assert response.json()["code"] == "policy_unauthenticated"
            assert "saved-test-key" not in response.text
            return
        assert response.status_code == 200, response.text
        data = response.json()
        # A patch bump is the pinned model; another release line previews as a fallback, not an error.
        drift = version in {"jev-other", "jev-1.14.0"}
        assert data["model_version"] == version and data["pinned_version"] == "jev-1.13.0"
        assert data["fallback_reason"] == ("policy_version_drift" if drift else "policy_low_confidence" if confidence < 0.85 else None)
        assert data["effective_model"] == ("haiku" if drift or confidence < 0.85 or mode == "shadow" else "sonnet")

    @pytest.mark.parametrize("egress", ["none", "redacted"])
    def test_sandbox_sends_task_text_only_when_production_would(self, bobi_install, monkeypatch, egress):
        import httpx
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(
            {**concise_config(), "prompt_egress": egress}) + "\nTYPESAFE_API_KEY=saved-test-key\n"
            "BILLING_DB_PASS=billing-local-pass\n")
        client_class = httpx.AsyncClient
        sent = []
        def handler(request):
            sent.append(json.loads(request.content)["state"])
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "haiku", "confidence": 0.9,
                "probabilities": {"haiku": 0.9, "sonnet": 0.1}}},
                "usage": {"input_tokens": 1, "output_tokens": 1}})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route",
                                  json={"prompt": "Refactor the billing module using billing-local-pass"})
        assert response.status_code == 200, response.text
        assert response.json()["prompt_egress"] == egress
        assert ("task" in sent[0]) is (egress == "redacted")
        assert sent[0]["prompt_bytes"] == len("Refactor the billing module using billing-local-pass")
        # The agent's own credential values never leave, whatever their shape.
        assert sent[0].get("task", "") in ("", "Refactor the billing module using [redacted]")


def test_turn_cost_models_follow_the_billed_models_by_identity():
    from bobi.webapp.runtime import _turn_cost_models

    one_call = [{"model_selected": "claude-haiku-4-5"}]
    # A background call on another model is billed but never logged as a call.
    assert _turn_cost_models({"invocations": one_call, "best_usage": [
        {"scope": "turn", "model": "claude-haiku-4-5"}, {"scope": "turn", "model": "claude-opus-4-7"},
        {"scope": "invocation", "model": "claude-sonnet-5-5"}]}) == ["claude-haiku-4-5", "claude-opus-4-7"]
    assert _turn_cost_models({"invocations": [*one_call, {"model_selected": None, "model_requested": "haiku"}],
                              "best_usage": []}) == ["claude-haiku-4-5"]
    assert _turn_cost_models({"invocations": [], "best_usage": []}) == []


def test_transcript_cache_is_bounded_by_the_memory_its_entries_use():
    from bobi.webapp.runtime import _TranscriptCache

    # Two bytes a character once the text is not Latin-1: two of these do not fit in 4 KB.
    entries = [{"kind": "message", "role": "user", "text": "ệ" * 1000, "at": ""}]
    cache = _TranscriptCache(4000)
    cache.put(("a",), entries, None)
    cache.put(("b",), entries, None)
    assert cache.get(("a",)) is None
    assert cache.get(("b",)) == (entries, None)
