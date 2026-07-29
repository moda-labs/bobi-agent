#!/usr/bin/env bash
#
# Structural checks on plan artifacts under plans/.
#
# This is the ONLY non-agent verification in the checklist execution model, and
# it is deliberately a shell script rather than a module in bobi/: both checks
# are git-shaped (a marker-aware diff and a grep), and the artifact format is a
# lifecycle convention, not a framework property. Nothing here parses the
# checklist into a data structure, writes a marker, or drives anything.
#
# It NEVER executes a `verify:` string. A verify: is attacker-reachable shell —
# plans arrive through pull requests from any account — and the whole reason no
# provenance gate exists around it is that nothing runs one unattended. If that
# ever changes, the gate comes back with it.
#
# Usage: check-plan-artifact.sh <base-ref> <head-ref>
#
# Exit 0 = pass, 1 = a violation (with a diagnostic), 2 = misuse.

set -uo pipefail

BASE="${1:-}"
HEAD="${2:-}"
if [ -z "$BASE" ] || [ -z "$HEAD" ]; then
  echo "usage: $0 <base-ref> <head-ref>" >&2
  exit 2
fi

FENCE='```checklist'
failures=0

fail() {
  echo "FAIL  $1" >&2
  failures=$((failures + 1))
}

note() { echo "      $1" >&2; }

# The review surface is everything above the appendix fence. A file with no
# fence is not under checklist execution and is all review surface.
review_surface() {
  awk -v fence="$FENCE" '$0 == fence { exit } { print }'
}

appendix() {
  awk -v fence="$FENCE" 'seen { print } $0 == fence { seen = 1 }'
}

# Collapse the STATE fields of a plan to fixed tokens so a diff sees past them.
# Everything else above the fence is approved prose and must survive verbatim.
#
# Two things count as state rather than prose:
#
#  - A checklist marker. `- [ ]` / `- [wip]` / `- [x]` / `- [f] state:<tag>` all
#    become `- [@]`, which is what makes this comparison marker-AWARE rather
#    than a plain diff. The optional state tag rides along because tagging an
#    [f] is the one marker transition that legitimately adds text.
#  - The `**Status:**` line. Every plan transitions Draft -> Approved -> done,
#    and a builder flipping it is doing the same bookkeeping as flipping a
#    marker. Freezing it would make the check unusable on the exact PRs that
#    advance a plan. Only the status VALUE is normalized — up to the first `·` —
#    so other metadata sharing that line (`· **Created:** ...`) stays frozen.
#
# Deliberately NOT normalized: anything else. A stale line reference, an item's
# wording, a gate command that turned out to be wrong — those are corrected by a
# dated amendment, never edited in place, because an in-place edit is how the
# reviewed document and the built one silently diverge.
normalize_markers() {
  sed -E \
    -e 's/^([[:space:]]*)- \[(x| |wip|f)\]([[:space:]]+state:[a-z][a-z0-9-]*)?/\1- [@]/' \
    -e 's/^(>?[[:space:]]*\*\*Status:\*\*)[^·]*/\1 [@]/'
}

changed_plans="$(git diff --name-only --no-renames "$BASE" "$HEAD" -- 'plans/*.md' || true)"

if [ -z "$changed_plans" ]; then
  echo "No plan files changed — nothing to check."
  exit 0
fi

echo "Checking plan artifacts changed between $BASE and $HEAD:"
echo "$changed_plans" | sed 's/^/  /'
echo

