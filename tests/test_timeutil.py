"""bobi.timeutil — the one wall-clock timestamp convention (aware UTC)."""

from datetime import datetime, timezone

import pytest

from bobi.timeutil import now_iso, parse_iso


def test_now_iso_is_timezone_aware_utc():
    dt = datetime.fromisoformat(now_iso())
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


class TestParseIso:
    """`parse_iso` is the one reader for timestamps `now_iso` writes.

    Reader and writer live together in bobi.timeutil so the accepted format
    has exactly one definition; the monitors package used to carry per-module
    copies of the parser.
    """

    def test_accepts_the_z_suffix_and_an_explicit_offset(self):
        for value in ("2026-07-31T12:00:00Z", "2026-07-31T12:00:00+00:00"):
            dt = parse_iso(value)
            assert dt == datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)

    def test_round_trips_what_now_iso_writes(self):
        assert parse_iso(now_iso()) is not None

    def test_defaults_a_naive_timestamp_to_utc(self):
        dt = parse_iso("2026-07-31T12:00:00")
        assert dt.tzinfo == timezone.utc

    @pytest.mark.parametrize("value", ["", None, "not-a-timestamp"])
    def test_unparseable_reads_as_absent(self, value):
        assert parse_iso(value) is None

    def test_a_non_string_in_state_reads_as_absent_rather_than_raising(self):
        # These strings come back out of JSON state documents, which can hold
        # anything after a torn write or a hand edit. Both callers treat an
        # unreadable timestamp as "no timestamp"; neither can survive an
        # AttributeError escaping the parser.
        assert parse_iso(1753963200) is None
        assert parse_iso({"at": "2026-07-31T12:00:00Z"}) is None

