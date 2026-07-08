"""Deprecated compatibility shim for the renamed sleep-cycle monitor."""

from .sleep_cycle import (
    MAX_SEED_INPUT_CHARS,
    MAX_SLEEP_CYCLE_INPUT_CHARS,
    _truncate_head_tail,
    build_sleep_cycle_task,
    parse_result,
    read_cursor,
    render_transcript,
    select_messages,
    write_cursor,
)

MAX_CURATOR_INPUT_CHARS = MAX_SLEEP_CYCLE_INPUT_CHARS
build_curator_task = build_sleep_cycle_task