while IFS= read -r path; do
  [ -n "$path" ] || continue

  # Deleted in HEAD: nothing to validate.
  if ! git cat-file -e "$HEAD:$path" 2>/dev/null; then
    note "$path: deleted, skipped"
    continue
  fi

  head_body="$(git show "$HEAD:$path")"

  # --- 1. Conflict debris and truncation ---------------------------------
  # A malformed artifact must fail with a diagnostic, never a traceback, and
  # never be silently accepted as "well, the diff matched".
  if printf '%s\n' "$head_body" | grep -qE '^(<<<<<<< |=======$|>>>>>>> )'; then
    fail "$path: unresolved merge-conflict markers in the artifact"
    note "a conflicted plan is not a plan; resolve it before committing"
  fi

  # Known limitation: a plan that DOCUMENTS the artifact format — quoting the
  # fence inside an example — trips this. No live plan does, and the fix if one
  # ever needs to is to wrap the example in a longer (````) fence, exactly as
  # skills/checklist-execution.md does. Rejecting an ambiguous appendix boundary
  # is worth that cost; guessing which fence is "the real one" is not.
  fence_count="$(printf '%s\n' "$head_body" | grep -cxF "$FENCE" || true)"
  if [ "$fence_count" -gt 1 ]; then
    fail "$path: $fence_count '$FENCE' fences — the appendix must open exactly once"
  fi

  # --- 2. Every [f] carries a machine-readable state tag ------------------
  # Scoped to lines this diff ADDS. Plans written before the convention carry
  # prose-only [f] markers, and retro-fitting them would mean rewriting
  # approved plan text — the exact thing the review surface exists to prevent.
  # So the rule binds what you write, not what you inherited, and the
  # convention takes hold as plans are worked rather than by a mass edit.
  bad_f="$(git diff -U0 "$BASE" "$HEAD" -- "$path" \
    | grep -E '^\+' | grep -v '^+++' \
    | grep -E '^\+[[:space:]]*- \[f\]' \
    | grep -vE '^\+[[:space:]]*- \[f\][[:space:]]+state:[a-z][a-z0-9-]*' || true)"
  if [ -n "$bad_f" ]; then
    fail "$path: [f] marker without a machine-readable state: tag"
    printf '%s\n' "$bad_f" | sed 's/^/        /' >&2
    note "use e.g. '- [f] state:blocked-on-human <why>'"
  fi

  # --- 3. Gate lines in the appendix are classified ----------------------
  # Scoped to the appendix on purpose. The appendix is the machine-rendered
  # surface, where a renderer is responsible for emitting `verify:` or
  # `judgement:` on every gate line. Hand-written gate lines above the fence
  # predate that contract and are not retro-fitted here.
  # An item is its marker line plus the indented continuation lines under it,
  # so the tag is looked for across the whole block — `verify:` almost always
  # sits on a line below the item text, not beside it.
  head_appendix="$(printf '%s\n' "$head_body" | appendix)"
  unclassified="$(printf '%s\n' "$head_appendix" | awk '
    function flush() {
      if (item != "" && block !~ /verify:/ && block !~ /judgement:/)
        print line ": " item
    }
    /^[[:space:]]*- \[(x| |wip|f)\]/ { flush(); item = $0; block = $0; line = NR; next }
    item != "" { block = block "\n" $0 }
    END { flush() }
  ')"
  if [ -n "$unclassified" ]; then
    fail "$path: appendix items with neither a verify: nor a judgement: tag"
    printf '%s\n' "$unclassified" | head -10 | sed 's/^/        /' >&2
    note "an item with neither is not checkable, so 'done' against it is empty"
  fi

  # --- 4. The review surface is append-only ------------------------------
  # The rule is INSERTION-ONLY, not byte-identical: an existing line above the
  # fence may never be modified or deleted, but new lines may be added.
  #
  # That distinction is what keeps this check out of workflow politics. A dated
  # amendment is additive by convention ("post-approval changes are dated
  # amendments, never silent rewrites"), so it passes — in ANY pull request,
  # including one that also carries the work. A worker rewriting approved text
  # is a modification, so it fails — again in any pull request. The check
  # therefore takes NO position on whether the plan rides the same PR as the
  # work or a separate one; that is the author's call, not this script's.
  #
  # An earlier version demanded byte-identity apart from markers, which forbade
  # amendments outright — and then needed a "did this diff touch the appendix?"
  # heuristic to guess worker-versus-human so amendments could escape. The
  # heuristic is gone, and so is the gap it left: this now applies to every
  # plans/ diff, including a prose edit that never touches the appendix.
  if ! git cat-file -e "$BASE:$path" 2>/dev/null; then
    note "$path: new file, no base to compare"
    continue
  fi

  base_body="$(git show "$BASE:$path")"

  base_appendix="$(printf '%s\n' "$base_body" | appendix)"

  # 4a. The appendix grew by appending, not by rewriting: the old appendix must
  # be a prefix of the new one. A reader takes it as a chronology, so an edit in
  # the middle is a rewritten history.
  #
  # MARKER-AWARE, like 4b. The appendix holds two different kinds of line — the
  # items, whose markers are supposed to change as work lands, and the round
  # log, which is append-only. A byte-exact prefix would forbid the ordinary
  # case of flipping an appendix item's marker, so markers are normalized away
  # first and what remains is the text, which must not move.
  base_ap_n="$(printf '%s\n' "$base_appendix" | normalize_markers)"
  head_ap_n="$(printf '%s\n' "$head_appendix" | normalize_markers)"
  if [ -n "$base_appendix" ] && [ "$base_ap_n" != "$head_ap_n" ] && \
     [ "${head_ap_n:0:${#base_ap_n}}" != "$base_ap_n" ]; then
    fail "$path: appendix was rewritten, not appended to"
    note "existing appendix text must survive as a prefix; markers may change"
  fi

  # 4b. Above the fence: markers may move, lines may be added, nothing else.
  # `diff` emitting any '<' line means a base line was changed or removed —
  # pure insertion yields only '>' lines.
  base_surface="$(printf '%s\n' "$base_body" | review_surface | normalize_markers)"
  head_surface="$(printf '%s\n' "$head_body" | review_surface | normalize_markers)"
  removed="$(diff <(printf '%s\n' "$base_surface") \
                  <(printf '%s\n' "$head_surface") | grep '^<' || true)"

  if [ -n "$removed" ]; then
    fail "$path: existing review-surface text was modified or deleted"
    printf '%s\n' "$removed" | head -20 | sed 's/^/        /' >&2
    note "above the fence you may change a marker or ADD lines — nothing else"
    note "if approved text is wrong, that is a block or a dated amendment,"
    note "never an in-place edit"
  fi

done <<< "$changed_plans"

echo
if [ "$failures" -gt 0 ]; then
  echo "$failures plan-artifact check(s) failed." >&2
  exit 1
fi
echo "Plan artifacts OK."
