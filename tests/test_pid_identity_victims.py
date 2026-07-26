"""No pid-identity test may bound its victim with a wall clock.

The pid-identity tests spawn a real process, write its pid into a pidfile, and
assert how the stop path classifies it. A victim bounded by ``time.sleep(N)``
decides that classification by how busy the machine is: once the box is loaded
enough that more than N seconds pass before the assertion, the child exits
unreaped and becomes a ZOMBIE - still answering ``os.kill(pid, 0)``, but with
an empty ``/proc/<pid>/cmdline``. ``process_argv`` then returns ``[]`` and the
stop path reports ``unidentified`` where the test demanded ``stale``.

That is not a hypothetical: it took down
``test_stale_pid_reused_by_another_process_is_never_signalled`` and
``test_never_signals_a_pid_the_sidecar_no_longer_owns`` on a contended run, and
reproduces with nothing more exotic than delaying the assertion by a second.

The ``sacrificial_process`` fixture spawns a victim that cannot exit on its
own. This guard keeps it the only spelling, because the timed version fails
rarely and somewhere else, which is the expensive way to find out.
"""

import re
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent

# A timed sleep reached through the `time` module inside a spawned `-c`
# program, however it is spelled: `import time; time.sleep(N)`, the same over a
# real newline in a triple-quoted program, or `__import__('time').sleep(N)`. An
# ordinary in-test `time.sleep(0.1)` has no `import time` beside it and does
# not match - only a program being handed to another process does.
_TIMED_VICTIM = re.compile(
    r"""(?:import[ \t]+time[;\n\\,][^\n]{0,120}?"""
    r"""|__import__\([ \t]*['"]time['"][ \t]*\)\."""
    r"""|from[ \t]+time[ \t]+import[ \t]+sleep[^\n]{0,120}?)"""
    r"""[ \t]*(?:time\.)?sleep\(""",
)

# `sleep N` as a line of a shell stand-in, whether escaped or over a real
# newline, indented or `exec`ed. It runs out the same way.
_TIMED_SHELL_VICTIM = re.compile(r"(?:\\n|\n)[ \t]*(?:exec[ \t]+)?sleep[ \t]+\d")

# `sleep` as the spawned program itself: Popen(["sleep", "30"]) or via a shell.
_TIMED_ARGV_VICTIM = re.compile(r"""['"]sleep['"][ \t]*,[ \t]*['"]?\d"""
                                r"""|['"]-c['"][ \t]*,[ \t]*['"][^'"\n]*\bsleep[ \t]+\d""")


def _offenders(pattern: re.Pattern) -> list[str]:
    """Scan the whole file text, so a spelling split across lines is seen.

    Every .py under tests/ - not just `test_*.py`. A shared spawn helper
    belongs in a `conftest.py` or a utils module, which is exactly where this
    would otherwise hide, and it is where `sacrificial_process` itself lives.
    """
    hits = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path.name == Path(__file__).name or "__pycache__" in path.parts:
            continue
        text = path.read_text()
        for match in pattern.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            snippet = " ".join(match.group(0).split())[:80]
            hits.append(f"{path.relative_to(_TESTS)}:{lineno}: {snippet}")
    return hits


@pytest.mark.parametrize("pattern, spelling", [
    (_TIMED_VICTIM, 'python -c "import time; time.sleep(N)"'),
    (_TIMED_SHELL_VICTIM, "a shell stand-in with a `sleep N` line"),
    (_TIMED_ARGV_VICTIM, '`sleep` spawned directly, e.g. Popen(["sleep", "30"])'),
])
def test_no_test_spawns_a_victim_bounded_by_a_wall_clock(pattern, spelling):
    offenders = _offenders(pattern)
    assert not offenders, (
        f"{len(offenders)} victim(s) spawned as {spelling}. Use the "
        "`sacrificial_process` fixture, whose victim blocks on a pipe and so "
        "cannot run out its clock while a loaded box delays the assertion:\n  "
        + "\n  ".join(offenders)
    )
