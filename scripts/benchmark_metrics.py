#!/usr/bin/env python3
"""Reproducible Phase 0 metrics producer, collector, and contention probes."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import multiprocessing
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bobi.metrics.collector import MetricsCollector, rebuild_database
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.runtime import MetricsRuntime
from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.metrics.events import MetricsEvent
from bobi.metrics.producer import MetricsProducer
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect, integrity_check, logical_snapshot


def percentile(values: list[int], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * quantile))
    return ordered[index] / 1000


def metadata() -> dict[str, object]:
    def version(*argv: str) -> str | None:
        try:
            return subprocess.run(
                argv, check=True, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    try:
        sdk_version = importlib.metadata.version("claude-agent-sdk")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = None
    return {
        "host": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "cpu_count": os.cpu_count(),
        "claude_agent_sdk": sdk_version,
        "claude_code": version("claude", "--version"),
        "codex_cli": version("codex", "--version"),
    }


def producer_worker(args: tuple[int, str]) -> dict[str, object]:
    count, segment = args
    producer = MetricsProducer(segment, capacity=count + 1)
    event = MetricsEvent(
        event_type="session.recorded",
        producer_id=f"benchmark-{os.getpid()}",
        producer_sequence=0,
        source="benchmark",
        payload={"session_id": "benchmark"},
    )
    producer.start()
    samples: list[int] = []
    stride = max(1, count // 10_000)
    start = time.perf_counter_ns()
    for index in range(count):
        before = time.perf_counter_ns()
        accepted = producer.try_emit(event)
        elapsed = time.perf_counter_ns() - before
        if index % stride == 0:
            samples.append(elapsed)
        if not accepted:
            raise RuntimeError("producer benchmark queue unexpectedly overflowed")
    total = time.perf_counter_ns() - start
    if not producer.close(timeout=120):
        raise RuntimeError("producer spool writer did not drain within 120 seconds")
    if producer.writer_errors:
        raise RuntimeError(
            f"producer spool writer reported {producer.writer_errors} errors"
        )
    return {"events": count, "elapsed_ns": total, "samples_ns": samples}


def benchmark_producer(args: argparse.Namespace) -> dict[str, object]:
    process_counts = [int(value) for value in args.processes.split(",")]
    runs = []
    with tempfile.TemporaryDirectory(prefix="bobi-metrics-producer-") as root:
        for process_count in process_counts:
            per_process = [args.events // process_count] * process_count
            for index in range(args.events % process_count):
                per_process[index] += 1
            work = [
                (count, str(Path(root) / f"{process_count}-{index}.telemetry"))
                for index, count in enumerate(per_process)
            ]
            started = time.perf_counter()
            if process_count == 1:
                results = [producer_worker(work[0])]
            else:
                with multiprocessing.get_context("spawn").Pool(process_count) as pool:
                    results = pool.map(producer_worker, work)
            wall = time.perf_counter() - started
            samples = [
                value
                for result in results
                for value in result["samples_ns"]
            ]
            run = {
                "processes": process_count,
                "events": sum(int(result["events"]) for result in results),
                "wall_seconds": wall,
                "events_per_second": args.events / wall,
                "enqueue_us": {
                    "p50": percentile(samples, 0.50),
                    "p95": percentile(samples, 0.95),
                    "p99": percentile(samples, 0.99),
                },
            }
            runs.append(run)
            if run["enqueue_us"]["p99"] > args.assert_p99_us:
                raise SystemExit(
                    f"producer p99 {run['enqueue_us']['p99']:.3f} us exceeds "
                    f"{args.assert_p99_us:.3f} us"
                )
    return {"benchmark": "producer", "metadata": metadata(), "runs": runs}


def _benchmark_event(sequence: int) -> MetricsEvent:
    return MetricsEvent(
        event_type="session.recorded",
        producer_id="collector-benchmark",
        producer_sequence=sequence,
        source="benchmark",
        payload={
            "session_id": f"session-{sequence}",
            "session_name": "benchmark",
            "brain": "stub",
            "provider": "stub",
            "started_at_us": sequence + 1,
            "status": "completed",
        },
        session_id=f"session-{sequence}",
    )


def benchmark_collector(args: argparse.Namespace) -> dict[str, object]:
    events = args.peak_events_per_second * args.burst_multiple
    with tempfile.TemporaryDirectory(prefix="bobi-metrics-collector-") as root:
        root_path = Path(root)
        segment = root_path / "burst.telemetry"
        with SpoolWriter(segment) as writer:
            for sequence in range(events):
                writer.append(_benchmark_event(sequence))
        db = root_path / "metrics.db"
        started = time.perf_counter()
        result = MetricsCollector(db).collect_segment(segment)
        elapsed = time.perf_counter() - started
    throughput = events / elapsed
    report = {
        "benchmark": "collector",
        "metadata": metadata(),
        "events": events,
        "elapsed_seconds": elapsed,
        "events_per_second": throughput,
        "production_peak_events_per_second": args.peak_events_per_second,
        "throughput_multiple": throughput / args.peak_events_per_second,
        "collector": result,
    }
    if elapsed > args.assert_drain_seconds:
        raise SystemExit(
            f"collector drain {elapsed:.3f}s exceeds {args.assert_drain_seconds:.3f}s"
        )
    if throughput < args.peak_events_per_second * 10:
        raise SystemExit("collector throughput is below 10x the supplied peak")
    return report


def _rebuild_events(turn_index: int) -> list[MetricsEvent]:
    session_id = f"session-{turn_index}"
    turn_id = f"turn-{turn_index}"
    first_sequence = turn_index * 3
    return [
        MetricsEvent(
            event_type="session.recorded",
            producer_id="rebuild-benchmark",
            producer_sequence=first_sequence,
            source="benchmark",
            session_id=session_id,
            payload={
                "session_id": session_id,
                "session_name": "benchmark",
                "brain": "stub",
                "provider": "stub",
                "started_at_us": first_sequence + 1,
                "status": "completed",
            },
        ),
        MetricsEvent(
            event_type="turn.recorded",
            producer_id="rebuild-benchmark",
            producer_sequence=first_sequence + 1,
            source="benchmark",
            session_id=session_id,
            turn_id=turn_id,
            payload={
                "turn_id": turn_id,
                "session_id": session_id,
                "turn_index": 1,
                "trigger_kind": "benchmark",
                "is_user_initiated": 1,
                "started_at_us": first_sequence + 2,
                "status": "completed",
            },
        ),
        MetricsEvent(
            event_type="usage.recorded",
            producer_id="rebuild-benchmark",
            producer_sequence=first_sequence + 2,
            source="benchmark",
            session_id=session_id,
            turn_id=turn_id,
            payload={
                "measurement_id": f"usage-{turn_index}",
                "scope": "turn",
                "turn_id": turn_id,
                "provider": "stub",
                "model": "stub-model",
                "measurement_source": "provider_stream",
                "is_estimated": 0,
                "token_semantics_version": 1,
                "input_tokens": 10 + turn_index,
                "uncached_input_tokens": 10 + turn_index,
                "output_tokens": 2,
                "observed_at_us": first_sequence + 3,
            },
        ),
    ]


def benchmark_rebuild(args: argparse.Namespace) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="bobi-metrics-rebuild-") as root:
        root_path = Path(root)
        segment = root_path / "source.telemetry"
        with SpoolWriter(segment) as writer:
            for turn_index in range(args.turns):
                for event in _rebuild_events(turn_index):
                    writer.append(event)
        source_path = root_path / "source.db"
        target_path = root_path / "rebuilt.db"
        collector = MetricsCollector(source_path)
        first = collector.collect_segment(segment)
        replay = collector.collect_segment(segment)
        source = connect(source_path, readonly=True)
        try:
            before = logical_snapshot(source)
            source_integrity = integrity_check(source)
        finally:
            source.close()
        started = time.perf_counter()
        after = rebuild_database(source_path, target_path)
        elapsed = time.perf_counter() - started
        target = connect(target_path, readonly=True)
        try:
            target_integrity = integrity_check(target)
            reopened = logical_snapshot(target)
        finally:
            target.close()
    snapshot_match = before == after == reopened
    if replay["staged"] != 0:
        raise SystemExit("collector replay created duplicate logical events")
    if source_integrity != "ok" or target_integrity != "ok":
        raise SystemExit("source or rebuilt database failed integrity_check")
    if not snapshot_match:
        raise SystemExit("rebuilt row counts or aggregate checksums differ")
    return {
        "benchmark": "rebuild",
        "metadata": metadata(),
        "turns": args.turns,
        "events": args.turns * 3,
        "elapsed_seconds": elapsed,
        "source_integrity": source_integrity,
        "target_integrity": target_integrity,
        "replay_duplicates": replay["staged"],
        "snapshot_match": snapshot_match,
        "snapshot": before,
        "collector": first,
    }


def contention_worker(args: tuple[str, int]) -> tuple[int, int]:
    path, writes = args
    conn = sqlite3.connect(path, timeout=0)
    succeeded = locked = 0
    try:
        for _ in range(writes):
            try:
                conn.execute("INSERT INTO writes DEFAULT VALUES")
                conn.commit()
                succeeded += 1
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" not in str(exc).lower():
                    raise
                locked += 1
    finally:
        conn.close()
    return succeeded, locked


def benchmark_contention(args: argparse.Namespace) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="bobi-metrics-contention-") as root:
        db = str(Path(root) / "direct.db")
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE writes(id INTEGER PRIMARY KEY)")
        conn.close()
        work = [(db, args.writes_per_process)] * args.processes
        with multiprocessing.get_context("spawn").Pool(args.processes) as pool:
            results = pool.map(contention_worker, work)
    succeeded = sum(item[0] for item in results)
    locked = sum(item[1] for item in results)
    return {
        "benchmark": "direct_sqlite_contention",
        "metadata": metadata(),
        "processes": args.processes,
        "attempted": args.processes * args.writes_per_process,
        "succeeded": succeeded,
        "database_locked": locked,
    }


def _turn_replay_mode(
    root: Path,
    *,
    mode: str,
    turns: int,
    turn_work_ms: float,
) -> dict[str, object]:
    runtime_mode = "disabled" if mode == "telemetry-off" else mode
    collector = MetricsCollectorService(root, poll_interval=0.005) if mode == "full" else None
    if collector is not None:
        collector.start()
    runtime = MetricsRuntime(root, mode=runtime_mode, capacity=max(8192, turns * 8))
    samples_ns: list[int] = []
    try:
        for index in range(turns):
            started_ns = time.perf_counter_ns()
            observation = runtime.begin_turn(
                "benchmark-session",
                provider="benchmark",
                brain="benchmark",
                trigger_kind="benchmark",
                trigger_id=str(index),
                model_requested="benchmark-model",
            )
            if turn_work_ms:
                time.sleep(turn_work_ms / 1000)
            usage = BrainUsage(
                model="benchmark-model",
                provider_event_id=f"provider-{index}",
                input_tokens=100,
                uncached_input_tokens=100,
                output_tokens=20,
                raw_usage={"input_tokens": 100, "output_tokens": 20},
            )
            observation.record_result(TurnResult(
                session_id="benchmark-provider-session",
                provider_turn_id=f"provider-{index}",
                usage=[usage],
                invocations=[BrainInvocation(
                    provider_event_id=f"provider-{index}",
                    model="benchmark-model",
                    usage=usage,
                )],
            ))
            observation.finish(status="completed")
            samples_ns.append(time.perf_counter_ns() - started_ns)
    finally:
        closed = runtime.close(timeout=30)
        if collector is not None:
            stopped = collector.stop(timeout=30)
        else:
            stopped = True
    if not closed or not stopped:
        raise RuntimeError(f"{mode} replay did not stop cleanly")
    health = runtime.health()
    if health.get("telemetry_events_dropped") or health.get("writer_errors"):
        raise RuntimeError(f"{mode} replay lost telemetry: {health}")
    result: dict[str, object] = {
        "mode": mode,
        "turns": turns,
        "turn_work_ms": turn_work_ms,
        "mean_ms": sum(samples_ns) / len(samples_ns) / 1_000_000,
        "latency_ms": {
            "p50": percentile(samples_ns, 0.50) / 1000,
            "p95": percentile(samples_ns, 0.95) / 1000,
            "p99": percentile(samples_ns, 0.99) / 1000,
        },
        "producer_health": health,
    }
    if collector is not None:
        conn = connect(collector.db_path, readonly=True)
        try:
            result["projected_turns"] = int(
                conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            )
        finally:
            conn.close()
        result["collector_health"] = collector.health()
        if result["projected_turns"] != turns:
            raise RuntimeError(
                f"full replay projected {result['projected_turns']} of {turns} turns"
            )
    return result


def benchmark_turn_replay(args: argparse.Namespace) -> dict[str, object]:
    modes = [value.strip() for value in args.compare.split(",") if value.strip()]
    allowed = {"telemetry-off", "shadow", "full"}
    if not modes or any(mode not in allowed for mode in modes):
        raise SystemExit(f"--compare must use: {','.join(sorted(allowed))}")
    if "telemetry-off" not in modes:
        raise SystemExit("--compare must include telemetry-off as the baseline")
    trials: dict[str, list[dict[str, object]]] = {mode: [] for mode in modes}
    with tempfile.TemporaryDirectory(prefix="bobi-metrics-turn-replay-") as root:
        for round_index in range(args.rounds):
            rotated = modes[round_index % len(modes):] + modes[:round_index % len(modes)]
            for mode in rotated:
                trial = _turn_replay_mode(
                    Path(root) / f"{round_index}-{mode}",
                    mode=mode,
                    turns=args.turns,
                    turn_work_ms=args.turn_work_ms,
                )
                trial["round"] = round_index + 1
                trials[mode].append(trial)
    runs = []
    for mode in modes:
        mode_trials = trials[mode]
        representative = dict(mode_trials[-1])
        representative["mean_ms"] = statistics.median(
            float(trial["mean_ms"]) for trial in mode_trials
        )
        representative["trials"] = [
            {
                "round": trial["round"],
                "mean_ms": trial["mean_ms"],
                "latency_ms": trial["latency_ms"],
            }
            for trial in mode_trials
        ]
        runs.append(representative)
    baseline_ms = float(next(
        run["mean_ms"] for run in runs if run["mode"] == "telemetry-off"
    ))
    for run in runs:
        regression = max(0.0, (float(run["mean_ms"]) - baseline_ms) / baseline_ms * 100)
        run["regression_percent"] = regression
        if run["mode"] != "telemetry-off" and regression >= args.assert_regression_percent:
            raise SystemExit(
                f"{run['mode']} replay regression {regression:.3f}% exceeds "
                f"{args.assert_regression_percent:.3f}%"
            )
    return {
        "benchmark": "turn_replay",
        "metadata": metadata(),
        "assert_regression_percent": args.assert_regression_percent,
        "rounds": args.rounds,
        "runs": runs,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--json-out", type=Path)
    commands = root.add_subparsers(dest="command", required=True)
    producer = commands.add_parser("producer")
    producer.add_argument("--events", type=int, default=1_000_000)
    producer.add_argument("--processes", default="1,8,32,64")
    producer.add_argument("--assert-p99-us", type=float, default=200)
    collector = commands.add_parser("collector")
    collector.add_argument("--peak-events-per-second", type=int, default=1000)
    collector.add_argument("--burst-multiple", type=int, default=10)
    collector.add_argument("--assert-drain-seconds", type=float, default=60)
    rebuild = commands.add_parser("rebuild")
    rebuild.add_argument("--turns", type=int, default=1000)
    contention = commands.add_parser("contention")
    contention.add_argument("--processes", type=int, default=32)
    contention.add_argument("--writes-per-process", type=int, default=100)
    replay = commands.add_parser("turn-replay")
    replay.add_argument("--compare", default="telemetry-off,shadow,full")
    replay.add_argument("--turns", type=int, default=200)
    replay.add_argument("--turn-work-ms", type=float, default=20.0)
    replay.add_argument("--rounds", type=int, default=3)
    replay.add_argument("--assert-regression-percent", type=float, default=1.0)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "producer":
        report = benchmark_producer(args)
    elif args.command == "collector":
        report = benchmark_collector(args)
    elif args.command == "rebuild":
        report = benchmark_rebuild(args)
    elif args.command == "turn-replay":
        report = benchmark_turn_replay(args)
    else:
        report = benchmark_contention(args)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n")


if __name__ == "__main__":
    main()
