#!/usr/bin/env python3
"""Long-running framed-spool, collector, process-kill, and replay soak."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bobi.fsutil import atomic_write_json
from bobi.metrics.collector import MetricsCollectorService, rebuild_database
from bobi.metrics.events import MetricsEvent
from bobi.metrics.spool import SpoolCorruptionError, SpoolWriter, iter_frames
from bobi.metrics.store import connect, integrity_check, logical_snapshot

MASK_256 = (1 << 256) - 1


def _event_fingerprint_update(
    fingerprint: tuple[int, int, int], event_id: str
) -> tuple[int, int, int]:
    count, xor_value, sum_value = fingerprint
    value = int.from_bytes(hashlib.sha256(event_id.encode()).digest(), "big")
    return count + 1, xor_value ^ value, (sum_value + value) & MASK_256


def _worker(root: Path, worker_id: int, generation: int, rate: float) -> None:
    producer_id = f"soak-{worker_id}-{generation}-{os.getpid()}"
    session_id = f"session-{producer_id}"
    segment = (
        root / "state" / "metrics" / "spool" / producer_id
        / f"{time.time_ns()}.telemetry"
    )
    interval = 1.0 / rate if rate > 0 else 0.0
    sequence = 0
    with SpoolWriter(segment) as writer:
        now_us = time.time_ns() // 1000
        writer.append(MetricsEvent(
            event_type="session.recorded",
            producer_id=producer_id,
            producer_sequence=sequence,
            source="metrics_soak",
            session_id=session_id,
            payload={
                "session_id": session_id,
                "session_name": producer_id,
                "brain": "soak",
                "provider": "soak",
                "started_at_us": now_us,
                "status": "running",
            },
        ))
        sequence += 1
        turn_index = 0
        while True:
            started = time.time_ns() // 1000
            turn_id = f"turn-{producer_id}-{turn_index}"
            input_tokens = 100 + turn_index % 10
            output_tokens = 20 + turn_index % 5
            writer.append(MetricsEvent(
                event_type="turn.recorded",
                producer_id=producer_id,
                producer_sequence=sequence,
                source="metrics_soak",
                session_id=session_id,
                turn_id=turn_id,
                payload={
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "turn_index": turn_index + 1,
                    "trigger_kind": "soak",
                    "is_user_initiated": 1,
                    "prompt_bytes": input_tokens * 4,
                    "started_at_us": started,
                    "ended_at_us": started + 1,
                    "wall_duration_ms": 0.001,
                    "status": "completed",
                },
            ))
            sequence += 1
            writer.append(MetricsEvent(
                event_type="usage.recorded",
                producer_id=producer_id,
                producer_sequence=sequence,
                source="metrics_soak",
                session_id=session_id,
                turn_id=turn_id,
                payload={
                    "measurement_id": f"usage-{producer_id}-{turn_index}",
                    "scope": "turn",
                    "turn_id": turn_id,
                    "invocation_id": None,
                    "provider": "soak",
                    "model": "soak-model",
                    "provider_event_id": f"provider-{producer_id}-{turn_index}",
                    "measurement_source": "provider_stream",
                    "is_estimated": 0,
                    "estimator_name": None,
                    "estimator_version": None,
                    "token_semantics_version": 1,
                    "input_tokens": input_tokens,
                    "uncached_input_tokens": input_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "cache_write_5m_input_tokens": 0,
                    "cache_write_1h_input_tokens": 0,
                    "cache_write_unknown_ttl_input_tokens": 0,
                    "cache_write_breakdown_complete": 1,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                    "observed_at_us": started + 1,
                    "raw_usage_json": None,
                    "supersedes_measurement_id": None,
                },
            ))
            sequence += 1
            turn_index += 1
            writer.flush_if_due()
            if interval:
                time.sleep(interval)


def _start_worker(
    root: Path, worker_id: int, generation: int, rate: float
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker-id", str(worker_id),
            "--generation", str(generation),
            "--root", str(root),
            "--turns-per-second", str(rate),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_worker(process: subprocess.Popen, *, kill: bool) -> int:
    if process.poll() is None:
        process.kill() if kill else process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def _spool_inventory(spool_root: Path) -> dict[str, object]:
    fingerprint = (0, 0, 0)
    usage_count = input_tokens = output_tokens = 0
    corrupt_segments = incomplete_tail_segments = 0
    segment_count = 0
    for segment in sorted(spool_root.glob("*/*.telemetry")):
        segment_count += 1
        last_offset = 0
        try:
            for frame in iter_frames(segment):
                last_offset = frame.next_offset
                event = frame.event
                fingerprint = _event_fingerprint_update(
                    fingerprint, event.event_id
                )
                if event.event_type == "usage.recorded":
                    usage_count += 1
                    input_tokens += int(event.payload.get("input_tokens") or 0)
                    output_tokens += int(event.payload.get("output_tokens") or 0)
        except SpoolCorruptionError:
            corrupt_segments += 1
        try:
            incomplete_tail_segments += int(last_offset < segment.stat().st_size)
        except OSError:
            corrupt_segments += 1
    return {
        "segments": segment_count,
        "complete_events": fingerprint[0],
        "event_fingerprint_xor": f"{fingerprint[1]:064x}",
        "event_fingerprint_sum": f"{fingerprint[2]:064x}",
        "usage_measurements": usage_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "corrupt_segments": corrupt_segments,
        "incomplete_tail_segments": incomplete_tail_segments,
    }


def _database_inventory(db_path: Path) -> dict[str, object]:
    conn = connect(db_path, readonly=True)
    try:
        fingerprint = (0, 0, 0)
        for row in conn.execute("SELECT event_id FROM raw_events"):
            fingerprint = _event_fingerprint_update(fingerprint, str(row[0]))
        usage = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(input_tokens),0),"
            "COALESCE(SUM(output_tokens),0) FROM usage_measurements "
            "WHERE is_estimated=0"
        ).fetchone()
        duplicate_usage = int(conn.execute(
            "SELECT COUNT(*) FROM (SELECT turn_id,scope,provider,model,"
            "measurement_source,COUNT(*) AS n FROM usage_measurements "
            "GROUP BY 1,2,3,4,5 HAVING n>1)"
        ).fetchone()[0])
        projection = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT projection_state,COUNT(*) FROM raw_events GROUP BY 1"
            )
        }
        return {
            "integrity": integrity_check(conn),
            "raw_events": fingerprint[0],
            "event_fingerprint_xor": f"{fingerprint[1]:064x}",
            "event_fingerprint_sum": f"{fingerprint[2]:064x}",
            "usage_measurements": int(usage[0]),
            "input_tokens": int(usage[1]),
            "output_tokens": int(usage[2]),
            "duplicate_logical_measurements": duplicate_usage,
            "projection": projection,
            "snapshot": logical_snapshot(conn),
        }
    finally:
        conn.close()


def run_soak(
    root: Path,
    *,
    duration_seconds: float,
    workers: int,
    turns_per_second: float,
    kill_workers: bool,
    kill_interval: float,
    replay: bool,
    poll_interval: float = 0.1,
) -> dict[str, object]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "state" / "metrics" / "metrics.db"
    collector = MetricsCollectorService(root, reconcile_interval=10**12)
    generations = [0 for _ in range(workers)]
    processes = [
        _start_worker(root, worker_id, 0, turns_per_second)
        for worker_id in range(workers)
    ]
    planned_kills = []
    worker_failures = []
    started = time.monotonic()
    deadline = started + max(0.1, duration_seconds)
    next_kill = started + max(0.1, kill_interval)
    kill_index = 0
    try:
        while time.monotonic() < deadline:
            collector.collect_once()
            for worker_id, process in enumerate(processes):
                returncode = process.poll()
                if returncode is None:
                    continue
                worker_failures.append({
                    "worker_id": worker_id,
                    "generation": generations[worker_id],
                    "returncode": returncode,
                })
                generations[worker_id] += 1
                processes[worker_id] = _start_worker(
                    root, worker_id, generations[worker_id], turns_per_second
                )
            if kill_workers and time.monotonic() >= next_kill:
                worker_id = kill_index % workers
                process = processes[worker_id]
                _stop_worker(process, kill=True)
                planned_kills.append({
                    "worker_id": worker_id,
                    "generation": generations[worker_id],
                    "at_seconds": round(time.monotonic() - started, 3),
                })
                generations[worker_id] += 1
                processes[worker_id] = _start_worker(
                    root, worker_id, generations[worker_id], turns_per_second
                )
                kill_index += 1
                next_kill = time.monotonic() + max(0.1, kill_interval)
            time.sleep(poll_interval)
    finally:
        for process in processes:
            _stop_worker(process, kill=kill_workers)

    idle_cycles = 0
    for _ in range(100):
        result = collector.collect_once()
        if int(result["staged"]) == 0 and int(result["projected"]) == 0:
            idle_cycles += 1
            if idle_cycles >= 2:
                break
        else:
            idle_cycles = 0
        time.sleep(0.02)

    spool = _spool_inventory(root / "state" / "metrics" / "spool")
    database = _database_inventory(db_path)
    replay_result = {"enabled": replay, "snapshot_match": None}
    if replay:
        replay_path = db_path.with_name("metrics-replay.db")
        replay_path.unlink(missing_ok=True)
        replay_snapshot = rebuild_database(db_path, replay_path)
        replay_conn = connect(replay_path, readonly=True)
        try:
            replay_result = {
                "enabled": True,
                "integrity": integrity_check(replay_conn),
                "snapshot_match": (
                    replay_snapshot == database["snapshot"]
                    and logical_snapshot(replay_conn) == database["snapshot"]
                ),
            }
        finally:
            replay_conn.close()
    fingerprint_match = (
        spool["complete_events"] == database["raw_events"]
        and spool["event_fingerprint_xor"] == database["event_fingerprint_xor"]
        and spool["event_fingerprint_sum"] == database["event_fingerprint_sum"]
    )
    token_parity = (
        spool["usage_measurements"] == database["usage_measurements"]
        and spool["input_tokens"] == database["input_tokens"]
        and spool["output_tokens"] == database["output_tokens"]
    )
    passed = all((
        database["integrity"] == "ok",
        spool["corrupt_segments"] == 0,
        fingerprint_match,
        token_parity,
        database["duplicate_logical_measurements"] == 0,
        not worker_failures,
        not replay or replay_result["snapshot_match"] is True,
    ))
    return {
        "status": "passed" if passed else "failed",
        "root": str(root),
        "configured_duration_seconds": duration_seconds,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "workers": workers,
        "turns_per_second_per_worker": turns_per_second,
        "planned_process_kills": planned_kills,
        "unexpected_worker_failures": worker_failures,
        "spool": spool,
        "database": database,
        "replay": replay_result,
        "checks": {
            "accepted_event_fingerprint_match": fingerprint_match,
            "exact_token_parity": token_parity,
            "zero_duplicate_logical_measurements": (
                database["duplicate_logical_measurements"] == 0
            ),
            "zero_database_corruption": database["integrity"] == "ok",
            "zero_unexpected_worker_failures": not worker_failures,
        },
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--hours", type=float, default=72.0)
    root.add_argument("--workers", type=int, default=8)
    root.add_argument("--turns-per-second", type=float, default=1.0)
    root.add_argument("--kill-workers", action="store_true")
    root.add_argument("--kill-interval", type=float, default=300.0)
    root.add_argument("--replay", action="store_true")
    root.add_argument("--root", type=Path)
    root.add_argument("--json-out", type=Path)
    root.add_argument("--worker-id", type=int, help=argparse.SUPPRESS)
    root.add_argument("--generation", type=int, default=0, help=argparse.SUPPRESS)
    return root


def main() -> None:
    args = parser().parse_args()
    root = args.root or Path(tempfile.mkdtemp(prefix="bobi-metrics-soak-"))
    if args.worker_id is not None:
        _worker(root, args.worker_id, args.generation, args.turns_per_second)
        return
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    result = run_soak(
        root,
        duration_seconds=args.hours * 3600,
        workers=args.workers,
        turns_per_second=args.turns_per_second,
        kill_workers=args.kill_workers,
        kill_interval=args.kill_interval,
        replay=args.replay,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.json_out, result, sort_keys=True, fsync=True)
    print(rendered, end="")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
