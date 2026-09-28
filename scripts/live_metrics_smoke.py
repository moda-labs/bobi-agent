#!/usr/bin/env python3
"""Disposable real-provider parity probe for fine-grained metrics."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bobi.brain.base import AssistantText, BrainUsage, TurnResult
from bobi.brain.claude import ClaudeBrain
from bobi.brain.codex import CodexBrain, _spawn_codex
from bobi.brain.codex_config import codex_home
from bobi.chat_history import claude_projects_dirs
from bobi.fsutil import atomic_write_text
from bobi.metrics.collector import MetricsCollector
from bobi.metrics.events import MetricsEvent
from bobi.metrics.providers import (
    ProviderUsage,
    claude_usage,
    codex_usage,
    parse_claude_transcript,
    parse_codex_rollout,
    parse_codex_stdout,
)
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_NAMES = {"claude": "anthropic", "codex": "openai"}
SMOKE_MARKER = ".bobi-metrics-smoke.json"


def _command_output(*argv: str) -> str:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=15
    ).stdout.strip()


def _preflight(provider: str) -> str:
    if provider == "claude":
        status = json.loads(_command_output("claude", "auth", "status", "--json"))
        if not status.get("loggedIn"):
            raise SystemExit("Claude CLI is not authenticated")
        return _command_output("claude", "--version")
    status = subprocess.run(
        ["codex", "login", "status"], capture_output=True, text=True, timeout=15
    )
    if status.returncode or "logged in" not in (status.stdout + status.stderr).lower():
        raise SystemExit("Codex CLI is not authenticated")
    return _command_output("codex", "--version")


@contextmanager
def _isolated_brain_defaults():
    """Keep the probe independent from the invoking Bobi agent's model pins."""
    names = ("BOBI_BRAIN_MODEL", "BOBI_BRAIN_EFFORT", "BOBI_BRAIN")
    previous = {name: os.environ.pop(name) for name in names if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(previous)


def _brain_options(model: str | None) -> dict | None:
    return {"model": model} if model else None


def _from_brain_usage(provider: str, usage: BrainUsage, scope: str) -> ProviderUsage:
    if provider == "claude":
        return claude_usage(
            usage.raw_usage,
            model=usage.model,
            provider_event_id=usage.provider_event_id,
            scope=scope,
        )
    return codex_usage(
        usage.raw_usage,
        model=usage.model,
        provider_event_id=usage.provider_event_id,
        scope=scope,
    )


def _sum(values: list[int | None]) -> int | None:
    return sum(value or 0 for value in values) if any(v is not None for v in values) else None


def _aggregate(items: list[ProviderUsage], *, source: str) -> list[ProviderUsage]:
    grouped: dict[str, list[ProviderUsage]] = defaultdict(list)
    for item in items:
        grouped[item.model].append(item)
    result = []
    for model, group in grouped.items():
        cache_write = _sum([item.cache_write_input_tokens for item in group])
        complete = all(item.cache_write_breakdown_complete for item in group)
        result.append(
            ProviderUsage(
                provider=group[0].provider,
                model=model,
                provider_event_id=f"aggregate:{source}:{model}",
                measurement_source=source,
                scope="turn",
                input_tokens=_sum([item.input_tokens for item in group]),
                uncached_input_tokens=_sum(
                    [item.uncached_input_tokens for item in group]
                ),
                cache_read_input_tokens=_sum(
                    [item.cache_read_input_tokens for item in group]
                ),
                cache_write_input_tokens=cache_write,
                cache_write_5m_input_tokens=_sum(
                    [item.cache_write_5m_input_tokens for item in group]
                ),
                cache_write_1h_input_tokens=_sum(
                    [item.cache_write_1h_input_tokens for item in group]
                ),
                cache_write_unknown_ttl_input_tokens=(
                    None
                    if complete
                    else _sum(
                        [item.cache_write_unknown_ttl_input_tokens for item in group]
                    )
                ),
                cache_write_breakdown_complete=complete,
                output_tokens=_sum([item.output_tokens for item in group]),
                reasoning_output_tokens=_sum(
                    [item.reasoning_output_tokens for item in group]
                ),
                raw_usage={"events": [item.raw_usage for item in group]},
            )
        )
    return result


def _find_named_jsonl(root: Path, needle: str, started_ns: int) -> Path:
    candidates = [
        path
        for path in root.rglob("*.jsonl")
        if path.stat().st_mtime_ns >= started_ns - 2_000_000_000
        and needle in path.name
    ]
    if not candidates:
        raise RuntimeError(f"provider transcript for {needle} was not found")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _find_claude_transcript(session_id: str, started_ns: int) -> Path:
    candidates = []
    for root in claude_projects_dirs():
        try:
            candidates.append(_find_named_jsonl(root, session_id, started_ns))
        except RuntimeError:
            continue
    if not candidates:
        raise RuntimeError(f"provider transcript for {session_id} was not found")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _codex_sessions_root() -> Path:
    return codex_home() / "sessions"


def _assert_common_parity(
    left: list[ProviderUsage],
    right: list[ProviderUsage],
    *,
    allow_left_extras: bool = False,
) -> None:
    left_by_model = {item.model: item for item in left}
    right_by_model = {item.model: item for item in right}
    models_match = (
        right_by_model.keys() <= left_by_model.keys()
        if allow_left_extras
        else left_by_model.keys() == right_by_model.keys()
    )
    if not models_match:
        raise RuntimeError(
            f"provider model mismatch: {sorted(left_by_model)} != {sorted(right_by_model)}"
        )
    for model in right_by_model:
        one = left_by_model[model].comparable_tokens()
        two = right_by_model[model].comparable_tokens()
        # Report primitive cache dimensions before the derived input total so
        # parity failures identify the provider field that was actually lost.
        for key in (
            "uncached_input_tokens",
            "cache_read_input_tokens",
            "cache_write_input_tokens",
            "cache_write_5m_input_tokens",
            "cache_write_1h_input_tokens",
            "cache_write_unknown_ttl_input_tokens",
            "cache_write_breakdown_complete",
            "output_tokens",
            "reasoning_output_tokens",
            "input_tokens",
        ):
            if two[key] is not None and one[key] != two[key]:
                raise RuntimeError(
                    f"token mismatch model={model} field={key}: "
                    f"{one[key]} != {two[key]}"
                )


async def _run_claude(
    prompt: str, cwd: Path, started_ns: int, model: str | None = None
):
    with _isolated_brain_defaults():
        session = ClaudeBrain().make_session(
            cwd=str(cwd), system_prompt="", options=_brain_options(model)
        )
    texts: list[str] = []
    result = None
    await session.connect()
    try:
        await session.query(prompt)
        async for message in session.receive_response():
            if isinstance(message, AssistantText) and message.text:
                texts.append(message.text)
            elif isinstance(message, TurnResult):
                result = message
    finally:
        await session.disconnect()
    if result is None or result.is_error:
        raise RuntimeError(result.error_text() if result else "Claude returned no result")
    transcript = _find_claude_transcript(result.session_id, started_ns)
    adapter = _aggregate(
        [_from_brain_usage("claude", item, "turn") for item in result.usage],
        source="provider_stream",
    )
    transcript_usage = _aggregate(
        parse_claude_transcript(transcript), source="claude_transcript"
    )
    # Claude's result may include exact usage for internal calls such as title
    # generation that the JSONL transcript does not persist. Every transcript
    # row must match, while result-only rows remain exact provider facts.
    _assert_common_parity(adapter, transcript_usage, allow_left_extras=True)
    return "\n".join(texts).strip(), result.session_id, adapter


async def _run_codex(
    prompt: str, cwd: Path, started_ns: int, model: str | None = None
):
    events = []

    async def capturing_runner(argv, run_cwd, stdin_text=None):
        async for event in _spawn_codex(argv, run_cwd, stdin_text):
            events.append(event)
            yield event

    with _isolated_brain_defaults():
        session = CodexBrain().make_session(
            cwd=str(cwd), system_prompt="", options=_brain_options(model)
        )
    session._runner = capturing_runner
    texts: list[str] = []
    result = None
    await session.connect()
    try:
        await session.query(prompt)
        async for message in session.receive_response():
            if isinstance(message, AssistantText) and message.text:
                texts.append(message.text)
            elif isinstance(message, TurnResult):
                result = message
    finally:
        await session.disconnect()
    if result is None or result.is_error:
        raise RuntimeError(result.error_text() if result else "Codex returned no result")
    rollout = _find_named_jsonl(
        _codex_sessions_root(), result.session_id, started_ns
    )
    rollout_usage = parse_codex_rollout(rollout, result.usage[0].model)
    if not rollout_usage:
        raise RuntimeError("Codex rollout contains no exact token usage record")
    stream_usage = parse_codex_stdout(events, rollout_usage[-1].model)
    _assert_common_parity([stream_usage], [rollout_usage[-1]])
    return "\n".join(texts).strip(), result.session_id, [rollout_usage[-1]]


def _write_database(
    db: Path,
    provider: str,
    provider_session_id: str,
    usage: list[ProviderUsage],
    wall_latency_ms: float,
) -> str:
    session_id = f"session-{uuid.uuid4().hex}"
    turn_id = f"turn-{uuid.uuid4().hex}"
    producer_id = f"live-smoke-{uuid.uuid4().hex}"
    now_us = time.time_ns() // 1000
    events = [
        MetricsEvent(
            event_type="session.recorded",
            producer_id=producer_id,
            producer_sequence=0,
            source="live_provider_probe",
            session_id=session_id,
            payload={
                "session_id": session_id,
                "session_name": f"phase0-{provider}",
                "provider_session_id": provider_session_id,
                "brain": provider,
                "provider": "anthropic" if provider == "claude" else "openai",
                "started_at_us": now_us,
                "ended_at_us": now_us,
                "status": "completed",
            },
        ),
        MetricsEvent(
            event_type="turn.recorded",
            producer_id=producer_id,
            producer_sequence=1,
            source="live_provider_probe",
            session_id=session_id,
            turn_id=turn_id,
            payload={
                "turn_id": turn_id,
                "session_id": session_id,
                "turn_index": 1,
                "trigger_kind": "live_smoke",
                "is_user_initiated": 1,
                "started_at_us": now_us,
                "ended_at_us": now_us,
                "wall_duration_ms": wall_latency_ms,
                "status": "completed",
            },
        ),
    ]
    for index, item in enumerate(usage, start=2):
        events.append(
            MetricsEvent(
                event_type="usage.recorded",
                producer_id=producer_id,
                producer_sequence=index,
                source=item.measurement_source,
                session_id=session_id,
                turn_id=turn_id,
                payload=item.to_measurement_payload(
                    measurement_id=f"measurement-{uuid.uuid4().hex}",
                    turn_id=turn_id,
                    observed_at_us=now_us,
                ),
            )
        )
    segment = db.with_name(f"{db.stem}-{producer_id}.telemetry")
    with SpoolWriter(segment, sync_every_frame=True) as writer:
        for event in events:
            writer.append(event)
    result = MetricsCollector(db).collect_segment(segment)
    if result.get("projected") != len(events):
        raise RuntimeError(f"collector did not project all live events: {result}")
    return turn_id


def _latest_turn(
    db: Path,
    *,
    provider: str,
    after_us: int = 0,
    trigger_kind: str | None = None,
) -> str | None:
    if not db.exists():
        return None
    conn = connect(db, readonly=True)
    try:
        clauses = ["s.provider=?", "t.started_at_us>=?", "t.ended_at_us IS NOT NULL"]
        values: list[object] = [PROVIDER_NAMES.get(provider, provider), after_us]
        if trigger_kind:
            clauses.append("t.trigger_kind=?")
            values.append(trigger_kind)
        row = conn.execute(
            "SELECT t.turn_id FROM turns AS t JOIN sessions AS s USING(session_id) "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY t.started_at_us DESC LIMIT 1",
            values,
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def wait_latest_turn(args: argparse.Namespace) -> str:
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        turn_id = _latest_turn(
            args.db,
            provider=args.provider,
            after_us=args.after_us,
            trigger_kind=args.trigger_kind,
        )
        if turn_id:
            print(turn_id)
            return turn_id
        time.sleep(0.1)
    raise SystemExit(
        f"no completed {args.provider} turn appeared in {args.db} within "
        f"{args.timeout:.1f}s"
    )


def _database_turn_usage(conn, turn_id: str) -> list[ProviderUsage]:
    rows = conn.execute(
        "SELECT * FROM best_usage WHERE turn_id=? AND scope='turn' "
        "ORDER BY model",
        (turn_id,),
    ).fetchall()
    result = []
    for row in rows:
        result.append(ProviderUsage(
            provider=str(row["provider"]),
            model=str(row["model"]),
            provider_event_id=str(row["provider_event_id"] or ""),
            measurement_source=str(row["measurement_source"]),
            scope="turn",
            input_tokens=row["input_tokens"],
            uncached_input_tokens=row["uncached_input_tokens"],
            cache_read_input_tokens=row["cache_read_input_tokens"],
            cache_write_input_tokens=row["cache_write_input_tokens"],
            cache_write_5m_input_tokens=row["cache_write_5m_input_tokens"],
            cache_write_1h_input_tokens=row["cache_write_1h_input_tokens"],
            cache_write_unknown_ttl_input_tokens=(
                row["cache_write_unknown_ttl_input_tokens"]
            ),
            cache_write_breakdown_complete=bool(
                row["cache_write_breakdown_complete"]
            ),
            output_tokens=row["output_tokens"],
            reasoning_output_tokens=row["reasoning_output_tokens"],
            raw_usage={},
        ))
    return result


def _codex_turn_usage(
    path: Path,
    *,
    started_at_us: int,
    ended_at_us: int,
    model: str,
) -> ProviderUsage:
    selected_model = model
    selected: list[tuple[int, ProviderUsage]] = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break
            payload = event.get("payload") or {}
            event_turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or "")
            if event.get("type") == "turn_context":
                selected_model = str(payload.get("model") or selected_model)
                continue
            if event.get("type") != "token_usage_record":
                continue
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                observed_at_us = int(datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).timestamp() * 1_000_000)
            except ValueError:
                continue
            if not (started_at_us - 2_000_000 <= observed_at_us <= ended_at_us + 5_000_000):
                continue
            usage = payload.get("turn_token_usage") or payload.get("usage")
            if isinstance(usage, dict):
                selected.append((observed_at_us, codex_usage(
                    usage,
                    model=selected_model,
                    provider_event_id=str(payload.get("response_id") or event_turn_id),
                    scope="turn",
                )))
    if not selected:
        raise RuntimeError("Codex rollout has no exact usage in the turn time window")
    return max(selected, key=lambda item: item[0])[1]


