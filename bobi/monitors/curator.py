"""Deprecated compatibility shim for the renamed sleep-cycle monitor."""

from .sleep_cycle import (
    _truncate_head_tail,
    build_sleep_cycle_task,
    parse_result,
    read_cursor,
    render_transcript,
    select_messages,
    write_cursor,
)

build_curator_task = build_sleep_cycle_task
