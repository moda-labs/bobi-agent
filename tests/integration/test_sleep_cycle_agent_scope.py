"""Two agents under one OS user must not read each other's transcripts (#1142).

Claude Code keeps every session of an OS user under one ``projects`` tree, so
the tree alone cannot say which agent a transcript belongs to. These tests give
two real runtime roots one shared tree and drive the real scheduler, indexer
and cursor; only the sleep-cycle subprocess is captured.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from bobi import history, paths
from bobi.chat_history import project_slug
from bobi.monitors.scheduler import MonitorScheduler
from bobi.monitors.schema import Monitor
from bobi.monitors.sleep_cycle import read_cursor, write_cursor

from .conftest import requires_claude


class Agent:
    """One runtime root, bound on demand: a process binds only one at a time."""

    def __init__(self, root: Path):
        self.root = root
        paths.state_dir(root)

    def bind(self):
        paths.bind_root(None)
        paths.bind_root(self.root)

    def sleep_cycle_task(self) -> str | None:
        """Run one due sleep cycle; return the task it would hand the agent."""
        self.bind()
        captured = {}

        def spawn(monitor, cwd, task, on_result):
            captured.update(task=task, on_result=on_result)

        scheduler = MonitorScheduler(
            publish=lambda event, data: True,
            state_path=paths.state_path(self.root) / "monitor_state.json",
            project_path=self.root, spawn_sleep_cycle=spawn)
        monitor = Monitor(name="sleep-cycle", sleep_cycle=True,
                          event="system/memory.updated", interval="6h")
        paths.long_term_memory_path(self.root).write_text(
            "## Facts\n\n## Decisions\n")
        scheduler._spawn_sleep_cycle(monitor, [self.root])
        if "on_result" in captured:
            captured["on_result"]({"success": True})
        return captured.get("task")

    def cursor(self) -> int:
        return read_cursor(paths.long_term_memory_cursor_path(self.root))


def _transcript(projects: Path, cwd: str, session_id: str, *texts: str) -> None:
    project = projects / project_slug(cwd)
    project.mkdir(parents=True, exist_ok=True)
    with (project / f"{session_id}.jsonl").open("a") as f:
        for text in texts:
            f.write(json.dumps({
                "type": "user", "cwd": cwd, "timestamp": "2026-11-03T00:00:00Z",
                "message": {"role": "user", "content": text},
            }) + "\n")


@pytest.fixture
def agents(tmp_path, monkeypatch):
    """Two agents sharing one projects tree, as two teams under one OS user."""
    previous = paths.bound_root()
    projects = tmp_path / "shared-claude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(projects.parent))
    alfred = Agent(tmp_path / "agents" / "alfred" / "run")
    eng = Agent(tmp_path / "agents" / "eng" / "run")
    yield alfred, eng, projects
    paths.bind_root(None)
    if previous is not None:
        paths.bind_root(previous)


class TestTwoAgentsOneProjectsTree:
    def test_each_sleep_cycle_reads_only_its_own_sessions(self, agents):
        alfred, eng, projects = agents
        # A worker runs in the repo it works on, outside its runtime root, and
        # the repo is shared: only the recorded session id can tell these apart.
        shared_repo = "/srv/work/shared-repo"
        _transcript(projects, str(alfred.root), "alfred-manager", "alfred manager note")
        _transcript(projects, shared_repo, "alfred-worker", "alfred worker note")
        _transcript(projects, str(eng.root), "eng-manager", "eng manager note")
        _transcript(projects, shared_repo, "eng-worker", "eng worker note")
        _transcript(projects, shared_repo, "human-session", "a person's own session")

        alfred.bind()
        history.record_owned_session("alfred-manager")
        history.record_owned_session("alfred-worker")
        eng.bind()
        history.record_owned_session("eng-manager")
        history.record_owned_session("eng-worker")

        alfred_task = alfred.sleep_cycle_task()
        assert "alfred manager note" in alfred_task
        assert "alfred worker note" in alfred_task
        for foreign in ("eng manager note", "eng worker note", "a person's own session"):
            assert foreign not in alfred_task

        eng_task = eng.sleep_cycle_task()
        assert "eng worker note" in eng_task
        assert "alfred" not in eng_task

        # The agent-taught read paths honour the same boundary.
        eng.bind()
        assert history.search("alfred") == []
        assert {c["session_id"] for c in history.conversations()} == {
            "eng-manager", "eng-worker"}

    def test_session_owned_after_the_cursor_moved_is_not_lost(self, agents):
        """An id cursor only works if a late-claimed session gets fresh ids.

        A session's first lines hit disk before bobi learns its id. If those
        lines were indexed then, its rows would sit below a cursor another
        session had already advanced past, and they would never be read.
        """
        alfred, _, projects = agents
        alfred.bind()
        # Claimed before the files exist, so the upgrade seed cannot claim "late".
        history.record_owned_session("first")
        # "late" is seen by the indexer before "first" exists, so an indexer that
        # took it now would give it a lower row id than anything "first" gets.
        _transcript(projects, str(alfred.root), "late", "late session note")
        history.index()
        _transcript(projects, str(alfred.root), "first", "first session note")

        assert "late session note" not in alfred.sleep_cycle_task()
        assert alfred.cursor() > 0

        alfred.bind()
        history.record_owned_session("late")
        assert "late session note" in alfred.sleep_cycle_task()

    def test_foreign_stretch_does_not_wedge_an_existing_cursor(self, agents):
        """The VM's shape: foreign rows already in history.db above the cursor."""
        alfred, _, projects = agents
        alfred.bind()
        # Ownership before the fix's first read, so the seed cannot claim it.
        history.record_owned_session("alfred-old")
        _transcript(projects, str(alfred.root), "alfred-old", "already absorbed")
        history.index()
        cursor_path = paths.long_term_memory_cursor_path(alfred.root)
        write_cursor(cursor_path, history.messages_since(0)[-1]["id"])

        # Rows the pre-fix indexer took from another agent.
        conn = history.sqlite3.connect(str(history._db_path()))
        conn.executemany(
            "INSERT INTO messages (session_id, type, role, content) VALUES (?,?,?,?)",
            [("eng-old", "user", "user", f"foreign {i}") for i in range(500)])
        conn.commit()
        conn.close()
        below = alfred.cursor()

        # Nothing of its own yet: no run, and the cursor stays put.
        assert alfred.sleep_cycle_task() is None
        assert alfred.cursor() == below

        _transcript(projects, str(alfred.root), "alfred-old", "new own note")
        task = alfred.sleep_cycle_task()
        assert "new own note" in task
        assert "foreign" not in task
        assert "already absorbed" not in task
        # Advanced past the whole foreign stretch to its own newest row.
        assert alfred.cursor() > below + 500

    def test_first_read_after_upgrade_keeps_the_agents_existing_sessions(self, agents):
        """No record exists yet on upgrade: the seed must not drop own memory."""
        alfred, eng, projects = agents
        worker_cwd = "/srv/work/alfred-repo"
        sessions = {
            "old-manager": (str(alfred.root), "old manager note"),
            "old-worker": (worker_cwd, "old worker note"),
            "rotated": ("/srv/work/alfred-rotated", "rotated note"),
            "eng-manager": (str(eng.root), "eng note"),
            "stranger": ("/srv/work/unrelated", "stranger note"),
        }
        for session_id, (cwd, text) in sessions.items():
            _transcript(projects, cwd, session_id, text)

        # The database the pre-fix indexer left, with every agent's sessions.
        alfred.bind()
        conn = history.sqlite3.connect(str(history._db_path()))
        history._init_db(conn)
        conn.executemany(
            "INSERT INTO conversations (session_id, project, cwd) VALUES (?,?,?)",
            [(sid, cwd, cwd) for sid, (cwd, _) in sessions.items() if sid != "rotated"])
        conn.commit()
        conn.close()

        registry = paths.sessions_dir(alfred.root)
        (registry / "wf-run").mkdir()
        (registry / "wf-run" / "state.json").write_text(json.dumps(
            {"name": "wf-run", "session_id": "", "cwd": worker_cwd}))
        # Current id of a session whose cwd no registry entry records.
        (registry / "mgr.id").write_text("rotated\n")
        # A bad file must cost only itself, not every later ownership read.
        (registry / "bad.id").write_bytes(b"\xff\xfe")
        (registry / "bad-entry").mkdir()
        (registry / "bad-entry" / "state.json").write_text("[]")

        task = alfred.sleep_cycle_task()
        for own in ("old manager note", "old worker note", "rotated note"):
            assert own in task
        assert "eng note" not in task
        assert "stranger note" not in task

    def test_new_agent_in_a_shared_repo_claims_only_its_own_session(self, agents):
        """Working-directory claims preserve an old database; a new agent has none."""
        alfred, _, projects = agents
        shared_repo = "/srv/work/shared-repo"
        _transcript(projects, shared_repo, "other-agent", "other agent note")
        _transcript(projects, shared_repo, "human-session", "a person's own session")
        _transcript(projects, shared_repo, "mine", "my first note")
        registry = paths.sessions_dir(alfred.root)
        (registry / "wf-run").mkdir()
        (registry / "wf-run" / "state.json").write_text(json.dumps(
            {"name": "wf-run", "session_id": "", "cwd": shared_repo}))

        alfred.bind()
        history.record_owned_session("mine")

        task = alfred.sleep_cycle_task()
        assert "my first note" in task
        assert "other agent note" not in task
        assert "a person's own session" not in task

    def test_index_totals_exclude_other_agents_rows(self, agents):
        alfred, _, projects = agents
        alfred.bind()
        history.record_owned_session("mine")
        _transcript(projects, str(alfred.root), "mine", "my note")
        history.index()
        conn = history.sqlite3.connect(str(history._db_path()))
        conn.execute("INSERT INTO conversations (session_id) VALUES ('foreign')")
        conn.execute("INSERT INTO messages (session_id, type) VALUES ('foreign', 'user')")
        conn.commit()
        conn.close()

        stats = history.index()
        assert (stats["total_conversations"], stats["total_messages"]) == (1, 1)


@requires_claude
@pytest.mark.claude
@pytest.mark.timeout(180)
class TestRealClaudeSessionIsOwned:
    def test_real_session_is_recorded_under_its_transcript_name(self, bobi_env):
        """The id bobi records must be the name Claude Code gives the file."""
        from bobi.brain import get_brain
        from bobi.brain.base import TurnResult
        from bobi.chat_history import find_claude_transcript

        session = get_brain().make_session(
            cwd=str(bobi_env.project_path), system_prompt="Answer in one word.",
            options={"max_turns": 1})

        async def turn() -> str:
            await session.connect()
            try:
                await session.query("Say ok.")
                async for msg in session.receive_response():
                    if isinstance(msg, TurnResult):
                        return msg.session_id
            finally:
                await session.disconnect()
            return ""

        session_id = asyncio.run(turn())
        assert session_id
        assert session_id in history._owned_sessions()
        assert find_claude_transcript(session_id) is not None
        assert os.path.basename(find_claude_transcript(session_id)) == f"{session_id}.jsonl"
