#!/usr/bin/env python3
"""Run the coding workflow executor against a real repo with a real sub-agent.

This creates a mini issue-lifecycle workflow:
  1. bash: echo starting
  2. prompt (triage/pickup): agent reads the issue, triages
  3. prompt (implement): agent writes the code
  4. prompt (prepare-pr): agent creates a PR

Usage:
  python scripts/test_executor_coding.py
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modastack.workflow.executor import WorkflowExecutor, ExecutorResult
from modastack.workflow.schema import NodeDef, NodeType, TriggerDef, WorkflowDef
from modastack.workflow.state import WorkflowRun
from modastack.workflow.actions import ActionRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_coding")

REPO = "/Users/zkozick/dev/memorize"
ISSUE_ID = "TEST-HEALTH"
TITLE = "Add a health check endpoint"
BODY = (
    "Add a GET /health endpoint to the Cloudflare Worker that returns "
    '{"status": "ok", "timestamp": <unix-seconds>}. '
    "No auth required. Add a test for it in the existing vitest suite."
)


def build_workflow() -> WorkflowDef:
    nodes = {
        "prep": NodeDef(
            id="prep", type=NodeType.BASH,
            command=f"cd {REPO} && git checkout main && git pull --ff-only",
            timeout=30,
        ),
        "triage": NodeDef(
            id="triage", type=NodeType.PROMPT,
            session=ISSUE_ID,
            inject=(
                f"/pickup Issue #{ISSUE_ID}: {TITLE}\n\n"
                f"Description: {BODY}\n\n"
                f"Repo: {REPO}\n"
                f"Working directory: {REPO}"
            ),
            timeout=600,
            depends_on=["prep"],
        ),
        "implement": NodeDef(
            id="implement", type=NodeType.PROMPT,
            session=ISSUE_ID,
            inject=f"/implement {ISSUE_ID}",
            timeout=1200,
            depends_on=["triage"],
        ),
        "prepare_pr": NodeDef(
            id="prepare_pr", type=NodeType.PROMPT,
            session=ISSUE_ID,
            inject="/prepare-pr",
            timeout=1800,
            depends_on=["implement"],
        ),
    }

    return WorkflowDef(
        name="coding-test", version=1,
        trigger=TriggerDef(event="test"),
        nodes=nodes,
    )


def on_input(tool_name: str, tool_input: dict) -> str:
    """Handle agent questions — pick first option or answer directly."""
    question = tool_input.get("question", tool_input.get("questions", ""))
    options = tool_input.get("options", [])

    log.info(f"Agent asked: {question}")
    if options:
        for i, opt in enumerate(options):
            label = opt.get("label", opt) if isinstance(opt, dict) else opt
            log.info(f"  {i+1}. {label}")
        log.info("Auto-selecting first option")
        first = options[0]
        return first.get("label", str(first)) if isinstance(first, dict) else str(first)

    return "Proceed with your best judgment."


def main(resume_id: str | None = None):
    start = time.time()

    print(f"\n{'='*60}")
    print(f"  Coding Workflow Test")
    print(f"  Repo:  {REPO}")
    print(f"  Issue: {ISSUE_ID} — {TITLE}")
    print(f"{'='*60}\n")

    wf = build_workflow()

    if resume_id:
        run = WorkflowRun.load(resume_id)
        reset = run.retry_failed()
        print(f"  Resuming run {resume_id}")
        print(f"  Reset failed nodes: {reset}")
        for nid, ns in run.nodes.items():
            print(f"    {nid:15s} {ns.status}")
        print()
    else:
        event = {"type": "test", "data": {
            "issue_id": ISSUE_ID,
            "title": TITLE,
            "body": BODY,
            "repo": REPO,
        }}
        run = WorkflowRun.create("coding-test", event)

    def notify(msg):
        elapsed = time.time() - start
        print(f"\n  [{elapsed:.0f}s] [NOTIFY] {msg}\n")

    ex = WorkflowExecutor(
        wf, run,
        on_notify=notify,
        on_input_needed=on_input,
    )

    print(f"  Run ID: {run.run_id}")
    print(f"  Phases: prep → triage → implement → prepare-pr")
    print(f"  Starting...\n")

    status = ex.execute()

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"  Result: {status}  ({elapsed:.0f}s)")
    print(f"{'='*60}")

    for nid, ns in run.nodes.items():
        cost = ns.outputs.get("_agent_cost_usd", "")
        dur = ns.outputs.get("_agent_duration_ms", "")
        extra = ""
        if cost:
            extra = f"${cost:.2f} / {int(dur)/1000:.0f}s"
        elif ns.outputs.get("stdout"):
            extra = ns.outputs["stdout"][:50]
        elif ns.error:
            extra = f"ERROR: {ns.error[:50]}"
        print(f"  {nid:15s} {ns.status:10s} {extra}")

    print()
    return status == ExecutorResult.COMPLETED


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=str, default=None,
                        help="Resume a failed run by ID (e.g. 1ec4563a)")
    args = parser.parse_args()

    ok = main(resume_id=args.resume)
    sys.exit(0 if ok else 1)
