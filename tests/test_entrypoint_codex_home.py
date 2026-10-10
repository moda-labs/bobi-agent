"""The entrypoint's codex config dir, exercised without docker (#958).

Section 3c exports ``CODEX_HOME`` at ``${DATA_DIR}/codex`` on every brain, so a
human-minted ``auth.json`` survives a roll and the 8 workers share it. These
tests boot the REAL ``docker/docker-entrypoint.sh`` in a temp dir with the
external commands stubbed, which is what makes the boot-order matrix and the
planted-symlink refusals runnable in the unit lane rather than only in docker.

The stub for ``gosu`` is the probe: the entrypoint's last act is
``exec gosu "${APP_USER}" env "HOME=..." ... bobi agent <name> supervise``, so
dumping that process's environment captures what the manager would actually
receive - which is both the export assertion and the "it survives `env` without
`-i`" assertion, measured rather than reasoned about.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "docker" / "docker-entrypoint.sh"

# Commands the entrypoint shells out to. Stubbed so a boot needs no root, no
# docker, no node and no network.
_STUBS = {
    # The handoff probe. It must PASS THROUGH: `as_app` routes the entrypoint's
    # own `python -m bobi.auth_bootstrap credential-status` calls through gosu,
    # so a stub that merely exits 0 would be read as "credential valid" and
    # would silently skip every write the guard is supposed to gate.
    "gosu": (
        '#!/bin/sh\n'
        'n=$(cat "$BOOT_PROBE/gosu.seq" 2>/dev/null || echo 0)\n'
        'n=$((n + 1)); echo "$n" > "$BOOT_PROBE/gosu.seq"\n'
        '{ env; echo "ARGV: $*"; } > "$BOOT_PROBE/gosu-$n.env"\n'
        'shift\n'
        'exec "$@"\n'
    ),
    # chown needs root; the test is about path shape and ownership intent.
    "chown": '#!/bin/sh\necho "chown $*" >> "$BOOT_PROBE/chown.log"\nexit 0\n',
    # The section-5 guard asks the CLI whether `supervise` is really there.
    "bobi": (
        '#!/bin/sh\n'
        'echo "bobi $*" >> "$BOOT_PROBE/bobi.log"\n'
        'if [ "$1" = "agent" ] && [ "$2" = "--help" ]; then\n'
        '  echo "  supervise  Run the supervisor sidecar"\n'
        'fi\n'
        'exit 0\n'
    ),
    "claude": '#!/bin/sh\nexit 0\n',
    "codex": '#!/bin/sh\nexit 0\n',
    "tini": '#!/bin/sh\nexec "$@"\n',
}


def _fake_bin(tmp_path: Path, probe: Path, extra: dict | None = None) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in {**_STUBS, **(extra or {})}.items():
        script = bin_dir / name
        script.write_text(body)
        script.chmod(0o755)
    # `python` must stay real: the entrypoint calls it to resolve the team's
    # brain and for credential-status, which are what items 23/24 pin. It is
    # pinned to THIS tree's sources, not whatever the venv's editable install
    # happens to point at, or the boot would exercise another checkout.
    for name in ("python", "python3"):
        script = bin_dir / name
        script.write_text(
            '#!/bin/sh\n'
            f'PYTHONPATH="{REPO_ROOT}${{PYTHONPATH:+:$PYTHONPATH}}" '
            f'exec "{sys.executable}" "$@"\n'
        )
        script.chmod(0o755)
    probe.mkdir(parents=True, exist_ok=True)
    return bin_dir


def _boot(tmp_path: Path, *, brain: str = "claude", auth: str = "subscription",
          data_dir: Path | None = None, home: Path | None = None,
          baked_skills: bool = False, env: dict | None = None,
          extra_stubs: dict | None = None) -> dict:
    """Run one real boot. Returns ``{rc, log, manager_env, data_dir, home}``."""
    data_dir = data_dir or (tmp_path / "data")
    home = home or (tmp_path / "home")
    probe = tmp_path / "probe"
    for d in (data_dir, home):
        d.mkdir(parents=True, exist_ok=True)
    bin_dir = _fake_bin(tmp_path, probe, extra_stubs)
    if baked_skills:
        skills = tmp_path / "opt-skills"
        (skills / "demo").mkdir(parents=True, exist_ok=True)

    bobi_home = data_dir / ".bobi"
    run_root = bobi_home / "agents" / "test-agent" / "run"
    (run_root / "package").mkdir(parents=True, exist_ok=True)
    (run_root / "package" / "agent.yaml").write_text(
        f"agent: test-agent\nbrain:\n  kind: {brain}\n")

    boot_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "DATA_DIR": str(data_dir),
        "BOBI_HOME": str(bobi_home),
        "BOBI_AGENT": "test-agent",
        "BOBI_BRAIN": brain,
        "BOBI_AUTH": auth,
        "BOOT_PROBE": str(probe),
        "BOBI_SKIP_DEP_BOOTSTRAP": "1",
        **(env or {}),
    }
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT)], env=boot_env, capture_output=True,
        text=True, timeout=180,
    )
    # The manager's environment is the handoff call's, identified by its argv
    # rather than by filename order: `as_app` goes through gosu too.
    manager_env: dict[str, str] = {}
    for dump in probe.glob("gosu-*.env"):
        text = dump.read_text()
        if " supervise" not in text:
            continue
        for line in text.splitlines():
            if "=" in line and not line.startswith("ARGV: "):
                key, _, value = line.partition("=")
                manager_env[key] = value
    return {
        "rc": proc.returncode,
        "log": proc.stdout + proc.stderr,
        "manager_env": manager_env,
        "data_dir": data_dir,
        "home": home,
        "probe": probe,
    }


# 22. The boot-order matrix, with real assertions.

@pytest.mark.parametrize("brains", [
    ("claude",),
    ("codex",),
    # The brain can change without a new rootfs, because it is read from the
    # installed team on the volume when BOBI_BRAIN is unset. Both directions.
    ("claude", "codex", "claude"),
    ("codex", "claude", "codex"),
])
@pytest.mark.parametrize("baked_skills", [True, False])
@pytest.mark.parametrize("fresh_home", [False, True])
def test_boot_order_matrix_exports_codex_home(tmp_path, brains, baked_skills,
                                              fresh_home):
    """Item 22. Exit 0 is not the assertion - it passes with 3c omitted
    entirely. Each boot must also show CODEX_HOME exported as
    ``${DATA_DIR}/codex``, as a real directory, and hold that across a brain
    switch on one persisted volume.

    ``${HOME}`` is persisted by default on purpose: a per-boot-fresh HOME is a
    `fly deploy`, not a same-machine restart, and that axis is what hid an
    earlier blocker. ``fresh_home`` exercises the deploy case too.
    """
    data_dir = tmp_path / "data"
    home = tmp_path / "home"
    for index, brain in enumerate(brains):
        boot_home = (tmp_path / f"home-{index}") if fresh_home else home
        result = _boot(
            tmp_path / f"boot-{index}", brain=brain, data_dir=data_dir,
            home=boot_home, baked_skills=baked_skills,
        )
        assert result["rc"] == 0, result["log"]
        assert result["manager_env"].get("CODEX_HOME") == str(data_dir / "codex"), (
            f"boot {index} ({brain}) did not export CODEX_HOME:\n{result['log']}"
        )
        assert (data_dir / "codex").is_dir()
        assert not (data_dir / "codex").is_symlink()


# 25. The export reaches the manager.

def test_the_export_survives_gosu_env_without_dash_i(tmp_path):
    """Item 25. `env` without `-i` inherits, so an exported variable reaches
    the manager rather than needing to be added to four call sites.

    Pinned because an env-allowlist reading of `as_app`/`exec gosu ... env
    "HOME=..."` would wrongly conclude otherwise.
    """
    result = _boot(tmp_path)
    assert result["rc"] == 0, result["log"]
    env = result["manager_env"]
    assert env.get("CODEX_HOME") == str(result["data_dir"] / "codex")
    # The same process also carries the variables `env` sets explicitly, which
    # is what proves inheritance rather than a cleared environment.
    assert env.get("HOME") == str(result["home"])
    assert "BOBI_HOME" in env


def _auth_env(auth_mode: str, brain: str, *, ambient_openai: bool = True) -> dict:
    """The env `validate_auth_mode` requires for this mode/brain pair.

    api_key mode needs the brain's own provider key present; subscription mode
    needs it ABSENT, because it would outrank the OAuth credential. Those are
    the entrypoint's own preconditions, not this change's.
    """
    env: dict[str, str] = {}
    if auth_mode == "api_key":
        env["ANTHROPIC_API_KEY" if brain == "claude" else "OPENAI_API_KEY"] = (
            "sk-brain-key")
        if ambient_openai:
            # The ambient key codex-as-a-tool materialization reads.
            env["OPENAI_API_KEY"] = "sk-ambient-key"
    elif brain == "codex":
        # Subscription mode on a codex brain refuses an ambient OPENAI_API_KEY.
        env["OPENAI_API_KEY"] = ""
    return env


# 23. Planted symlinks, one outcome: fail, not repair.

@pytest.mark.parametrize("what", ["data-dir", "auth-file"])
def test_planted_symlinks_abort_the_boot_and_leave_the_target_untouched(
    tmp_path, what,
):
    """Item 23. Neither state is one any boot path creates, so repairing it
    silently would discard the only signal that something planted it.

    The entrypoint runs as root against a path the bobi user owns, and
    chown/mkdir -p and the credential write all follow a symlink.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "passwd").write_text("root:x:0:0:root:/root:/bin/sh\n")
    before = (protected / "passwd").read_text()
    before_mode = (protected / "passwd").stat().st_mode

    if what == "data-dir":
        (data_dir / "codex").symlink_to(protected)
        auth_mode = "subscription"
    else:
        (data_dir / "codex").mkdir()
        (data_dir / "codex" / "auth.json").symlink_to(protected / "passwd")
        # The api-key write is the root-run path that would follow the link.
        auth_mode = "api_key"

    result = _boot(
        tmp_path, data_dir=data_dir, auth=auth_mode,
        env=_auth_env(auth_mode, "claude"),
    )
    assert result["rc"] != 0, f"boot must refuse:\n{result['log']}"
    assert "FATAL" in result["log"]
    assert "refusing" in result["log"]
    assert (protected / "passwd").read_text() == before
    assert (protected / "passwd").stat().st_mode == before_mode