def _codex_parity_expected(
    database: ProviderUsage, rollout: ProviderUsage
) -> ProviderUsage:
    if database.model == rollout.model:
        return rollout
    if database.model == "codex":
        return replace(rollout, model=database.model)
    raise RuntimeError(
        f"Codex model mismatch: stream={database.model} rollout={rollout.model}"
    )


def verify_token_parity(args: argparse.Namespace) -> dict[str, object]:
    conn = connect(args.db, readonly=True)
    try:
        turn = conn.execute(
            "SELECT t.*, s.provider_session_id FROM turns AS t "
            "JOIN sessions AS s USING(session_id) WHERE t.turn_id=?",
            (args.turn_id,),
        ).fetchone()
        if turn is None:
            raise RuntimeError(f"turn {args.turn_id} was not found")
        database = _database_turn_usage(conn, args.turn_id)
        invocation_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT provider_event_id FROM llm_invocations "
                "WHERE turn_id=? AND provider_event_id IS NOT NULL",
                (args.turn_id,),
            )
        }
    finally:
        conn.close()
    if not database or any(item.measurement_source != "provider_stream" for item in database):
        raise RuntimeError("turn has no exact provider_stream usage")

    provider_session_id = str(turn["provider_session_id"] or "")
    if args.provider == "claude":
        transcript = _find_claude_transcript(
            provider_session_id, int(turn["started_at_us"]) * 1000
        )
        exact = [
            item for item in parse_claude_transcript(transcript)
            if item.provider_event_id in invocation_ids
        ]
        if not exact:
            raise RuntimeError("Claude transcript has no matching invocation usage")
        expected = _aggregate(exact, source="claude_transcript")
        _assert_common_parity(database, expected, allow_left_extras=True)
        source = transcript
    else:
        rollout = _find_named_jsonl(
            _codex_sessions_root(), provider_session_id, int(turn["started_at_us"]) * 1000
        )
        rollout_usage = _codex_turn_usage(
            rollout,
            started_at_us=int(turn["started_at_us"]),
            ended_at_us=int(turn["ended_at_us"]),
            model=database[0].model,
        )
        expected = [_codex_parity_expected(database[0], rollout_usage)]
        _assert_common_parity(database, expected)
        source = rollout

    artifact = {
        "provider": args.provider,
        "turn_id": args.turn_id,
        "is_estimated": 0,
        "parity": "exact",
        "provider_source": str(source),
        "usage": {item.model: item.comparable_tokens() for item in database},
    }
    if args.provider == "codex":
        artifact["provider_model"] = rollout_usage.model
    if getattr(args, "json_out", None):
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(
        f"PASS provider={args.provider} turn_id={args.turn_id} "
        "is_estimated=0 parity=exact"
    )
    return artifact


