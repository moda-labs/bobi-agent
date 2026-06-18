"""Regression tests for asyncio event loop isolation (#318).

The polluter is pytest-playwright's session-scoped ``playwright`` fixture,
which runs an asyncio event loop inside a greenlet.  Because greenlets share
the OS thread, the C-level ``_running_loop`` thread-local leaks across tests.

The ``_clear_leaked_event_loop`` autouse fixture in conftest.py clears the
thread-local before each non-e2e test.  These tests verify the guard works.
"""

import asyncio


def test_no_running_loop_visible_to_unit_tests():
    """The conftest fixture must clear any leaked running loop before we run."""
    assert asyncio.events._get_running_loop() is None, (
        "A running event loop leaked into a unit test. "
        "The _clear_leaked_event_loop fixture should have cleared it."
    )


def test_asyncio_run_succeeds():
    """asyncio.run() must work in unit tests — the original symptom of #318."""
    async def _noop():
        return 42

    result = asyncio.run(_noop())
    assert result == 42


async def test_async_test_runs_cleanly():
    """Async tests (via pytest-asyncio auto mode) must not see a stale loop."""
    await asyncio.sleep(0)  # would raise if loop state is poisoned


def test_simulated_leak_is_cleaned_up():
    """If a test leaks a running loop, the fixture cleans it for the next test.

    This simulates what Playwright's greenlet does: set _running_loop to a
    real loop object.  The fixture's teardown restores the saved value, and
    the next test's setup clears it again.
    """
    loop = asyncio.new_event_loop()
    try:
        # Simulate a leak — as if a greenlet left a running loop on this thread.
        asyncio.events._set_running_loop(loop)
        # The NEXT test (test_asyncio_run_after_simulated_leak) will verify
        # the fixture cleared this leak.  Within this test, we just confirm
        # the leak is in place.
        assert asyncio.events._get_running_loop() is loop
    finally:
        # Cleanup: the fixture will restore the pre-test value (None) on
        # teardown, but we also close the loop to avoid ResourceWarning.
        loop.close()


def test_asyncio_run_after_simulated_leak():
    """Proves the fixture cleaned up the leak from the previous test."""
    assert asyncio.events._get_running_loop() is None

    async def _noop():
        return 99

    result = asyncio.run(_noop())
    assert result == 99
