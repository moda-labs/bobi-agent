"""Qualified out-of-band token estimators for otherwise uncovered turns."""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from bobi.metrics.collector import MetricsCollector
from bobi.metrics.events import MetricsEvent, deterministic_event_id, uuid7
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect

MIN_QUALIFICATION_SAMPLES = 100
MAX_MAPE_PERCENT = 10.0
MAX_P95_APE_PERCENT = 20.0
REGISTRY_VERSION = 1
ESTIMATOR_ALGORITHM_VERSION = "calibrated-bytes-v1"


@dataclass(frozen=True)
class CalibrationSample:
    byte_count: int
    exact_tokens: int


@dataclass(frozen=True)
class EstimatorQualification:
    provider: str
    model_family: str
    estimator_name: str
    estimator_version: str
    tokens_per_byte: float
    calibration_samples: int
    held_out_samples: int
    mape_percent: float | None
    p95_ape_percent: float | None
    qualified: bool

    def matches(self, provider: str, model: str) -> bool:
        return (
            self.qualified
            and provider == self.provider
            and model.lower().startswith(self.model_family.lower())
        )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1)
    return ordered[max(0, index)]


def qualify_byte_estimator(
    *,
    provider: str,
    model_family: str,
    calibration: Iterable[CalibrationSample],
    held_out: Iterable[CalibrationSample],
    estimator_version: str,
) -> EstimatorQualification:
    """Fit a byte ratio and apply the RFC's held-out qualification gates."""
    calibration = [
        sample for sample in calibration
        if sample.byte_count > 0 and sample.exact_tokens >= 0
    ]
    held_out = [
        sample for sample in held_out
        if sample.byte_count > 0 and sample.exact_tokens > 0
    ]
    ratios = [sample.exact_tokens / sample.byte_count for sample in calibration]
    tokens_per_byte = statistics.median(ratios) if ratios else 0.0
    errors = [
        abs(round(sample.byte_count * tokens_per_byte) - sample.exact_tokens)
        / sample.exact_tokens
        * 100
        for sample in held_out
    ]
    mape = statistics.fmean(errors) if errors else None
    p95 = _percentile(errors, 0.95) if errors else None
    qualified = bool(
        tokens_per_byte > 0
        and len(calibration) >= MIN_QUALIFICATION_SAMPLES
        and len(held_out) >= MIN_QUALIFICATION_SAMPLES
        and mape is not None
        and p95 is not None
        and mape <= MAX_MAPE_PERCENT
        and p95 <= MAX_P95_APE_PERCENT
    )
    return EstimatorQualification(
        provider=provider,
        model_family=model_family,
        estimator_name="calibrated_bytes",
        estimator_version=estimator_version,
        tokens_per_byte=tokens_per_byte,
        calibration_samples=len(calibration),
        held_out_samples=len(held_out),
        mape_percent=mape,
        p95_ape_percent=p95,
        qualified=qualified,
    )


class EstimatorRegistry:
    """Model-family estimator registry; unqualified entries never run."""

    def __init__(self, entries: Iterable[EstimatorQualification] = ()) -> None:
        self.entries = tuple(entry for entry in entries if entry.qualified)

    @classmethod
    def load(cls, path: Path | str) -> "EstimatorRegistry":
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError, TypeError):
            return cls()
        if data.get("registry_version") != REGISTRY_VERSION:
            return cls()
        entries = []
        for raw in data.get("estimators", []):
            try:
                entries.append(EstimatorQualification(**raw))
            except (TypeError, ValueError):
                continue
        return cls(entries)

    def dump(self, path: Path | str) -> None:
        from bobi.fsutil import atomic_write_json

        atomic_write_json(
            path,
            {
                "registry_version": REGISTRY_VERSION,
                "estimators": [asdict(entry) for entry in self.entries],
            },
            sort_keys=True,
        )

    def resolve(self, provider: str, model: str) -> EstimatorQualification | None:
        matches = [
            entry for entry in self.entries if entry.matches(provider, model)
        ]
        if not matches:
            return None
        return max(matches, key=lambda entry: len(entry.model_family))


