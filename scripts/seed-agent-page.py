"""Seed an isolated BOBI_HOME with every state the agent page renders.

A dev fixture for QA-ing the single-agent view (#887) without waiting for a
real team to produce a stalled workflow or a crashed session. Writes one
team, `content-review`, holding at least one of everything the page has a
branch for:

  sessions   idle manager · completed · failed · running · dead-pid (reads
             crashed on the next registry read) · a monitor check agent
  workflows  completed · suspended 50h ago (STALLED) · freshly suspended
  monitors   notified · a $0 cached tick · spawn-failed · one with a session
  savings    a script-cache ledger with a priced basis, and one without

Usage::

    python scripts/seed-agent-page.py /tmp/qa-home
    BOBI_HOME=/tmp/qa-home bobi app start

then open the printed URL at `#/agents/content-review`.

The agent is NOT started: the strip renders STOPPED from the manager's last
terminal record. Start it from the page to exercise the running state.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

HOME = Path(sys.argv[1])
AGENT = "content-review"
ROOT = HOME / "agents" / AGENT / "run"
os.environ["BOBI_HOME"] = str(HOME)
NOW = time.time()

for d in ("package/roles/director", "package/roles/review-worker",
          "package/workflows", "package/monitors", "state/sessions",
          "state/workflow/runs", "state/monitor_runs", "state/scripts",
          "workspace"):
    (ROOT / d).mkdir(parents=True, exist_ok=True)

(ROOT / "package" / "agent.md").write_text(
    "# Content Review\n\nWatches the editorial inbox, triages drafts, and "
    "files review tickets.\n")
(ROOT / "package" / "agent.yaml").write_text(yaml.dump({
    "version": "0.1.0", "agent": AGENT, "entry_point": "director",
    "chat": "slack", "spend_cap": 40,
    "brain": {"kind": "claude", "model": "sonnet-5", "max_turns": 40},
    "services": [
        {"name": "slack", "events": True, "channels": ["#editorial"]},
        {"name": "linear", "events": True, "required": True},
        {"name": "gmail", "events": True},
    ],
    "monitors": [
        {"name": "inbox-watch", "interval": "15m", "command": "true",
         "description": "watch the editorial inbox"},
        {"name": "stale-drafts", "at": ["09:00"], "command": "true",
         "description": "drafts with no activity in 2 days"},
    ],
}))
(ROOT / "package" / "roles" / "director" / "ROLE.md").write_text(
    "# Director\n\nDispatches work and talks to the team.\n")
(ROOT / "package" / "roles" / "review-worker" / "ROLE.md").write_text(
    "# Review worker\n\nCopy-edits drafts and triages incoming.\n")
for wf in ("triage", "publish"):
    (ROOT / "package" / "workflows" / f"{wf}.yaml").write_text(
        yaml.dump({"name": wf, "steps": []}))


def session(name, **kw):
    d = ROOT / "state" / "sessions" / name
    d.mkdir(parents=True, exist_ok=True)
    base = {"name": name, "session_id": f"sid-{name}", "role": "review-worker",
            "status": "completed", "pid": 0, "error": "",
            "started_at": NOW - 500, "terminal_at": NOW - 400,
            "last_activity": NOW - 400, "model_usage": {}, "requested_by": {},
            "total_cost_usd": 0.0, "title": "", "run_key": "",
            "model": "sonnet-5", "provider": "anthropic"}
    base.update(kw)
    (d / "state.json").write_text(json.dumps(base))


USAGE = {"anthropic:claude-sonnet-4-20250514": {
    "input_tokens": 1_400_000, "output_tokens": 210_000,
    "cached_input_tokens": 900_000}}

session("bobi-content-review-director", role="director", status="completed",
        terminal_at=NOW - 60, started_at=NOW - 7200, last_activity=NOW - 65,
        title="manager", model_usage=USAGE)
session("review-worker-3", title="copy-edit the Q3 launch post",
        requested_by={"session": "bobi-content-review-director"},
        started_at=NOW - 900, terminal_at=NOW - 720,
        model_usage={"anthropic:claude-sonnet-4-20250514": {
            "input_tokens": 320_000, "output_tokens": 41_000,
            "cached_input_tokens": 210_000}})
session("review-worker-4", title="triage inbound drafts", status="failed",
        error="turn errored: tool call exceeded budget",
        started_at=NOW - 600, terminal_at=NOW - 540, total_cost_usd=0.31)
session("review-worker-5", title="rebuild the style index", status="running",
        started_at=NOW - 210, terminal_at=0.0, last_activity=NOW - 12)
session("review-worker-6", title="draft the newsletter", status="running",
        pid=999999999, started_at=NOW - 3000, terminal_at=0.0)
session("check-agent-1", role="monitor", run_key="stale-drafts",
        title="stale-drafts check", started_at=NOW - 300,
        terminal_at=NOW - 288)


def _local(e):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(e))


def _utc(e):
    return datetime.fromtimestamp(e, tz=timezone.utc).isoformat()


def workflow(run_id, **kw):
    base = dict(run_id=run_id, workflow_name="triage",
                trigger_event={"type": "github/issue.opened", "data": {}},
                started_at=_local(NOW - 1800), completed_at=_local(NOW - 1500),
                status="completed", suspended_at_step=-1, await_event="",
                session_name="", variable_scopes={}, repo="", cwd="",
                run_key="", resumed_at="")
    base.update(kw)
    p = ROOT / "state" / "workflow" / "runs" / f"{run_id}.json"
    p.write_text(json.dumps(base, indent=2))


workflow("wf-done", session_name="review-worker-3")
workflow("wf-stalled", workflow_name="publish", status="waiting",
         started_at=_local(NOW - 50 * 3600), completed_at="",
         suspended_at_step=3, await_event="pr.merged")
workflow("wf-fresh", workflow_name="publish", status="waiting",
         started_at=_local(NOW - 300), completed_at="",
         suspended_at_step=1, await_event="deploy.done")


def monitor_run(monitor, **kw):
    base = dict(run_id=f"{monitor}-{len(kw)}{int(kw.get('t', 0))}",
                monitor=monitor, started_at=_utc(NOW - 200),
                ended_at=_utc(NOW - 195), outcome="quiet", reason="",
                flavor="command", script_cache_mode="", session_ref="",
                published=0)
    base.pop("t", None)
    kw.pop("t", None)
    base.update(kw)
    p = ROOT / "state" / "monitor_runs" / f"{monitor}.json"
    runs = json.loads(p.read_text())["runs"] if p.exists() else []
    runs.insert(0, base)
    p.write_text(json.dumps({"monitor": monitor, "runs": runs}, indent=2))


monitor_run("inbox-watch", run_id="inbox-watch-a1", outcome="notified",
            published=2, reason="posted digest to #editorial",
            started_at=_utc(NOW - 150), ended_at=_utc(NOW - 146))
monitor_run("inbox-watch", run_id="inbox-watch-a2", outcome="quiet",
            script_cache_mode="cached", flavor="check:script_cache",
            started_at=_utc(NOW - 900), ended_at=_utc(NOW - 899))
monitor_run("stale-drafts", run_id="stale-drafts-b1", outcome="failed",
            reason="spawn-failed: claude CLI exited before first turn",
            flavor="description", started_at=_utc(NOW - 400),
            ended_at=_utc(NOW - 398))
monitor_run("stale-drafts", run_id="stale-drafts-b2", outcome="quiet",
            flavor="description", session_ref="check-agent-1",
            started_at=_utc(NOW - 300), ended_at=_utc(NOW - 288))

(ROOT / "state" / "scripts" / "inbox-watch.state.json").write_text(json.dumps(
    {"cached_runs": 412, "fallback_runs": 7, "total_agent_cost_usd": 1.05}))
(ROOT / "state" / "scripts" / "stale-drafts.state.json").write_text(json.dumps(
    {"cached_runs": 88, "fallback_runs": 2, "total_agent_cost_usd": 0.0}))

print(f"seeded {AGENT} in {HOME}")