# 24. The credential guard and the conservative sweep.

_OAUTH = json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                     "tokens": {"refresh_token": "refresh-token-value",
                                "account_id": "acct"}})
_API_KEY = json.dumps({"OPENAI_API_KEY": "sk-stale-apikey"})
_BLANK_REFRESH = json.dumps({"tokens": {"refresh_token": ""}})

_STATES = {
    "absent": None,
    "api-key shaped": _API_KEY,
    "truncated": '{"tokens": {"refresh_to',
    "blank refresh token": _BLANK_REFRESH,
    "oauth": _OAUTH,
}

# (auth mode, brain) -> {state: expected outcome}
_EXPECTED = {
    ("api_key", "claude"): {
        "absent": "materialized", "api-key shaped": "replaced",
        "truncated": "replaced", "blank refresh token": "replaced",
        # The non-clobber guard: a human-minted credential survives a boot.
        "oauth": "intact",
    },
    # On a codex BRAIN every cell is main's behaviour, unguarded. This axis is
    # the only one that distinguishes a guard on the call site from a guard
    # inside the shared function.
    ("api_key", "codex"): {
        "absent": "materialized", "api-key shaped": "replaced",
        "truncated": "replaced", "blank refresh token": "replaced",
        "oauth": "replaced",
    },
    ("subscription", "claude"): {
        "absent": "absent", "api-key shaped": "removed",
        "truncated": "intact", "blank refresh token": "intact",
        "oauth": "intact",
    },
    ("subscription", "codex"): {
        "absent": "absent", "api-key shaped": "removed",
        "truncated": "intact", "blank refresh token": "intact",
        "oauth": "intact",
    },
}


