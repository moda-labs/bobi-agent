# Building Agent Teams

A focused guide for authoring a bobi agent team. Written for the
agent (or human) doing the authoring. For the product vision around
interactive onboarding, see AGENT_TEAM_ONBOARDING.md — this document is
the durable reference for what a team *is*.

## Team layout

A team is a directory:

```
my-team/
  agent.yaml        # the manifest — required
  agent.md          # one-paragraph description, roles, usage
  roles/
    <role>/ROLE.md  # one prompt per role
  tools/*.md        # how-to docs injected for the agents (CLI recipes)
  workflows/*.yaml  # step-based workflows (validate with `bobi agent <name> workflows validate`)
  monitors/         # optional: defaults.yaml + custom check scripts
  context/          # optional: reference files agents read on demand
  workspace/        # optional: seed templates for user-owned domain files
```

Ship it inside a project at `agents/<name>/`, or in a registry repo with
an `agents/` directory. Users run `bobi agents install <path-or-name>`.

## agent.yaml

Fields the framework parses (see `bobi/config.py`):

```yaml
version: "1.0.0"
entry_point: manager        # role launched by `bobi agent <name> start` — must exist in roles/
chat: slack                 # human interaction surface: slack | telegram | none

services:                   # what the team connects to
  - name: github            # native: github | slack | linear
    events: true            # events: true → auto-subscribe to this source
  - name: email             # non-native names resolve through Venn
    events: true

# Credentials and per-machine values: ALWAYS ${VAR} references, never
# literals. Install scans for ${VAR}, prompts for missing values, and
# writes run/.env. Config.load() resolves them at runtime.
slack:
  bot_token: ${SLACK_BOT_TOKEN}
linear:
  api_key: ${LINEAR_API_KEY}
venn_api_key: ${VENN_API_KEY}
event_server: ${BOBI_EVENT_SERVER}   # empty → auto-started local server

mcp_servers:                # custom tools, wired into agent sessions via the SDK
  internal-crm:
    type: http              # http | sse (needs url) | stdio (needs command)
    url: https://crm.internal/mcp
    headers:
      Authorization: Bearer ${CRM_TOKEN}

monitors:                   # polling that fills webhook gaps
  - name: new-emails
    command: 'venn --json tools execute -s gmail -t list_emails -a ...'
    interval: 5m
    event: email/received

subscribe:                  # explicit subscription override — rarely needed;
  - github:org/repo         # omitting it enables auto-detection (preferred)
```

Auto-detected subscriptions (when `subscribe:` is absent): each service
with `events: true` resolves itself — github from the project's git
remote (or, when the project root is not a git repo, from each immediate
child repo — the director-at-`~/dev` layout), slack workspace from the
bot token, linear teams from the API key.

Blocks like `task_tracking`, `verify`, and `context` are not parsed by
the framework — they are advisory config your roles read from the
installed agent.yaml. Use them to give the manager judgment guidance
(trigger labels, review policy, style), and document them in the role
prompts that consume them.

## Roles

One directory per role under `roles/`, each with a `ROLE.md` prompt.
`entry_point` names the role that `bobi agent <name> start` launches; it spawns
the others. Write prompts the way you'd brief a person: responsibilities,
what to do with each event type, when to escalate, which tools docs to
follow.

## Monitors

A monitor either runs on an `interval:` (`15m`, `1h`, `2d`) or at
wall-clock times (`at:`), and detects a condition the scheduler dedups and
publishes as `event:`. The flavors:

- **Command monitors** — a shell command returning JSON; the scheduler
  diffs results by stable `id` across runs and fires `event:` for new
  items. The command must return a flat, diffable list. Discover the
  right Venn tool interactively (`venn tools search/describe/execute`)
  and test it before writing the line.
- **Description-only monitors** — when output needs interpretation, give
  a `description:` instead of a `command:`; the scheduler spawns a
  short-lived agent to decide whether to fire. Costs an LLM call per
  interval; use when diffable JSON isn't available.
- **Native checks** — `check: pr_conflicts` names a Python runner shipped
  with the framework or the pack's `monitors/*_checks.py`.
- **Scheduled notifications** — `notify: true` fires `event:` once on every
  scheduled run, keyed to the due time so dedup never suppresses it. For
  *nudges* an agent reacts to, not condition detection.

### Wall-clock and weekly schedules

`at:` runs a monitor at fixed times of day instead of on an interval,
optionally pinned to a timezone and gated to specific weekdays:

| field | meaning |
|---|---|
| `at:` | time(s) of day — `"21:00"` or `["06:00", "18:00"]` |
| `tz:` | IANA timezone for `at:` (e.g. `America/Los_Angeles`); defaults to host local |
| `days:` | weekday(s) the `at:` times may fire on — names (`sun`, `mon`) or numbers (`0`/`7`=Sunday … `6`=Saturday). Absent ⇒ every day |

`days:` is how you express **weekly** recurrence — it's just a filter on
which weekdays an `at:` time is eligible. An at-monitor never fires on
first sight (the first tick records a baseline). A plain daily `at:` slot
missed while the manager was down fires **once, late**, on the next tick
(catch-up); a **weekly** (`days:`-gated) slot does **not** catch up — a
missed run is skipped and only the next scheduled occurrence fires. DST is
handled: "Sunday 21:00 LA" stays 21:00 local across the boundary.

### Schedule a weekly job

A weekly job is a `notify` monitor on a weekly `at:`/`days:` schedule whose
`event:` an agent reacts to — no special "job" machinery. Create it without
hand-editing YAML:

```bash
bobi agent <name> monitors add weekly-prep-doc \
  --at 21:00 --days sun --tz America/Los_Angeles --notify \
  --event monitor/prep.weekly_due \
  --description "Generate my prep doc for the upcoming week"
```