def _canonical_tool(name: str) -> str:
    normalized = name.strip().lower().replace("-", "_")
    if normalized in {"read", "view", "read_file", "view_file"}:
        return "view_file"
    if normalized in {"edit", "write", "edit_file", "write_file", "apply_patch"}:
        return "edit_file"
    return normalized


def _contains_ordered(actual: list[str], required: list[str]) -> bool:
    remaining = iter(actual)
    return all(any(value == wanted for value in remaining) for wanted in required)


def verify_tools(args: argparse.Namespace) -> dict[str, object]:
    conn = connect(args.db, readonly=True)
    try:
        rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM tool_executions WHERE turn_id=? ORDER BY started_at_us",
                (args.turn_id,),
            )
        ]
    finally:
        conn.close()
    if len(rows) < args.minimum_tool_count:
        raise RuntimeError(
            f"turn {args.turn_id} has {len(rows)} tools, expected at least "
            f"{args.minimum_tool_count}"
        )
    for row in rows:
        if row["ended_at_us"] is None or row["ended_at_us"] < row["started_at_us"]:
            raise RuntimeError(f"tool {row['tool_execution_id']} has invalid timing")
        if not row["triggering_invocation_id"]:
            raise RuntimeError(f"tool {row['tool_execution_id']} has no triggering invocation")
        if row["output_estimated_tokens"] is not None:
            raise RuntimeError("Phase 1 must not fabricate per-tool billing tokens")
    required_tools = [
        _canonical_tool(value) for value in (args.required_tools or "").split(",") if value
    ]
    actual_tools = [_canonical_tool(str(row["tool_name"])) for row in rows]
    if required_tools and not _contains_ordered(actual_tools, required_tools):
        raise RuntimeError(f"tool sequence {actual_tools} does not contain {required_tools}")
    required_kinds = [value for value in (args.required_kinds or "").split(",") if value]
    actual_kinds = [str(row["tool_kind"]) for row in rows]
    if required_kinds and not _contains_ordered(actual_kinds, required_kinds):
        raise RuntimeError(f"tool kinds {actual_kinds} do not contain {required_kinds}")
    artifact = {
        "turn_id": args.turn_id,
        "tools": actual_tools,
        "tool_kinds": actual_kinds,
        "timings": "complete",
        "token_contribution": "unavailable_not_fabricated",
    }
    if getattr(args, "json_out", None):
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(
        f"PASS turn_id={args.turn_id} tools={','.join(actual_tools)} "
        "timings=complete token_contribution=unavailable_not_fabricated"
    )
    return artifact


