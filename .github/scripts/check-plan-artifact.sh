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

has_fence() {
  grep -qxF "$FENCE"
}

# Collapse a marker transition to a single token so a diff sees past it.
# `- [ ]` / `- [wip]` / `- [x]` / `- [f] state:<tag>` all normalize to `- [@]`,
# which is what makes this comparison marker-AWARE rather than a plain diff.
# The optional state tag is included because tagging an [f] is the one marker
# transition that legitimately adds text above the fence.
normalize_markers() {
  sed -E 's/^([[:space:]]*)- \[(x| |wip|f)\]([[:space:]]+state:[a-z][a-z0-9-]*)?/\1- [@]/'
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

  # --- 3. Review-surface freeze ------------------------------------------
  # Scope: only when this diff touches the appendix. That is the mechanical
  # signal for "a worker mutated this file" as opposed to "a human amended
  # it" — amendments legitimately rewrite prose above the fence, and freezing
  # them would make the plan un-amendable.
  #
  # Known and accepted gap: a worker that edits prose WITHOUT touching the
  # appendix is not caught here. Stated rather than papered over — this is a
  # marker-aware diff, not a proof.
  if ! git cat-file -e "$BASE:$path" 2>/dev/null; then
    note "$path: new file, no base to compare"
    continue
  fi

  base_body="$(git show "$BASE:$path")"

  printf '%s\n' "$head_body" | has_fence || { note "$path: no appendix"; continue; }

  base_appendix="$(printf '%s\n' "$base_body" | appendix)"
  head_appendix="$(printf '%s\n' "$head_body" | appendix)"

  if [ "$base_appendix" = "$head_appendix" ]; then
    note "$path: appendix unchanged — treated as a human amendment"
    continue
  fi

  # 3a. The appendix grew by appending, not by insertion or rewrite: the old
  # appendix must be a literal prefix of the new one. A reviewer reads it as a
  # chronology, so an edit in the middle is a rewritten history.
  if [ -n "$base_appendix" ] && \
     [ "${head_appendix:0:${#base_appendix}}" != "$base_appendix" ]; then
    fail "$path: appendix was rewritten, not appended to"
    note "the existing round log must survive byte-for-byte as a prefix"
  fi

  # 3b. Above the fence, only markers moved.
  base_surface="$(printf '%s\n' "$base_body" | review_surface | normalize_markers)"
  head_surface="$(printf '%s\n' "$head_body" | review_surface | normalize_markers)"

  if [ "$base_surface" != "$head_surface" ]; then
    fail "$path: the review surface changed by more than checklist markers"
    diff <(printf '%s\n' "$base_surface") <(printf '%s\n' "$head_surface") \
      | head -40 | sed 's/^/        /' >&2
    note "a worker may only change the marker inside an existing '- [ ]'"
    note "if the approved text is wrong, that is a block, not an edit"
  fi

  # --- 4. Gate lines in the appendix are classified ----------------------
  # Scoped to the appendix on purpose. The appendix is the machine-rendered
  # surface, where a renderer is responsible for emitting `verify:` or
  # `judgement:` on every gate line. Hand-written gate lines above the fence
  # predate that contract and are not retro-fitted here.
  # An item is its marker line plus the indented continuation lines under it,
  # so the tag is looked for across the whole block — `verify:` almost always
  # sits on a line below the item text, not beside it.
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

done <<< "$changed_plans"

echo
if [ "$failures" -gt 0 ]; then
  echo "$failures plan-artifact check(s) failed." >&2
  exit 1
fi
echo "Plan artifacts OK."