The *task* the job performs lives in the pack, not the framework: a
`context/` skill file the agent reads when the event arrives, plus a
routing line in the role prompt that points to it. The eng-team ships
exactly this pattern as a worked example — `context/prep-doc.md` (the
skill) wired from the director role's `monitor/prep.weekly_due` handler.
Copy it as a template for your own weekly jobs.

## Tool guides: function vs. policy

`tools/*.md` loads fully into every role's prompt, so it should carry
**policy** — how this team uses a service: thread discipline, voice,
attribution, escalation, what counts as actionable. The rules a human
lead would put in an onboarding doc.

**Function** (command syntax, flags) belongs to surfaces that can't
drift from the installed version:

- command-specific `--help` output — generated from code, always correct
- `bobi skill bobi` — the full CLI reference
- `venn tools describe` — live schemas from the gateway

Never re-document bobi or venn CLI syntax in a tool guide — name
the command and let agents pull syntax from those surfaces.
`tests/test_tool_guides.py` fails the build if a pack prompt references
a bobi command that doesn't exist (this drift reached main twice).

The exception: services the framework doesn't wrap (a raw REST/GraphQL
API the team calls with a `${VAR}` credential). The pack is the only
owner of those mechanics, so document them in the tool guide — minimal
and tested by hand. See `agents/eng-team/tools/linear.md`.

## Context files

`context/*.md` is team-shipped reference content — rubrics, methodology,
output format specs, worked examples. It installs frozen to
`run/package/context/` and agents see an index (path + first line) in
their prompt, reading files on demand. Make the first line of each file
a one-line description.

Use `context/` instead of `tools/` when the content is reference
material rather than a service guide: tools load fully into every
role's prompt; context files cost nothing until an agent reads one.

## Workspace seeds

`workspace/` holds templates for user-owned domain content — the things
only the user can fill in (positioning, source lists, watchlists) and
the directories agents write work products into. Install copies it to
`<project>/workspace/`, each file only if absent: reinstall never
overwrites what users or agents wrote there.

Reference these files from role prompts by installed path
(`workspace/<file>`), and tell users in `agent.md` which files to fill
in before starting the team. Filling in workspace files is not
customization — the team source stays untouched and updatable.

## Decision log (memory)

Every agent has a persistent decision log at
`run/state/memory/<session-name>/`. The framework injects it into
context at every session start — this is what makes `--fresh` and session
rotation safe. The agent curates the content; the framework owns the
lifecycle.

### Storage

```
run/state/memory/<session-name>/
  INDEX.md           # YAML current-state block + prose notes
  2026-06-10-deploy-policy.md   # optional per-topic notes
```

The `INDEX.md` opens with a YAML frontmatter block for machine-readable
current operational state, followed by timestamped prose notes:

```markdown
---
managed_repos:
  - moda-labs/bobi
  - moda-labs/jobtack
slack_channel: "#eng-alerts"
linear_team: MDS
---

- dogfood tracks in MDS — Zach, 2026-06-10
- prefer squash merges for single-commit PRs — team decision, 2026-06-09
```

### Prompt contract

The base prompt (`prompts/base.md`) instructs every agent to:

- **Write a note** when making a durable decision or learning something
  that future sessions need.
- **Keep the YAML current-state block accurate** — update it when facts
  change.
- **Prune** entries that turn out to be wrong or superseded.
- **One fact per note line**, with provenance (who said it, when).
- **Never store secrets** in the decision log.

### Lifecycle

- Memory survives `--fresh` (which only wipes the session ID, not state).
- Memory survives reinstall and version upgrades (lives in `state/`,
  which is gitignored and not part of the frozen install image).
- `bobi agent <name> doctor` checks for agents with empty decision logs and
  flags them as potential drift.

### Team authoring notes

When designing roles for your team, consider what each role should record:

- **Directors/managers**: topology decisions (which repos, routing
  preferences, team mappings), operational intent.
- **Project leads**: per-repo context, preferred workflows, known quirks.
- **Engineers**: generally don't need persistent memory — they work on
  bounded tasks via workflows.

You don't need to add memory-related instructions to your role prompts —
the contract in `prompts/base.md` is inherited by every role automatically.

## The frozen-image contract

`bobi agents install` regenerates `run/package/` verbatim from the team
source — every time, no merging. Authoring rules that follow from this:

- Never instruct users (or agents) to edit `run/package/` — edits are
  destroyed by the next install, and `bobi agent <name> doctor` flags them
  against the install manifest.
- All variance a deployment needs must be expressible as `${VAR}` in
  agent.yaml + a value in `run/.env`, or as a user-owned file
  seeded from `workspace/`. If your team needs a knob, make it an env
  reference; if it needs domain content, make it a workspace file.
- Customization means editing the team source and reinstalling. For
  teams the user doesn't own, that means materializing a copy into their
  repo first (setup's customize branch / eject).

## Validating your team

```bash
bobi agents install agents/my-team    # copies image, prompts for ${VAR}s
bobi agent <name> start                     # preflight: entry point, credentials,
                                    # Venn connections, MCP probe (lists tools)
bobi agent <name> doctor                    # env checks + install-image drift
bobi agent <name> workflows validate        # workflow schema
```

Then the real test: file an issue / send the event your team claims to
handle, and watch `run/state/manager.log`.

## Reference implementations

- `agents/eng-team` (bobi repo) — multi-repo org: director entry
  point, project leads, engineers; github + slack with tool-agnostic seams
  (Moda's house team derives from it via `from: eng-team`).
- `agents/dogfood-content-review` (bobi repo) — single-repo content
  pipeline: manager entry point, researcher/editor/fact-checker roles,
  github + email via Venn, command monitor for inbound email.
