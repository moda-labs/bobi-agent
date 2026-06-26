# `deployments/` — per-instance deploy config

Each `deployments/<name>.yaml` is **one instance an operator runs**. Adding a
file (and running `bobi deploy <name>`, or letting the GitOps Action pick
it up) deploys; deleting it surfaces the orphaned Fly app for a human-gated
`bobi destroy`. This decouples **what a team is** (`agents/<team>/`, a
portable package) from **where/how an operator runs it** (here) — one team can
back many deployments (`acme-eng`, `staging-eng`, …).

See `docs/design/DEPLOY_INTERFACE.md` for the full design and
`docs/DEPLOYMENT.md` for the runbook (including the bring-your-own-repo setup).

## Schema

```yaml
# deployments/<name>.yaml   — the deployment NAME is the filename
team: my-team               # local package agents/my-team → ssh-push delivery
# team-url: https://…/my-team.tar.gz   # …OR a published tarball → HTTPS-fetch
                                        # (set exactly one of team / team-url)

fleet: acme                 # app = "<fleet>-<name>"; BOBI_FLEET stamp
event_server: https://ev.acme.workers.dev
region: sjc
memory: 8gb
cpus: 2
volume_size: 15
auth: subscription          # api_key (default) | subscription
# subscription mode: first-boot Slack login destination:
#   "#ops-private"          private/public channel name
#   "@zachkozick"           bot DM with a user in the bot token workspace
#   "C0PRIVATE"/"D0DM..."   raw Slack channel ID, still supported
login_channel: "#ops-private"
# login_channel:
#   type: im
#   user: zachkozick
claude_version: 2.1.89      # pin the baked-in claude CLI

secrets:
  env: acme-eng             # named source (a GitHub Environment) — CI materializes it
  # env-file: ./acme-eng.env   # …OR a local file (self-service)
```

Only the team source is required; everything else falls back through
`deployments/defaults.yaml` (shared values) to built-in defaults. Name a file by
the **team/instance slug**, not by repeating the fleet (fleet is prepended for
you: `fleet: acme` + `eng-team.yaml` → app `acme-eng-team`).

## Precedence

```
CLI flags  ›  deployments/<name>.yaml  ›  deployments/defaults.yaml  ›  built-ins
```

`bobi deploy <name>` performs this merge itself, so it works standalone.