def _calibration_rows(db_path: Path) -> list[dict[str, object]]:
    conn = connect(db_path, readonly=True)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT t.turn_id,t.prompt_bytes,s.provider,u.model,u.input_tokens "
                "FROM turns AS t JOIN sessions AS s USING(session_id) "
                "JOIN best_usage AS u ON u.turn_id=t.turn_id "
                "WHERE u.scope='turn' AND u.is_estimated=0 "
                "AND t.prompt_bytes>0 AND u.input_tokens>0 "
                "AND 1=(SELECT COUNT(*) FROM best_usage AS ux "
                "WHERE ux.turn_id=t.turn_id AND ux.scope='turn' "
                "AND ux.is_estimated=0)"
            )
        ]
    finally:
        conn.close()


def calibrate_estimators(
    root: Path | str,
    *,
    report_path: Path | str | None = None,
    registry_path: Path | str | None = None,
) -> dict[str, object]:
    """Build a metadata-only qualification report from exact retained turns."""
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    db_path = metrics_root / "metrics.db"
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    rows = _calibration_rows(db_path)
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row["provider"]), str(row["model"])), []
        ).append(row)
    qualifications = []
    dataset_material = []
    for (provider, model), group in sorted(grouped.items()):
        ordered = sorted(
            group,
            key=lambda row: hashlib.sha256(
                f"{row['turn_id']}\0{provider}\0{model}".encode()
            ).digest(),
        )
        split = len(ordered) // 2
        calibration = [
            CalibrationSample(int(row["prompt_bytes"]), int(row["input_tokens"]))
            for row in ordered[:split]
        ]
        held_out = [
            CalibrationSample(int(row["prompt_bytes"]), int(row["input_tokens"]))
            for row in ordered[split:]
        ]
        group_hash = hashlib.sha256(
            json.dumps(
                [
                    [row["turn_id"], row["prompt_bytes"], row["input_tokens"]]
                    for row in ordered
                ],
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        version = f"{ESTIMATOR_ALGORITHM_VERSION}-{group_hash[:12]}"
        qualification = qualify_byte_estimator(
            provider=provider,
            model_family=model,
            calibration=calibration,
            held_out=held_out,
            estimator_version=version,
        )
        qualifications.append(qualification)
        dataset_material.append([provider, model, group_hash, len(ordered)])
    dataset_sha256 = hashlib.sha256(
        json.dumps(dataset_material, separators=(",", ":")).encode()
    ).hexdigest()
    registry = EstimatorRegistry(qualifications)
    registry_path = Path(registry_path or metrics_root / "estimators.json")
    report_path = Path(
        report_path or metrics_root / "estimator-calibration.json"
    )
    registry.dump(registry_path)
    report = {
        "status": "done",
        "generated_at_us": time.time_ns() // 1000,
        "algorithm_version": ESTIMATOR_ALGORITHM_VERSION,
        "dataset_sha256": dataset_sha256,
        "exact_turns_considered": len(rows),
        "model_groups": len(qualifications),
        "qualified_model_groups": sum(item.qualified for item in qualifications),
        "thresholds": {
            "minimum_calibration_samples": MIN_QUALIFICATION_SAMPLES,
            "minimum_held_out_samples": MIN_QUALIFICATION_SAMPLES,
            "maximum_mape_percent": MAX_MAPE_PERCENT,
            "maximum_p95_ape_percent": MAX_P95_APE_PERCENT,
        },
        "estimators": [asdict(item) for item in qualifications],
        "registry": str(registry_path),
        "report": str(report_path),
    }
    from bobi.fsutil import atomic_write_json

    atomic_write_json(report_path, report, sort_keys=True, fsync=True)
    return report


def _stable_id(prefix: str, *parts: object) -> str:
    material = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()}"


def build_estimate_event(
    db_path: Path | str,
    turn_id: str,
    registry: EstimatorRegistry,
) -> MetricsEvent | None:
    """Return one deterministic estimate event, or None when unsafe/unneeded."""
    conn = connect(db_path, readonly=True)
    try:
        row = conn.execute(
            "SELECT t.turn_id,t.session_id,t.prompt_bytes,t.started_at_us,"
            "t.ended_at_us,s.provider,"
            "(SELECT i.model_selected FROM llm_invocations AS i "
            " WHERE i.turn_id=t.turn_id ORDER BY i.invocation_index DESC LIMIT 1) "
            "AS model "
            "FROM turns AS t JOIN sessions AS s USING(session_id) "
            "WHERE t.turn_id=?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        if conn.execute(
            "SELECT 1 FROM usage_measurements WHERE turn_id=? AND scope='turn' "
            "AND is_estimated=0 LIMIT 1",
            (turn_id,),
        ).fetchone():
            return None
        byte_count = row["prompt_bytes"]
        model = str(row["model"] or "")
        qualification = registry.resolve(str(row["provider"]), model)
        if not qualification or not isinstance(byte_count, int) or byte_count <= 0:
            return None
        measurement_id = _stable_id(
            "use",
            turn_id,
            "turn",
            model,
            qualification.estimator_name,
            qualification.estimator_version,
        )
        if conn.execute(
            "SELECT 1 FROM usage_measurements WHERE measurement_id=?",
            (measurement_id,),
        ).fetchone():
            return None
        estimated_input = max(0, round(byte_count * qualification.tokens_per_byte))
        observed_at_us = int(row["ended_at_us"] or time.time_ns() // 1000)
        provider = str(row["provider"])
        return MetricsEvent(
            event_type="usage.recorded",
            producer_id=f"estimator-{provider}-{turn_id}",
            producer_sequence=0,
            source="calibrated_estimator",
            payload={
                "measurement_id": measurement_id,
                "scope": "turn",
                "turn_id": turn_id,
                "invocation_id": None,
                "provider": provider,
                "model": model,
                "provider_event_id": None,
                "measurement_source": "calibrated_estimator",
                "is_estimated": 1,
                "estimator_name": qualification.estimator_name,
                "estimator_version": qualification.estimator_version,
                "token_semantics_version": 1,
                "input_tokens": estimated_input,
                "uncached_input_tokens": None,
                "cache_read_input_tokens": None,
                "cache_write_input_tokens": None,
                "cache_write_5m_input_tokens": None,
                "cache_write_1h_input_tokens": None,
                "cache_write_unknown_ttl_input_tokens": None,
                "cache_write_breakdown_complete": 0,
                "output_tokens": None,
                "reasoning_output_tokens": None,
                "observed_at_us": observed_at_us,
                "raw_usage_json": None,
                "supersedes_measurement_id": None,
            },
            event_id=deterministic_event_id(
                provider,
                f"{turn_id}:estimate:{qualification.estimator_name}:"
                f"{qualification.estimator_version}",
            ),
            emitted_at_us=observed_at_us,
            session_id=str(row["session_id"]),
            turn_id=turn_id,
        )
    finally:
        conn.close()


def estimate_turn(
    root: Path | str,
    turn_id: str,
    *,
    registry: EstimatorRegistry | None = None,
    collect: bool = True,
) -> dict[str, object]:
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    db_path = metrics_root / "metrics.db"
    registry = registry or EstimatorRegistry.load(metrics_root / "estimators.json")
    event = build_estimate_event(db_path, turn_id, registry)
    if event is None:
        return {"status": "skipped", "turn_id": turn_id, "events_emitted": 0}
    segment = (
        metrics_root / "spool" / f"estimator-{os.getpid()}-{uuid7()}"
        / f"{time.time_ns()}-{uuid7()}.telemetry"
    )
    with SpoolWriter(segment, sync_every_frame=True) as writer:
        writer.append(event)
    result = {"role": "deferred"}
    if collect:
        result = MetricsCollector(db_path).collect_segment(segment)
    return {
        "status": "done" if collect and result["role"] == "active" else "pending",
        "turn_id": turn_id,
        "events_emitted": 1,
        "collector_role": result["role"],
    }


def estimate_missing(
    root: Path | str,
    *,
    registry: EstimatorRegistry | None = None,
    collect: bool = True,
    terminal_only: bool = False,
) -> dict[str, object]:
    root = Path(root).resolve()
    db_path = root / "state" / "metrics" / "metrics.db"
    registry = registry or EstimatorRegistry.load(
        root / "state" / "metrics" / "estimators.json"
    )
    conn = connect(db_path, readonly=True)
    try:
        turn_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT t.turn_id FROM turns AS t WHERE NOT EXISTS ("
                "SELECT 1 FROM usage_measurements AS u WHERE u.turn_id=t.turn_id "
                "AND u.scope='turn') "
                + ("AND t.ended_at_us IS NOT NULL " if terminal_only else "")
                + "ORDER BY t.started_at_us"
            )
        ]
    finally:
        conn.close()
    results = [
        estimate_turn(root, turn_id, registry=registry, collect=collect)
        for turn_id in turn_ids
    ]
    return {
        "turns_considered": len(turn_ids),
        "turns_estimated": sum(result["events_emitted"] for result in results),
        "results": results,
    }
