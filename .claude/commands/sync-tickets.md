---
description: Reconcile docs/TICKET_STATE.md against live GitHub issues and Linear tickets. Finds stale rows (closed-but-shown-open), missing new issues, count drift, and GitHub↔Linear sync gaps; updates the doc; optionally fixes Linear. Read-only by default until the diff is shown.
---

# Sync Ticket State

Reconcile `docs/TICKET_STATE.md` (the living GitHub-issue overview) with the
**actual** state of GitHub issues and Linear tickets, then update the doc.
`docs/TICKET_STATE.md` is GitHub-first; Linear is cross-checked for sync gaps.

**Golden rule:** gather all real state FIRST, show the user a diff/findings, then
write. Never edit the doc before you've reconciled against live data. Linear
mutations are outward-facing — propose them and get a yes before applying.

## Phase 1 — Read the doc

Read `docs/TICKET_STATE.md` end to end. Note its claimed open-count, "Last
reviewed" date, every track table, the "One-offs" tables, and the "Recently
closed" list. These are what you'll reconcile.

## Phase 2 — Gather live GitHub state

```bash
cd ~/dev/bobi
# Open issues (the authoritative set the doc must match)
gh issue list --state open --limit 200 \
  --json number,title,labels,assignees,updatedAt \
  --jq 'sort_by(.number) | .[] | "#\(.number) [\(.assignees|map(.login)|join(","))] \(.title)"'

# Recently closed (to catch rows the doc still shows as active + fill "Recently closed")
gh issue list --state closed --limit 40 --json number,title,closedAt \
  --jq 'sort_by(.closedAt) | reverse | .[] | "#\(.number) (closed \(.closedAt[:10])) \(.title)"'
```

For any issue whose status is ambiguous (e.g. closed as done vs not-planned, or a
new issue the doc never mentions), pull its detail:

```bash
gh issue view <N> --json number,state,stateReason,assignees,labels,body
```

## Phase 3 — Gather live Linear state

Linear is **not** in my Venn connection — query its GraphQL API directly with the
key in `.env`. **Gotchas (learned the hard way):**
- Use **`curl`**, not Python `urllib` — urllib fails with `CERTIFICATE_VERIFY_FAILED` on this Mac.
- **Don't** inline an f-string in a `bash -c` python heredoc (line-continuation/escaping breaks). Write the parser to a temp file, then run it.
- Team keys: **`MDS`** = bobi engineering (maps to the GitHub repo). **`MOD`** = the moda/storyteller product + GTM/marketing — *mostly not tracked by `docs/TICKET_STATE.md`*; only flag MOD tickets that clearly mirror a GitHub track.

List open (non-completed/canceled) Linear issues:

```bash
cd ~/dev/bobi
cat > /tmp/parse_linear.py <<'PYEOF'
import sys, json
d = json.load(sys.stdin)
if not d.get("data"):
    print("ERR:", json.dumps(d)[:800]); sys.exit()
for n in sorted(d["data"]["issues"]["nodes"], key=lambda x: x["identifier"]):
    asg = (n["assignee"] or {}).get("name", "-")
    print(f'{n["identifier"]:10} [{n["state"]["name"]:13}] {asg:14} {n["title"][:70]}')
print(f'--- {len(d["data"]["issues"]["nodes"])} open ---')
PYEOF
KEY=$(grep '^LINEAR_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $KEY" -H "Content-Type: application/json" \
  -d '{"query":"{ issues(filter:{state:{type:{nin:[\"completed\",\"canceled\"]}}}, first:200, orderBy:updatedAt){ nodes{ identifier title state{name type} assignee{name} } } }"}' \
  | python3 /tmp/parse_linear.py
```

Fetch a specific Linear ticket's description (for re-scope/close decisions):

```bash
KEY=$(grep '^LINEAR_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $KEY" -H "Content-Type: application/json" \
  -d '{"query":"{ issues(filter:{number:{in:[47,48]}, team:{key:{eq:\"MDS\"}}}){ nodes{ id identifier title state{name} description } } }"}'
```

## Phase 4 — Reconcile (compute the diff, don't write yet)

Cross-reference the doc against live state and produce a findings list:

1. **Count drift** — doc's claimed open-count vs `gh` actual.
2. **Stale-active rows** — any issue the doc shows in a track/one-off table that
   GitHub reports **CLOSED**. These usually mean a whole track silently finished.
3. **Missing new issues** — open GitHub issues the doc never mentions. Slot each
   into an existing track or propose a new one.
4. **Recently-closed gaps** — closed issues not yet in the "Recently closed" list.
5. **GitHub↔Linear sync gaps** — Linear tickets still open whose GitHub twin is
   closed (look for `[MDS-NN]`/`[MOD-NN]` tags in GitHub titles/bodies and commit
   messages, or matching subject matter). Flag stale Linear state and any epic
   that needs re-scoping. Distinguish *the goal is still open* from *this specific
   mechanism was retired* before recommending close vs re-scope.

Present these findings to the user as a concise table/summary **before editing**.

## Phase 5 — Update `docs/TICKET_STATE.md`

Apply the reconciliation with `Edit`:
- Fix the **open-count** and bump **"Last reviewed"** to today (derive "today" from
  the newest GitHub `closedAt`/`updatedAt` you saw — the harness clock can lag).
  Keep a short "Prev reviewed" trail of the previous header bullet.
- Move closed issues out of track tables into **"Recently closed"** with a one-line
  resolution each.
- Flip finished tracks to ✅ DONE; add new tracks/one-offs for the missing issues.
- Make sure **every open GitHub issue maps to exactly one track** — state that as a
  closing check.
- Keep the doc GitHub-first; mention Linear only as sync-gap callouts.

## Phase 6 — Optionally fix Linear (ask first)

If Phase 4 found Linear sync gaps, propose the specific mutations and get a yes
before applying (these are outward-facing). Pattern that works (curl, not urllib):

```bash
cd ~/dev/bobi
cat > /tmp/linear_mutate.py <<'PYEOF'
import json, subprocess, pathlib
KEY = next(l.split("=",1)[1].strip().strip('"').strip("'")
           for l in pathlib.Path(".env").read_text().splitlines()
           if l.startswith("LINEAR_API_KEY="))
def gql(query, variables):
    body = json.dumps({"query": query, "variables": variables})
    p = subprocess.run(["curl","-s","-X","POST","https://api.linear.app/graphql",
        "-H",f"Authorization: {KEY}","-H","Content-Type: application/json",
        "--data-binary","@-"], input=body.encode(), capture_output=True)
    d = json.loads(p.stdout)
    if d.get("errors"): print("  ERROR:", json.dumps(d["errors"])[:400]); return None
    return d.get("data")
UPDATE  = "mutation($id:String!,$input:IssueUpdateInput!){ issueUpdate(id:$id,input:$input){ success issue{ identifier state{name} } } }"
COMMENT = "mutation($id:String!,$body:String!){ commentCreate(input:{issueId:$id,body:$body}){ success } }"
# Resolve UUIDs + workflow-state IDs first (mutations need UUIDs, not MDS-NN):
#   issues(filter:{number:{in:[..]},team:{key:{eq:"MDS"}}}){ nodes{ id identifier } }
#   workflowStates(filter:{team:{key:{eq:"MDS"}}}){ nodes{ id name type } }   # find the "Done"/"Canceled" id
# Example: close + comment
# print(gql(UPDATE,  {"id": ISSUE_UUID, "input": {"stateId": DONE_STATE_UUID}}))
# print(gql(COMMENT, {"id": ISSUE_UUID, "body": "Closing — shipped via #NNN ..."}))
# To reframe without closing: COMMENT only, or prepend a banner to description via UPDATE.
PYEOF
echo "edit /tmp/linear_mutate.py with the resolved UUIDs, then: python3 /tmp/linear_mutate.py"
```

Reframe-don't-delete: when an epic/ticket's *mechanism* was retired but its *goal*
is still open, prepend a dated status banner to its description (keep the original
notes below) and/or add a clarifying comment — don't rewrite or close it.

## Phase 7 — Report + offer to commit

Summarize what changed in the doc and in Linear. The doc edits land in the working
tree unstaged. Per repo convention, **don't commit to `main` directly** — offer to
branch + commit `docs/TICKET_STATE.md`, or leave it for the user. A
`docs:`/chore-style commit touching only this file needs no VERSION/CHANGELOG bump
(that policy is release-only).
