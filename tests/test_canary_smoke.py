"""Release canary gate (`scripts/canary-smoke.sh`).

Reproduces the v0.29.0 release flake: the functional CANARY-OK ask raced a cold
image-swap boot. `fly deploy` only waits for the manager HEALTHCHECK (up BEFORE
the Claude session is ready) and the canary auto-suspends, so the ask fast-fails
until the session spins up — but the old gate gave it only 3 x 30s = 90s, which
a cold boot blows past, failing a release on a perfectly good wheel.

The script is exercised for real (bash subprocess) with a stubbed `fly` on PATH
that models a session that only becomes ready after N fast-failing probes. The
load-bearing fix is encoded as a before/after: with the OLD 90s-equivalent
budget the same readiness delay FAILS the gate; with the shipped budget it
PASSES. A genuinely broken wheel (never answers) still fails.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SMOKE_SH = REPO / "scripts" / "canary-smoke.sh"


def _run_smoke(tmp_path, *, ready_after, max_wait, interval=1, ask_fail=False):
    """Run canary-smoke.sh against a stubbed `fly`.

    The stub models the cold-boot race: `ssh console` (the functional ask)
    fast-fails for the first `ready_after` calls, then answers CANARY-OK —
    unless `ask_fail`, which never answers (a broken wheel). A call counter
    persists across the stub's invocations via a file in `tmp_path`.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    counter = tmp_path / "calls"
    started = tmp_path / "machine_started"
    answer = "" if ask_fail else "CANARY-OK"
    fly = bindir / "fly"
    fly.write_text(
        "#!/usr/bin/env bash\n"
        "# stub fly: record `machine start`, model session-readiness on `ssh console`\n"
        f'counter="{counter}"\n'
        f'started="{started}"\n'
        'case "$1 $2" in\n'
        '  "machine start") : > "$started"; exit 0;;\n'
        'esac\n'
        # everything else here is the `ssh console -C <ask>` probe
        'n=0; [ -f "$counter" ] && n="$(cat "$counter")"\n'
        'n=$((n + 1)); printf %s "$n" > "$counter"\n'
        f'if [ "$n" -gt "{ready_after}" ]; then printf %s "{answer}"; fi\n'
        "exit 0\n"
    )
    fly.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CANARY_SMOKE_MAX_WAIT": str(max_wait),
        "CANARY_SMOKE_INTERVAL": str(interval),
        "CANARY_SMOKE_ASK_TIMEOUT": "1",
    }
    proc = subprocess.run(
        ["bash", str(SMOKE_SH), "ci-canary"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return proc, started, counter


def test_old_budget_would_have_failed_the_flake(tmp_path):
    """Reproduce the flake: a session that needs ~5 probes to come ready fails
    under the old 90s-equivalent budget (max_wait small, like 3 x 30s)."""
    proc, _, _ = _run_smoke(tmp_path, ready_after=5, max_wait=2)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "did not answer CANARY-OK" in proc.stderr


def test_shipped_budget_tolerates_a_cold_boot(tmp_path):
    """The fix: the same readiness delay passes under the shipped budget."""
    proc, _, counter = _run_smoke(tmp_path, ready_after=5, max_wait=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "answered CANARY-OK" in proc.stdout
    # it kept probing past the point the old 3-attempt budget gave up
    assert int(counter.read_text()) >= 6


def test_starts_the_machine_before_asking(tmp_path):
    """Boot off the clock — the gate wakes the auto-suspended canary up-front."""
    proc, started, _ = _run_smoke(tmp_path, ready_after=0, max_wait=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert started.exists(), "canary-smoke.sh must `fly machine start` before probing"


def test_ready_immediately_passes_first_attempt(tmp_path):
    proc, _, counter = _run_smoke(tmp_path, ready_after=0, max_wait=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert counter.read_text() == "1"


def test_broken_wheel_never_answers_and_fails(tmp_path):
    """A wheel that never answers must still abort the release."""
    proc, _, _ = _run_smoke(tmp_path, ready_after=0, max_wait=1, ask_fail=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "did not answer CANARY-OK" in proc.stderr


def test_canary_smoke_passes_shellcheck():
    sc = subprocess.run(["shellcheck", str(SMOKE_SH)], capture_output=True, text=True)
    assert sc.returncode == 0, sc.stdout + sc.stderr
