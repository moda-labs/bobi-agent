# Linear

File issues so the engineering team's agent (the eng-team pack) picks them
up. The handoff mechanism is a labeled ticket: a ticket created with the
**trigger label** (`agent`) in the configured **initial state**, in the
configured team, is exactly what eng-team dispatch auto-picks-up. You do not
message the engineering manager directly — the labeled ticket is the trigger.

Team, trigger label, initial state, and priority mapping all come from
`workspace/support-context.md`. Once you have filled in the concrete UUIDs
there (team, `agent` label, initial state), prefer those directly and skip
the lookup below. Use the lookup only if the UUIDs are missing or stale.

## Credentials

A Linear personal API key (`lin_api_...`) is in your environment as
`$LINEAR_API_KEY` (declared in `agent.yaml`, loaded from `.modastack/.env`).
There is no `modastack linear create` command, so create and update issues
against the Linear GraphQL API at `https://api.linear.app/graphql` with
`Authorization: $LINEAR_API_KEY`.

## Find the team and label IDs (once per run, then reuse)

The create mutation needs the team's UUID and the label's UUID, not their
names. Resolve them by the names in the context file:

```bash
KEY=$(grep -A1 'Team / project key' workspace/support-context.md | grep -oE '\b[A-Z]{2,}\b' | head -1)
curl -s https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"{ teams { nodes { id key name labels { nodes { id name } } states { nodes { id name type } } } } }"}'
```

From the response, pick the team whose `key` matches, then grab:
- the team `id`,
- the `id` of the label named `agent` (the trigger label),
- the `id` of the state named `Triage` (or the first state with
  `type == "triage"`, falling back to a `type == "unstarted"` state).

## Create the ticket

```bash
curl -s https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" \
  -d @- <<'JSON'
{"query":"mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url } } }",
 "variables":{"input":{
   "teamId":"<TEAM_UUID>",
   "title":"<concise symptom-first title>",
   "description":"<markdown body: problem, scope/blast radius, investigation (suspect files, recent commits, hypothesis), source link, severity, rough effort>",
   "stateId":"<TRIAGE_STATE_UUID>",
   "labelIds":["<AGENT_LABEL_UUID>"],
   "priority":<0-4>
 }}}
JSON
```

Priority: `1` Urgent, `2` High, `3` Normal, `4` Low (`0` = no priority).
Map from the severity in `workspace/support-context.md`.

The response returns `issue.identifier` (e.g. `BAO-142`) and `issue.url` —
use the URL in the Slack alert and the support log.

## Comment on an issue (e.g. a recurrence)

```bash
curl -s https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"mutation($id:String!,$body:String!){ commentCreate(input:{issueId:$id,body:$body}){ success } }","variables":{"id":"<ISSUE_UUID>","body":"Recurred: <count> more occurrences since <date>. <link>"}}'
```

## Key rules

- **The label is the trigger.** Always create with the `agent` label in the
  configured initial state. Without the label, eng-team will not pick it up;
  in a started or Done state, dispatch skips it.
- **Match the label name** to the eng-team pack's `trigger_labels`. If they
  changed it, update `workspace/support-context.md`.
- **One ticket per real issue.** Dedup against the support log before
  filing (see the role prompt's Intake phase). On a recurrence, comment on
  the existing ticket instead of opening a new one.
- **Write a startable brief.** The engineer agent works from the
  description — include the suspect file(s), recent commits, and the
  source link, not just the symptom.
- A `401` means the key is missing/expired — regenerate at
  linear.app/settings/api and update credentials.
