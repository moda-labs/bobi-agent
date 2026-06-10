# Building Agent Teams

A focused guide for authoring a modastack agent team. Written for the
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
  workflows/*.yaml  # step-based workflows (validate with `modastack workflows validate`)
  monitors/         # optional: defaults.yaml + custom check scripts
```

Ship it inside a project at `agents/<name>/`, or in a registry repo with
an `agents/` directory. Users run `modastack install <path-or-name>`.

## agent.yaml

Fields the framework parses (see `modastack/config.py`):

```yaml
version: "1.0.0"
entry_point: manager        # role launched by `modastack start` — must exist in roles/
chat: slack                 # human interaction surface: slack | telegram | none

services:                   # what the team connects to
  - name: github            # native: github | slack | linear
    events: true            # events: true → auto-subscribe to this source
  - name: email             # non-native names resolve through Venn
    events: true

# Credentials and per-machine values: ALWAYS ${VAR} references, never
# literals. Install scans for ${VAR}, prompts for missing values, and
# writes .modastack/.env. Config.load() resolves them at runtime.
slack:
  bot_token: ${SLACK_BOT_TOKEN}
linear:
  api_key: ${LINEAR_API_KEY}
venn_api_key: ${VENN_API_KEY}
event_server: ${MODASTACK_EVENT_SERVER}   # empty → auto-started local server

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
`entry_point` names the role that `modastack start` launches; it spawns
the others. Write prompts the way you'd brief a person: responsibilities,
what to do with each event type, when to escalate, which tools docs to
follow.

## Monitors

Two kinds:

- **Command monitors** — a shell command returning JSON; the scheduler
  diffs results by stable `id` across runs and fires `event:` for new
  items. The command must return a flat, diffable list. Discover the
  right Venn tool interactively (`venn tools search/describe/execute`)
  and test it before writing the line.
- **Description-only monitors** — when output needs interpretation, give
  a `description:` instead of a `command:`; the scheduler spawns a
  short-lived agent to decide whether to fire. Costs an LLM call per
  interval; use when diffable JSON isn't available.

## The frozen-image contract

`modastack install` regenerates `.modastack/` verbatim from the team
source — every time, no merging. Authoring rules that follow from this:

- Never instruct users (or agents) to edit `.modastack/` — edits are
  destroyed by the next install, and `modastack doctor` flags them
  against the install manifest.
- All variance a deployment needs must be expressible as `${VAR}` in
  agent.yaml + a value in `.modastack/.env`. If your team needs a knob,
  make it an env reference.
- Customization means editing the team source and reinstalling. For
  teams the user doesn't own, that means materializing a copy into their
  repo first (setup's customize branch / eject).

## Validating your team

```bash
modastack install agents/my-team    # copies image, prompts for ${VAR}s
modastack start                     # preflight: entry point, credentials,
                                    # Venn connections, MCP probe (lists tools)
modastack doctor                    # env checks + install-image drift
modastack workflows validate        # workflow schema
```

Then the real test: file an issue / send the event your team claims to
handle, and watch `.modastack/state/manager.log`.

## Reference implementations

- `agents/eng-team` (modastack repo) — multi-repo org: director entry
  point, project leads, engineers; github + linear + slack.
- `agents/content-review` (modastack-dogfood repo) — single-repo content
  pipeline: manager entry point, researcher/editor/fact-checker roles,
  github + email via Venn, command monitor for inbound email.