@pytest.mark.parametrize("auth_mode", ["api_key", "subscription"])
@pytest.mark.parametrize("brain", ["claude", "codex"])
@pytest.mark.parametrize("state", list(_STATES))
def test_credential_guard_and_conservative_sweep(tmp_path, auth_mode, brain,
                                                 state):
    """Item 24, one table. The "absent" row's silence and the two left-intact
    rows are the pins for keeping the predicate conservative: a widened
    "delete anything credential-status rejects" would log on every pre-login
    boot of every subscription machine and would delete an unobserved schema.
    """
    data_dir = tmp_path / "data"
    codex_dir = data_dir / "codex"
    codex_dir.mkdir(parents=True)
    auth = codex_dir / "auth.json"
    payload = _STATES[state]
    if payload is not None:
        auth.write_text(payload)

    result = _boot(
        tmp_path, brain=brain, auth=auth_mode, data_dir=data_dir,
        env=_auth_env(auth_mode, brain),
    )
    assert result["rc"] == 0, result["log"]
    expected = _EXPECTED[(auth_mode, brain)][state]
    got = auth.read_text() if auth.exists() else None

    if expected == "absent":
        assert got is None
        assert "removing Codex API-key auth" not in result["log"], (
            "a pre-login subscription boot must stay silent")
    elif expected == "removed":
        assert got is None
        assert "removing Codex API-key auth" in result["log"]
    elif expected == "intact":
        assert got == payload, f"{state} under {auth_mode}/{brain} was modified"
        if auth_mode == "subscription" and state in {"truncated",
                                                     "blank refresh token"}:
            assert "removing Codex API-key auth" not in result["log"]
    elif expected == "materialized":
        assert got is not None and json.loads(got)["OPENAI_API_KEY"] == "sk-ambient-key"
    elif expected == "replaced":
        assert got is not None
        assert json.loads(got)["OPENAI_API_KEY"] == "sk-ambient-key"
        assert got != payload
    else:  # pragma: no cover - the table is closed
        raise AssertionError(expected)