def _smoke_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in ("BOBI_BRAIN", "BOBI_BRAIN_MODEL", "BOBI_BRAIN_EFFORT"):
        env.pop(name, None)
    env["BOBI_HOME"] = str(home)
    env["BOBI_METRICS_MODE"] = "shadow"
    env.setdefault("BOBI_EVENT_SERVER", "http://localhost:8080")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    return env


def _bobi(env: dict[str, str], *argv: str, timeout: int = 300) -> str:
    executable = Path(sys.executable).with_name("bobi")
    try:
        completed = subprocess.run(
            [str(executable), *argv],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stdout or exc.stderr or "command failed").strip()
        raise RuntimeError(f"bobi {' '.join(argv)}: {detail}") from None
    return completed.stdout.strip()


def _wait_agent_ready(run_root: Path, timeout: float = 120) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    sessions = run_root / "state" / "sessions"
    while time.monotonic() < deadline:
        for state_path in sessions.glob("*/state.json"):
            try:
                state = json.loads(state_path.read_text())
            except (OSError, ValueError, TypeError):
                continue
            if state.get("role") != "manager":
                continue
            if state.get("status") == "idle":
                return state
            if state.get("status") in {"error", "crashed", "failed"}:
                raise RuntimeError(
                    f"manager failed during startup: {state.get('error') or state}"
                )
        time.sleep(0.1)
    raise RuntimeError(f"manager did not become idle within {timeout:.1f}s")


