"""Unit tests for the shared transient-error classifier (MDS-65 §4.3).

This module single-sources "what counts as transient" so the persistent-session
turn loop (session.py) and the sub-agent spawn/executor path (subagent.py) never
drift. The classifier is pure — no session state — so it is tested directly.
"""

import pytest

from bobi.transient import (
    TRANSIENT_API_STATUSES,
    TURN_RETRY_BASE,
    TURN_RETRY_MAX_ATTEMPTS,
    is_transient_api_error,
)


class TestTransientStatuses:
    @pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 529])
    def test_known_transient_statuses_are_transient(self, status):
        assert is_transient_api_error(status) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_concrete_non_transient_statuses_are_not(self, status):
        # A concrete non-transient status wins over any text fingerprint.
        assert is_transient_api_error(status) is False
        assert is_transient_api_error(status, "overloaded 529") is False

    def test_status_set_matches_444_definition(self):
        assert TRANSIENT_API_STATUSES == frozenset(
            {408, 409, 429, 500, 502, 503, 504, 529}
        )


class TestTransientTextFallback:
    @pytest.mark.parametrize("text", [
        "API Error: 529 Overloaded",
        "rate limit exceeded",
        "rate_limit_error",
        "upstream returned 503",
        "502 Bad Gateway",
        "request timed out",
        "connection timeout",
    ])
    def test_text_fingerprints_when_no_status(self, text):
        assert is_transient_api_error(None, text) is True

    def test_no_status_no_match_is_not_transient(self):
        assert is_transient_api_error(None, "permission denied") is False
        assert is_transient_api_error(None, "") is False
        assert is_transient_api_error(None) is False


class TestRetryBudget:
    def test_budget_constants_present(self):
        assert TURN_RETRY_BASE == 2.0
        assert TURN_RETRY_MAX_ATTEMPTS == 2


class TestSessionDelegates:
    """Session._is_transient_turn_error must delegate to the shared classifier
    (behaviour-preserving extraction — same verdict it gave before #MDS-65)."""

    def _session(self):
        from bobi.session import Session
        return Session(name="t", cwd="/tmp")

    def test_session_uses_status(self):
        s = self._session()
        s._last_api_error_status = 529
        s._last_response = ""
        assert s._is_transient_turn_error() is True

    def test_session_concrete_status_not_transient(self):
        s = self._session()
        s._last_api_error_status = 400
        s._last_response = "overloaded"
        assert s._is_transient_turn_error() is False

    def test_session_text_fallback(self):
        s = self._session()
        s._last_api_error_status = None
        s._last_response = "API Error: 529 Overloaded"
        assert s._is_transient_turn_error() is True

    def test_session_module_reexports_shared_symbols(self):
        import bobi.session as sess
        from bobi import transient
        assert sess.TRANSIENT_API_STATUSES is transient.TRANSIENT_API_STATUSES
        assert sess.is_transient_api_error is transient.is_transient_api_error
