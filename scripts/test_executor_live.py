#!/usr/bin/env python3
"""Live smoke test for the workflow executor.

Usage:
  python scripts/test_executor_live.py              # bash-only smoke test
  python scripts/test_executor_live.py --with-agent  # adds a real sub-agent call
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modastack.workflow.executor import WorkflowExecutor, ExecutorResult
from modastack.workflow.schema import (
    BranchDef, NodeDef, NodeType, TriggerDef, WorkflowDef,
)
from modastack.workflow.state import WorkflowRun
from modastack.workflow.actions import ActionRegistry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def smoke_test():
    """Bash + action + gate nodes — no real Claude sessions."""
    registry = ActionRegistry()
    registry.register("test.echo", lambda p: {"msg": p.get("text", "")})

    nodes = {
        "step1": NodeDef(
            id="step1", type=NodeType.BASH,
            command="echo 'Hello from the executor'",
        ),
        "step2": NodeDef(
            id="step2", type=NodeType.ACTION,
            action="test.echo",
            params={"text": "Issue ${{event.issue_id}}: ${{event.title}}"},
            depends_on=["step1"],
        ),
        "gate": NodeDef(
            id="gate", type=NodeType.GATE,
            depends_on=["step1"],
            branches={
                "yes": BranchDef(when="'Hello' in ${{step1.stdout}}"),
                "no": BranchDef(when="'Goodbye' in ${{step1.stdout}}"),
            },
            fallback="yes",
        ),
        "after_gate": NodeDef(
            id="after_gate", type=NodeType.BASH,
            command="echo 'Gate chose: ${{gate.branch}}'",
            depends_on=["gate"],
        ),
    }

    wf = WorkflowDef(name="smoke-test", version=1,
                      trigger=TriggerDef(event="test"), nodes=nodes)
    event = {"type": "test", "data": {
        "issue_id": "SMOKE-1", "title": "Executor smoke test",
        "repo": "modastack",
    }}
    run = WorkflowRun.create("smoke-test", event)

    print(f"\n{'='*60}")
    print(f"  Smoke Test — run {run.run_id}")
    print(f"{'='*60}\n")

    ex = WorkflowExecutor(wf, run, registry=registry,
                          on_notify=lambda msg: print(f"  [NOTIFY] {msg}"))
    status = ex.execute()

    print(f"\n  Status: {status}")
    for nid, ns in run.nodes.items():
        out = ns.outputs.get("stdout") or ns.outputs.get("msg") or ns.outputs.get("branch") or ""
        print(f"  {nid:15s} {ns.status:10s} {out[:60]}")

    print()
    return status == ExecutorResult.COMPLETED


def agent_test():
    """One real sub-agent call — launches a Claude Code session."""
    nodes = {
        "prep": NodeDef(
            id="prep", type=NodeType.BASH,
            command="echo 'Preparing agent test'",
        ),
        "agent": NodeDef(
            id="agent", type=NodeType.PROMPT,
            session="SMOKE-2",
            inject=(
                "Reply with exactly: EXECUTOR_TEST_OK\n"
                "Do not use any tools. Just reply with that text."
            ),
            timeout=120,
            depends_on=["prep"],
        ),
    }

    wf = WorkflowDef(name="agent-test", version=1,
                      trigger=TriggerDef(event="test"), nodes=nodes)
    event = {"type": "test", "data": {
        "issue_id": "SMOKE-2", "title": "Agent smoke test",
        "repo": str(Path.cwd()),
    }}
    run = WorkflowRun.create("agent-test", event)

    print(f"\n{'='*60}")
    print(f"  Agent Test — run {run.run_id}")
    print(f"  This will launch a real Claude Code session...")
    print(f"{'='*60}\n")

    def on_input(tool_name, tool_input):
        print(f"  [INPUT NEEDED] {tool_name}: {tool_input}")
        return "Use the first option"

    ex = WorkflowExecutor(wf, run,
                          on_notify=lambda msg: print(f"  [NOTIFY] {msg}"),
                          on_input_needed=on_input)
    status = ex.execute()

    print(f"\n  Status: {status}")
    for nid, ns in run.nodes.items():
        out = ns.outputs.get("stdout", "")
        cost = ns.outputs.get("_agent_cost_usd", "")
        extra = f"cost=${cost}" if cost else out[:60]
        print(f"  {nid:15s} {ns.status:10s} {extra}")

    print()
    return status == ExecutorResult.COMPLETED


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-agent", action="store_true",
                        help="Include a real sub-agent call")
    args = parser.parse_args()

    ok = smoke_test()
    if not ok:
        print("SMOKE TEST FAILED")
        sys.exit(1)

    if args.with_agent:
        ok = agent_test()
        if not ok:
            print("AGENT TEST FAILED")
            sys.exit(1)

    print("ALL TESTS PASSED")
