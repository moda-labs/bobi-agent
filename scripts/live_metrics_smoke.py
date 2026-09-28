#!/usr/bin/env python3
"""Disposable real-provider parity probe for fine-grained metrics."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bobi.brain.base import AssistantText, BrainUsage, TurnResult
from bobi.brain.claude import ClaudeBrain
from bobi.brain.codex import CodexBrain, _spawn_codex
from bobi.brain.codex_config import codex_home
from bobi.chat_history import claude_projects_dirs
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
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "provider-probe":
        provider_probe(args)


if __name__ == "__main__":
    main()