def _configure_installed_smoke_package(package: Path, provider: str) -> None:
    config = yaml.safe_load(package.read_text()) or {}
    brain = config.get("brain")
    if not isinstance(brain, dict):
        brain = {}
    brain["kind"] = provider
    if provider == "claude":
        # Keep the disposable smoke independent from ambient model defaults.
        brain["model"] = "opus"
    config["brain"] = brain
    atomic_write_text(package, yaml.safe_dump(config, sort_keys=False))


def provision(args: argparse.Namespace) -> None:
    home = args.bobi_home.resolve()
    marker = home / SMOKE_MARKER
    if home.exists() and any(home.iterdir()) and not marker.exists():
        raise SystemExit(f"refusing non-disposable BOBI_HOME without {SMOKE_MARKER}: {home}")
    home.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"created_by": "live_metrics_smoke.py"}) + "\n")
    env = _smoke_env(home)
    packs = (
        (args.claude_agent, REPO_ROOT / "tests" / "fixtures" / "claude-smoke", "claude"),
        (args.codex_agent, REPO_ROOT / "tests" / "fixtures" / "codex-smoke", "codex"),
    )
    for agent, pack, provider in packs:
        with tempfile.TemporaryDirectory(prefix=f"bobi-{provider}-metrics-smoke-") as tmp:
            configured_pack = Path(tmp) / pack.name
            shutil.copytree(pack, configured_pack)
            _configure_installed_smoke_package(
                configured_pack / "agent.yaml", provider
            )
            _bobi(
                env, "agents", "install", str(configured_pack),
                "--name", agent, "--non-interactive",
            )
        print(f"READY agent={agent} provider={provider}")


