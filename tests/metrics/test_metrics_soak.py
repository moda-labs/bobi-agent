from scripts.metrics_soak import parser, run_soak


def test_soak_parser_matches_phase2_command_contract(tmp_path):
    args = parser().parse_args([
        "--hours", "72", "--kill-workers", "--replay",
        "--json-out", str(tmp_path / "soak.json"),
    ])

    assert args.hours == 72
    assert args.kill_workers is True
    assert args.replay is True


def test_short_soak_survives_process_kills_and_rebuilds(tmp_path):
    result = run_soak(
        tmp_path,
        duration_seconds=1.0,
        workers=2,
        turns_per_second=25,
        kill_workers=True,
        kill_interval=0.25,
        replay=True,
        poll_interval=0.02,
    )

    assert result["status"] == "passed"
    assert result["planned_process_kills"]
    assert result["checks"]["accepted_event_fingerprint_match"] is True
    assert result["checks"]["exact_token_parity"] is True
    assert result["replay"]["snapshot_match"] is True
