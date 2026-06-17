"""Hermetic tests for the stateless streaming LLM transport.

No network, no CLI: a scripted `stream_fn` stands in for the SDK source.
"""

import asyncio

import pytest

from modastack.setup import llm
from modastack.setup.llm import LLMError


def _run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [c async for c in agen]


def _fake(chunks):
    """A stream_fn that records its kwargs and yields scripted chunks."""
    captured = {}

    async def fn(*, system_prompt, user_prompt, model, cwd):
        captured.update(system_prompt=system_prompt, user_prompt=user_prompt,
                        model=model, cwd=cwd)
        for c in chunks:
            yield c

    return fn, captured


class TestStream:
    def test_yields_chunks_in_order(self):
        fn, _ = _fake(["Hel", "lo, ", "world"])
        out = _run(_collect(llm.stream("sys", "hi", stream_fn=fn)))
        assert out == ["Hel", "lo, ", "world"]

    def test_passes_context_through_to_source(self):
        fn, captured = _fake(["x"])
        _run(_collect(llm.stream("SYS", "USER", model="sonnet",
                                 cwd="/tmp/p", stream_fn=fn)))
        assert captured == {"system_prompt": "SYS", "user_prompt": "USER",
                            "model": "sonnet", "cwd": "/tmp/p"}


class TestComplete:
    def test_concatenates_full_text(self):
        fn, _ = _fake(["a", "b", "c"])
        assert _run(llm.complete("sys", "u", stream_fn=fn)) == "abc"

    def test_empty_stream_is_empty_string(self):
        fn, _ = _fake([])
        assert _run(llm.complete("sys", "u", stream_fn=fn)) == ""

    def test_source_error_propagates(self):
        async def boom(*, system_prompt, user_prompt, model, cwd):
            raise LLMError("transport died")
            yield  # pragma: no cover  (makes this an async generator)

        with pytest.raises(LLMError):
            _run(llm.complete("sys", "u", stream_fn=boom))


class TestDeltaExtraction:
    def test_pulls_text_from_content_block_delta(self):
        ev = {"type": "content_block_delta",
              "delta": {"type": "text_delta", "text": "hello"}}
        assert llm._delta_text(ev) == "hello"

    def test_ignores_non_text_events(self):
        assert llm._delta_text({"type": "message_start"}) == ""
        assert llm._delta_text({"type": "content_block_delta",
                                "delta": {"type": "input_json_delta"}}) == ""
        assert llm._delta_text("not a dict") == ""
