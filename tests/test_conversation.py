"""Tests for conversation references (#618)."""

import pytest

from bobi.conversation import Conversation, build_conversation, parse_conversation

# GOLDEN VECTOR - keep identical to event-server/test/conversation.spec.ts.
# The grammar is hand-mirrored across TS and Python; this shared vector turns
# drift into a test failure (same pattern as the bubble-signing parity vector).
GOLDEN_REF = "slack:T0GOLD:channel:C0GOLD:thread:1718000000.000042"
GOLDEN_PARSED = Conversation("slack", "T0GOLD", "channel", "C0GOLD", "1718000000.000042")


class TestGoldenVector:

    def test_parses_and_rebuilds_the_golden_ref(self):
        assert parse_conversation(GOLDEN_REF) == GOLDEN_PARSED
        assert build_conversation(GOLDEN_PARSED) == GOLDEN_REF


class TestParseConversation:

    def test_round_trip_without_thread(self):
        conv = Conversation("slack", "T0123", "dm", "D0789")
        ref = build_conversation(conv)
        assert ref == "slack:T0123:dm:D0789"
        assert parse_conversation(ref) == conv

    def test_round_trip_with_thread(self):
        conv = Conversation("slack", "T0123", "channel", "C0456", "1718000000.123456")
        ref = build_conversation(conv)
        assert ref == "slack:T0123:channel:C0456:thread:1718000000.123456"
        assert parse_conversation(ref) == conv

    def test_whatsapp_shaped_ref(self):
        conv = parse_conversation("whatsapp:747556541:dm:15551234567")
        assert conv == Conversation("whatsapp", "747556541", "dm", "15551234567")

    @pytest.mark.parametrize("ref", [
        "",
        "slack:T0123:dm",                          # too few segments
        "slack:T0123:dm:D0789:extra",              # five segments
        "slack:T0123:dm:D0789:thread:1.2:extra",   # too many segments
        "slack:T0123:dm:D0789:topic:1.2",          # unknown trailer keyword
        "slack:T0123:channel:",                    # empty chat id
        "slack::channel:C1",                       # empty scope
        "slack:T0123:mpim:C1",                     # unknown chat type
    ])
    def test_rejects_malformed_refs(self, ref):
        assert parse_conversation(ref) is None

    def test_rejects_non_string_ref(self):
        assert parse_conversation(["slack", "T1"]) is None
        assert parse_conversation(42) is None


class TestBuildConversation:

    @pytest.mark.parametrize("conv", [
        Conversation("slack", "T1", "channel", "a:thread:b"),
        Conversation("slack", "T1", "channel", "C1", "1:2"),
        Conversation("slack", "", "dm", "D1"),
    ])
    def test_rejects_delimiter_bearing_or_empty_segments(self, conv):
        with pytest.raises(ValueError, match="invalid conversation segment"):
            build_conversation(conv)
