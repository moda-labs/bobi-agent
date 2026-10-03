import json

from click.testing import CliRunner

from bobi.cli import metrics
from bobi.metrics.store import connect, migrate


def test_metrics_status_reports_database_and_reconciliation(monkeypatch, tmp_path):
    db = tmp_path / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    try:
        migrate(conn)
    finally:
        conn.close()
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setenv("BOBI_METRICS_MODE", "enabled")

    result = CliRunner().invoke(metrics, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "enabled"
    assert payload["database"]["ready"] is True
    assert payload["database"]["integrity"] == "ok"
    assert payload["reconciliation"]["uncovered_turns"] == 0
    assert {"maintenance", "retention", "backup"}.isdisjoint(payload)


def test_metrics_reconcile_invokes_one_turn(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bobi.metrics.reconcile.reconcile_turn",
        lambda root, turn_id, wait, timeout: calls.append(
            (root, turn_id, wait, timeout)
        ) or {
            "status": "done",
            "exact_measurements_recovered": 1,
            "turn_id": turn_id,
        },
    )

    result = CliRunner().invoke(
        metrics,
        ["reconcile", "--turn-id", "turn-1", "--wait", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, "turn-1", True, 60.0)]
    assert json.loads(result.output)["turn_id"] == "turn-1"


def test_metrics_calibrate_reports_qualified_groups(monkeypatch, tmp_path):
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bobi.metrics.estimate.calibrate_estimators",
        lambda root: {
            "status": "done",
            "model_groups": 2,
            "qualified_model_groups": 1,
        },
    )

    result = CliRunner().invoke(metrics, ["calibrate", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["qualified_model_groups"] == 1


def test_metrics_commands_are_operational_only():
    assert set(metrics.commands) == {"status", "reconcile", "calibrate"}
