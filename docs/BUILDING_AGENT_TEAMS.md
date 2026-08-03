# Building Agent Teams

The definitive guide for authoring a bobi agent team. Written for the
agent (or human) doing the authoring: how to design the team, what each
part of the package is, and how to validate it.

## Designing your team

Before writing files, settle what the team is. A few questions frame
everything else:

1. **What does this agent do?** One sentence describing the domain
   ("manage the engineering SDLC", "run sales outreach", "monitor support
   tickets"). This frames everything that follows.
2. **What distinct jobs does it involve?** Each job a human would wear a
   different hat for is a candidate role - a coder, a reviewer, a QA; or a
   company researcher, a voice tracker, a PMF analyst. Each role gets its
   own prompt and responsibilities.
3. **What services does it read from and write to?** email, github,
   linear, salesforce, calendar, notion, and so on. This is the connection
   list (see [Connecting services](#connecting-services)).
4. **How do you interact with it?** A chat surface (`slack`, `telegram`,
   or `none`). Interaction is the human channel; autonomy comes from
   monitors and event triggers, not from chat.
5. **What should it do on a schedule?** Recurring proactive work - a
   Monday-morning digest, a nightly health check - becomes monitors or
   scheduled jobs.
6. **What events should it react to?** For each service, the specific
   trigger condition: a PR opening, a VIP-domain email, a ticket moved to
   "To Do". This is the difference between an agent that watches your inbox
   and one that only acts when told.
7. **(Optional) What needs a human gate?** High-stakes steps where a
   person must sign off become workflow `await` steps - e.g. outreach copy
   approved in Slack before it sends, a deploy approved before it promotes.

### Start with fewer roles

Bias toward the smallest team that covers the distinct jobs, and let it
grow into more roles as real needs surface.

- **Prefer one role doing more over many narrow roles.** A single capable
  role with a clear prompt is easier to operate, debug, and reason about
  than a fan-out of specialists. Many domains start best as one role.
- **You don't need a role per task.** The entry-point role spawns
  sub-agents on demand for bounded work (`bobi agent <name> subagents
  launch`), so transient or one-off jobs never need their own standing
  role. Define roles for durable, recurring responsibilities, not
  individual tasks.
- **Add a role when a real need recurs.** Split out a new role once a
  distinct responsibility shows up often enough that a dedicated prompt and
  its own context clearly help - not preemptively. Growing a team is
  cheaper than coordinating one that was over-divided up front.

A team can be a single role, and should scale to a
director-plus-leads-plus-engineers org only when the work genuinely
demands it.

### Worked examples

Same problem space, very different sizes - pick the smallest shape that
does the job:

- **Engineering SDLC** (multi-role org): a **director** (triages incoming
  work, assigns it), **project leads** (coordinate within a project), and
  **engineers** (execute tasks). GitHub + Linear, Slack chat, director as
  entry point. Reacts to PRs, issue assignments, and status changes.
- **Deploy monitor** (single role): one agent watches deploy events, runs
  smoke tests on an interval, and posts alerts to a Slack channel. No
  back-and-forth chat, fully autonomous.

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

### Launch caps

Three independent caps bound how much a team can spawn. They stack, and each
answers a different question:

| Field | Bounds | Default |
|---|---|---|
| `max_launch_depth` | how *deep* a chain of launches can go | 8 |
| `max_concurrent_agents` | how many agents run at once | 2 |
| `spend_cap` | agent invocations per rolling hour, deployment-wide | 50 |

All three follow the `0 = use default` idiom.

`max_launch_depth` is the first resort in front of `spend_cap`. Every launch
carries the ordered chain of runs that led to it in `BOBI_LAUNCH_LINEAGE`, and
a launch is refused before any process is spawned when either shape appears:

- **self-recursion** - a run launching a named workflow already in its own
  chain. `adhoc` is exempt, since `-w adhoc --wait` delegation is ordinary
  work; depth governs that instead.
- **depth** - a chain deeper than the cap.

Set the cap in `agent.yaml`, or override it per deployment with the
`BOBI_MAX_LAUNCH_DEPTH` environment variable (which wins, so an operator can
move the ceiling on a running fleet without rebuilding the team image).

Refusals emit `system/launch.blocked` carrying the rendered chain and which
rule fired; `system/launch.depth.approaching` warns one level below the cap.

**Known limits.** This is a rail against accidental recursion, not a security
boundary: both variables live in the agent's own shell, so an agent that clears
the chain or raises the cap defeats it - the spend governor stays the backstop.
It sees direct launches only; a cycle mediated by the event bus attributes the
new run to the manager, which is the event-server circuit breaker's shape. And
a process that started before an upgrade carries no chain, so restart the
manager after upgrading. Lineage is deliberately **not** persisted to the
session registry - forensics ride the `agent/session.started` event instead.

Blocks like `task_tracking`, `verify`, and `context` are not parsed by
the framework — they are advisory config your roles read from the
installed agent.yaml. Use them to give the manager judgment guidance
(trigger labels, review policy, style), and document them in the role
prompts that consume them.

## Connecting services

Every service the team uses connects through one of three mechanisms:

- **Native** (`github`, `slack`, `linear`) - built-in webhook
  integrations. GitHub auto-detects from the git remote and acts via the
  `gh` CLI; Slack uses `${SLACK_BOT_TOKEN}`; Linear uses
  `${LINEAR_API_KEY}`. Paste the key when `bobi agents install` prompts.
- **OAuth via Venn** (`gmail`, `salesforce`, `calendar`, `notion`, `jira`,
  ...) - anything that needs OAuth. Venn holds pre-registered OAuth apps
  for 50+ services behind a single API key, which sidesteps headless token
  acquisition (no browser on the box, no per-provider OAuth app to
  register). The user connects services once at venn.ai, then agents read
  and write with the `venn` CLI and poll with `venn exec` in monitors.
  Paste `${VENN_API_KEY}`.
- **Custom MCP servers** - internal or bespoke tools, declared under
  `mcp_servers:` (`http`/`sse`/`stdio`). They wire into the agent session
  through the SDK, so the agent gets their tools automatically. Preflight
  probes each server and lists its tools.

Prefer native where it exists, Venn for OAuth SaaS, and MCP for anything
custom.

## Roles

One directory per role under `roles/`, each with a `ROLE.md` prompt.
`entry_point` names the role that `bobi agent <name> start` launches; it spawns
the others. Write prompts the way you'd brief a person: responsibilities,
what to do with each event type, when to escalate, which tools docs to
follow.

### Slack reply style in role prompts

Every role inherits the framework prompt before its own `ROLE.md`. That base
prompt already tells agents to make Slack replies concise, human-readable
summaries rather than reasoning transcripts, and the Slack delivery path handles
common markdown-to-Slack formatting.

Use role prompts to add the shape that is specific to that job, not to restate
the framework default. For example, a support role can require a verdict and
customer-facing next step; a director role can require a short status digest.
When a role truly needs a different Slack format, say so explicitly in the
role prompt, because the role prompt is appended after the base prompt.

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
routing line in the role prompt that points to it. The eng-team ships the
skill half of this pattern — `agents/eng-team/context/prep-doc.md` — but no
`prep.weekly_due` monitor or handler, so it is a template for the *file*, not
a wired end-to-end example. For a wired one, see the director role's
`monitor/status.roundup_due` handler (`roles/director/ROLE.md`), which routes a
scheduled monitor event to a task the same way; swap its schedule for the
`at:`/`days:` form above.

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
owner of those mechanics, so document them — minimal and tested by hand.
See `agents/eng-team/context/linear.md`, which documents Linear's GraphQL
calls; it sits in `context/` rather than `tools/` because there is no
`linear` CLI for a guide to teach.

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

## Long-term memory

The team has a single, team-scoped long-term memory at
`run/state/long_term_memory.md`. The framework injects it **read-only** into
every agent's prompt at session start — this is what makes `--fresh` and
session rotation safe. The `sleep-cycle` monitor owns the content; the
framework owns the lifecycle.

It replaces the per-session decision log (`run/state/memory/<session>/INDEX.md`)
that earlier versions of Bobi shipped. That journal bloated prompts and died
with the agent; nothing writes it now.

### Storage

```
run/state/long_term_memory.md   # one file, team-scoped, two sections
```

The file holds two sections — `## Facts` (the current state of the world: which
tracker the repo uses, the deploy command, stable preferences) and
`## Decisions` (settled choices not to re-litigate):

```markdown
## Facts

- dogfood tracks in MDS — Zach, 2026-06-10
- deploys run from `main` only — team decision, 2026-06-09

## Decisions

- prefer squash merges for single-commit PRs — team decision, 2026-06-09
```

### Prompt contract

The base prompt (`prompts/base.md`) tells every agent the opposite of a
write-your-own-notes contract:

- **Agents do not write it.** The `sleep-cycle` monitor is the single writer.
  `prompts/base.md` says explicitly: *"Do not edit
  `<run>/state/long_term_memory.md` yourself."*
- **To make something durable, state it plainly in the transcript** — the
  decision, the preference, or the standing instruction, with who said it and
  when. The sleep cycle distills transcripts into the file on a schedule.
- **There is no per-session journal** to maintain and no flush step on rotation.
- **Don't record volatile detail** (a single ticket number, a transient session
  id) — that is re-derived from GitHub/Linear/`agents list`.
- **Never store secrets**, tokens, or credentials.

### Lifecycle

- Memory survives `--fresh` (which only wipes the session ID, not state).
- Memory survives reinstall and version upgrades (lives in `state/`,
  which is gitignored and not part of the frozen install image).
- `bobi agent <name> doctor` checks `long_term_memory.md` — that it exists, is
  within its size cap, and has no foreign writers.

### Team authoring notes

Because the sleep cycle is the only writer, you do **not** design per-role
memory contracts. What you influence is what ends up worth distilling: roles
whose prompts ask for decisions to be stated plainly (with provenance) give the
sleep cycle something to work with; roles that work silently do not.

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

- `agents/eng-team` (bobi repo) - multi-repo org: director entry
  point plus async engineer workers; github + slack with tool-agnostic seams
  (Moda's house team derives from it via `from: eng-team`).
- `agents/dogfood-content-review` (bobi repo) - single-repo content
  pipeline: manager entry point, researcher/editor/fact-checker roles,
  github + email via Venn, command monitor for inbound email.