def _wait_for_collector_health(path: Path, timeout: float = 30) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            time.sleep(0.1)
            continue
        if health.get("db_ready") and health.get("role") == "active":
            return health
        time.sleep(0.1)
    raise RuntimeError(f"collector health did not become active: {path}")


def installed_matrix(args: argparse.Namespace) -> None:
    checks = {value for value in args.checks.split(",") if value}
    unsupported = checks - {"single-turn", "tool-loop"}
    if unsupported:
        raise SystemExit(
            "Phase 1 installed-matrix supports single-turn and tool-loop only; "
            f"unsupported: {','.join(sorted(unsupported))}"
        )
    home = args.bobi_home.resolve()
    if not (home / SMOKE_MARKER).exists():
        raise SystemExit(f"run provision first; missing {home / SMOKE_MARKER}")
    artifacts = (args.artifacts or home / "metrics-smoke-artifacts").resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    env = _smoke_env(home)
    providers = {value for value in args.providers.split(",") if value}
    unsupported_providers = providers - PROVIDER_NAMES.keys()
    if not providers or unsupported_providers:
        raise SystemExit(
            "providers must contain claude and/or codex; unsupported: "
            f"{','.join(sorted(unsupported_providers)) or 'none selected'}"
        )
    matrix = tuple(
        item for item in (
            ("claude", args.claude_agent),
            ("codex", args.codex_agent),
        )
        if item[0] in providers
    )
    for provider, agent in matrix:
        _preflight(provider)
        run_root = home / "agents" / agent / "run"
        db = run_root / "state" / "metrics" / "metrics.db"
        try:
            _bobi(env, "agent", agent, "start", "--fresh", timeout=120)
            _wait_agent_ready(run_root)
            if "single-turn" in checks:
                after_us = time.time_ns() // 1000
                response = _bobi(
                    env,
                    "agent", agent, "message",
                    "Reply with exactly METRICS_SMOKE_OK.",
                    "--wait", "--timeout", "300",
                    timeout=330,
                )
                if response.strip() != "METRICS_SMOKE_OK":
                    raise RuntimeError(f"{provider} response mismatch: {response!r}")
                turn_id = wait_latest_turn(argparse.Namespace(
                    db=db,
                    provider=provider,
                    after_us=after_us,
                    trigger_kind=None,
                    timeout=30.0,
                ))
                verify_token_parity(argparse.Namespace(
                    agent=agent,
                    provider=provider,
                    turn_id=turn_id,
                    db=db,
                    json_out=artifacts / f"{provider}-single-turn.json",
                ))
            if "tool-loop" in checks:
                smoke_file = run_root / "workspace" / "LIVE_SMOKE.txt"
                smoke_file.parent.mkdir(parents=True, exist_ok=True)
                smoke_file.write_text("state=before\n")
                after_us = time.time_ns() // 1000
                prompt = (
                    "Use the Read tool on LIVE_SMOKE.txt. Use the Edit tool to change "
                    "state=before to state=after. Use the Read tool again to verify it, "
                    "then reply with exactly TOOL_SMOKE_OK."
                    if provider == "claude"
                    else
                    "Inspect LIVE_SMOKE.txt, use the file editing tool to change "
                    "state=before to state=after, inspect it again, then reply with "
                    "exactly TOOL_SMOKE_OK."
                )
                response = _bobi(
                    env,
                    "agent", agent, "message", prompt,
                    "--wait", "--timeout", "300",
                    timeout=330,
                )
                if response.strip() != "TOOL_SMOKE_OK":
                    raise RuntimeError(f"{provider} tool response mismatch: {response!r}")
                turn_id = wait_latest_turn(argparse.Namespace(
                    db=db,
                    provider=provider,
                    after_us=after_us,
                    trigger_kind=None,
                    timeout=30.0,
                ))
                required_tools = "view_file,edit_file,view_file" if provider == "claude" else ""
                required_kinds = "file_read,file_edit,file_read" if provider == "claude" else "shell,file_edit,shell"
                verify_tools(argparse.Namespace(
                    db=db,
                    turn_id=turn_id,
                    required_tools=required_tools,
                    required_kinds=required_kinds,
                    minimum_tool_count=3,
                    json_out=artifacts / f"{provider}-tool-loop.json",
                ))
            health = _wait_for_collector_health(
                run_root / "state" / "metrics" / "collector.state.json"
            )
            (artifacts / f"{provider}-collector-health.json").write_text(
                json.dumps(health, indent=2, sort_keys=True) + "\n"
            )
            if health.get("uncommitted_spool_bytes") != 0:
                raise RuntimeError(f"collector backlog remains for {provider}: {health}")
            print(
                f"PASS provider={provider} collector_role=active "
                "uncommitted_spool_bytes=0"
            )
        finally:
            subprocess.run(
                [str(Path(sys.executable).with_name("bobi")), "agent", agent, "stop"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )


def provider_probe(args: argparse.Namespace) -> None:
    version = _preflight(args.provider)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"bobi-metrics-{args.provider}-", dir=args.db.parent
    ) as cwd:
        started_ns = time.time_ns()
        wall_started = time.perf_counter()
        if args.provider == "claude":
            response, provider_session_id, usage = asyncio.run(
                _run_claude(args.prompt, Path(cwd), started_ns, args.model)
            )
        else:
            response, provider_session_id, usage = asyncio.run(
                _run_codex(args.prompt, Path(cwd), started_ns, args.model)
            )
        wall_latency_ms = (time.perf_counter() - wall_started) * 1000
    if response.strip() != "METRICS_SMOKE_OK":
        raise SystemExit(f"provider response mismatch: {response!r}")
    turn_id = _write_database(
        args.db, args.provider, provider_session_id, usage, wall_latency_ms
    )
    conn = connect(args.db, readonly=True)
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM best_usage WHERE turn_id = ?", (turn_id,)
        )
    ]
    conn.close()
    if not rows or any(row["is_estimated"] for row in rows):
        raise RuntimeError("live database has no exact best_usage row")
    database = {
        row["model"]: {
            key: row[key]
            for key in next(
                item for item in usage if item.model == row["model"]
            ).comparable_tokens()
        }
        for row in rows
    }
    expected = {item.model: item.comparable_tokens() for item in usage}
    if database != expected:
        raise RuntimeError(f"database token mismatch: {database} != {expected}")
    artifact = {
        "provider": args.provider,
        "cli_version": version,
        "model_override": args.model,
        "turn_id": turn_id,
        "wall_latency_ms": wall_latency_ms,
        "is_estimated": 0,
        "parity": "exact",
        "usage": expected,
    }
    artifact_path = args.db.with_suffix(".parity.json")
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(
        f"PASS provider={args.provider} response=METRICS_SMOKE_OK "
        f"wall_latency_ms={wall_latency_ms:.3f} is_estimated=0 parity=exact"
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("provider-probe")
    probe.add_argument("--provider", choices=("claude", "codex"), required=True)
    probe.add_argument("--model")
    probe.add_argument("--prompt", default="Reply with exactly METRICS_SMOKE_OK.")
    probe.add_argument("--db", type=Path, required=True)
    wait = commands.add_parser("wait-latest-turn")
    wait.add_argument("--db", type=Path, required=True)
    wait.add_argument("--provider", choices=("claude", "codex"), required=True)
    wait.add_argument("--after-us", type=int, default=0)
    wait.add_argument("--trigger-kind")
    wait.add_argument("--timeout", type=float, default=30)
    parity = commands.add_parser("verify-token-parity")
    parity.add_argument("--agent", required=True)
    parity.add_argument("--provider", choices=("claude", "codex"), required=True)
    parity.add_argument("--turn-id", required=True)
    parity.add_argument("--db", type=Path, required=True)
    parity.add_argument("--json-out", type=Path)
    tools = commands.add_parser("verify-tools")
    tools.add_argument("--db", type=Path, required=True)
    tools.add_argument("--turn-id", required=True)
    tools.add_argument("--required-tools", default="")
    tools.add_argument("--required-kinds", default="")
    tools.add_argument("--minimum-tool-count", type=int, default=1)
    tools.add_argument("--json-out", type=Path)
    provision_cmd = commands.add_parser("provision")
    provision_cmd.add_argument(
        "--bobi-home", type=Path,
        default=Path(os.environ.get("BOBI_HOME", "")) if os.environ.get("BOBI_HOME") else None,
        required=not bool(os.environ.get("BOBI_HOME")),
    )
    provision_cmd.add_argument(
        "--claude-agent", default=os.environ.get("BOBI_CLAUDE_AGENT", "metrics-smoke-claude")
    )
    provision_cmd.add_argument(
        "--codex-agent", default=os.environ.get("BOBI_CODEX_AGENT", "metrics-smoke-codex")
    )
    matrix = commands.add_parser("installed-matrix")
    matrix.add_argument(
        "--bobi-home", type=Path,
        default=Path(os.environ.get("BOBI_HOME", "")) if os.environ.get("BOBI_HOME") else None,
        required=not bool(os.environ.get("BOBI_HOME")),
    )
    matrix.add_argument(
        "--claude-agent", default=os.environ.get("BOBI_CLAUDE_AGENT", "metrics-smoke-claude")
    )
    matrix.add_argument(
        "--codex-agent", default=os.environ.get("BOBI_CODEX_AGENT", "metrics-smoke-codex")
    )
    matrix.add_argument("--checks", default="single-turn,tool-loop")
    matrix.add_argument("--providers", default="claude,codex")
    matrix.add_argument("--artifacts", type=Path)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "provider-probe":
        provider_probe(args)
    elif args.command == "wait-latest-turn":
        wait_latest_turn(args)
    elif args.command == "verify-token-parity":
        verify_token_parity(args)
    elif args.command == "verify-tools":
        verify_tools(args)
    elif args.command == "provision":
        provision(args)
    else:
        installed_matrix(args)


if __name__ == "__main__":
    main()
