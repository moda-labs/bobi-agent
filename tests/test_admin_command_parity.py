"""The admin command vocabulary exists TWICE, so it gets a gate.

``bobi/supervisor/admin.py`` decides what a deployment will execute;
``event-server/worker/src/fleet.ts`` decides what the server will forward, and
``index.ts`` rejects anything absent from its list with 400 "unknown command".
Those two lists are one contract in two languages.

A verb added to only the Python half is **unreachable in production** and
every Python test still passes - the supervisor implements it, and no command
ever arrives. A verb added to only the TypeScript half is accepted, published,
and then dropped by the supervisor without a reply, so the caller waits out its
whole timeout for a command that was never going to run.

Both failure modes are silent, which is why this is a test and not a comment.
It was written after the second half was in fact forgotten.
"""

import re
from pathlib import Path

from bobi.supervisor.admin import ADMIN_COMMANDS

FLEET_TS = (Path(__file__).resolve().parents[1]
            / "event-server" / "worker" / "src" / "fleet.ts")


def _worker_commands() -> set[str]:
    """The verbs in fleet.ts's ``ADMIN_COMMANDS`` array literal.

    Parsed rather than imported - there is no TS runtime here, and shelling to
    node would make a unit test depend on a toolchain. The array is a flat list
    of string literals by construction (it is typed ``as const``), so a regex
    over the block between the declaration and its closing bracket is exact.
    """
    source = FLEET_TS.read_text(encoding="utf-8")
    match = re.search(r"export const ADMIN_COMMANDS = \[(.*?)\] as const;",
                      source, re.DOTALL)
    assert match, f"ADMIN_COMMANDS array not found in {FLEET_TS}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_two_halves_of_the_command_vocabulary_agree():
    worker = _worker_commands()
    python = set(ADMIN_COMMANDS)

    unreachable = python - worker
    assert not unreachable, (
        "these commands are implemented by the supervisor but NOT allowed by "
        "the server, so they 400 before reaching any deployment: "
        f"{sorted(unreachable)}. Add them to ADMIN_COMMANDS in "
        f"{FLEET_TS.name}.")

    dropped = worker - python
    assert not dropped, (
        "the server forwards these but no supervisor executes them, so a "
        "caller waits out its whole timeout for a command that never runs: "
        f"{sorted(dropped)}.")


def test_the_parse_is_not_vacuous():
    """A regex that silently matched nothing would make the gate above pass
    for every possible drift."""
    worker = _worker_commands()
    assert len(worker) >= 15
    # A verb from each half of the vocabulary: the original lifecycle set and
    # the single-agent view's additions.
    assert {"restart", "chat", "runs", "resume_run"} <= worker
