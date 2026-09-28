from argparse import Namespace

from scripts.benchmark_metrics import benchmark_rebuild


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
