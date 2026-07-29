# Widget cache invalidation

> **Status:** Approved · **Created:** 2026-07-29
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked

FROZEN FIXTURE. `tests/test_plan_artifact_check.py` mutates copies of this file
to prove the artifact check catches what it claims to. It is deliberately a
small invented plan rather than a copy of a live one: the real plans under
`plans/` mutate as work lands, and a fixture that drifts stops proving anything.

## Purpose

Widget lookups miss the cache whenever a tenant renames a widget, because the
key is built from the display name. Move the key to the immutable id.

## Problem

`WidgetCache.key()` interpolates `widget.display_name`. A rename produces a new
key, the old entry is never evicted, and the cache grows without bound.

## Phases

### Phase 1 — Key on the id

- [x] Reproduce the leak: a rename leaves the pre-rename entry resident
- [x] Key on `widget.id`; leave the display name out of the key entirely
- [ ] Evict pre-existing stale entries on first read after deploy
- [f] state:blocked-on-human Decide whether to purge the cache on deploy or let
      stale entries age out — needs an owner call on the read-amplification cost

**Validation gate**

- [ ] Failing-first: a rename no longer leaves a resident entry
- [ ] The eviction path is exercised by a test that fails without it
- [ ] `pytest tests/test_widget_cache.py -q`

## Notes

- The display name stays in the cached *value*; only the key changes.

```checklist
- [x] Reproduce the leak with a failing test
      verify: git log --oneline -1 --grep "test: rename leaves a resident entry"
- [x] Key on widget.id
      verify: grep -q "widget.id" src/widget_cache.py
- [ ] Evict stale entries on first read
      verify: pytest tests/test_widget_cache.py::test_evicts_stale -q
- [ ] Confirm the read-amplification cost is acceptable to the owner
      judgement: a human decides whether the extra read per key is worth it

### Round log

2026-07-29 — Keyed on `id` rather than a composite of id+name. A composite
would have kept the rename information in the key, which is exactly what makes
the entry unreachable after a rename.
```
