from argparse import Namespace

from scripts.benchmark_metrics import benchmark_rebuild, benchmark_turn_replay


def test_rebuild_benchmark_proves_replay_and_snapshot_parity():
    report = benchmark_rebuild(Namespace(turns=3))

    assert report["events"] == 9
    assert report["replay_duplicates"] == 0
    assert report["source_integrity"] == "ok"
    assert report["target_integrity"] == "ok"
    assert report["snapshot_match"] is True
    assert report["metadata"]["sqlite"]
    assert report["snapshot"]["counts"]["sessions"] == 3
    assert report["snapshot"]["counts"]["turns"] == 3
    assert report["snapshot"]["counts"]["usage_measurements"] == 3


def test_turn_replay_benchmark_compares_all_runtime_modes():
    report = benchmark_turn_replay(Namespace(
        compare="telemetry-off,shadow,full",
        turns=3,
        turn_work_ms=1.0,
        rounds=1,
        assert_regression_percent=10_000,
    ))

    assert [run["mode"] for run in report["runs"]] == [
        "telemetry-off", "shadow", "full"
    ]
    assert report["runs"][2]["projected_turns"] == 3
    assert report["runs"][2]["collector_health"]["uncommitted_spool_bytes"] == 0
