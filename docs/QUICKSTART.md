# Quickstart

Go from nothing to a running Bobi agent - installed, set up, and deployed
either on your own machine or on Fly.io. Every command is copy-pasteable.
Expect about 15 minutes for the local path; add 15-30 more for a cloud deploy.

If you get stuck at any point, jump to [If you get stuck](#if-you-get-stuck) -
worst case, you can point a Claude Code or Codex session at Bobi's own docs and
have it troubleshoot with you.

## What you'll end up with

- The `bobi` CLI installed.
- An agent runtime (Claude Code) installed and logged in.
- Your first agent team installed and running - either one you design with the
  interactive wizard, or the ready-made `eng-team`.
- The agent running locally, or deployed as an always-on instance on Fly.io.

## Prerequisites

- **macOS or Linux** with a terminal.
- **An Anthropic account** - a Claude Pro/Max subscription or an API key.
  Every Bobi agent runs on Claude Code by default (OpenAI Codex also works;
  see [Choose the runtime](../README.md#choose-the-runtime-optional)).
- **Homebrew or uv** to install the CLI. If you have neither, uv is the
  quickest to get:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- **Optional, for the ready-made `eng-team`**: a GitHub personal access token
  and a Slack workspace where you can install an app (Step 3, Path B).
- **Optional, for cloud deployment**: a [Fly.io](https://fly.io) account
  (Step 5, Option B).

You do not need to clone the Bobi repo. Bobi is a published package - install
the CLI and go.

## Step 1: Install and log in to Claude Code

Bobi runs each agent on Claude Code. Skip this step if you already have it
installed and logged in.

```bash
brew install --cask claude-code
```

Or with npm:

```bash
npm install -g @anthropic-ai/claude-code
```

Then launch it once to log in:

```bash
claude
```

Follow the login prompt (choose your Claude subscription or API key), then exit
the session with `/exit`. Verify it works:

```bash
claude --version
```

> **Tip:** if `claude` is not found after installing, open a new terminal so
> your `PATH` refreshes. See the
> [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) for
> platform-specific help.

## Step 2: Install Bobi

With Homebrew:

```bash
brew install moda-labs/bobi-agent/bobi
```

Or with uv:

```bash
uv tool install bobi
```

Verify the install:

```bash
bobi --version
```

> **Tip:** with uv, if `bobi` is not found, run
> `export PATH="$HOME/.local/bin:$PATH"` (and add it to your shell profile).
> You can also install with `pipx install bobi` if you prefer pipx.

Everything Bobi creates lives under one home directory, `~/.bobi` by default.
You can inspect or delete it at any time without affecting anything else.

## Step 3: Create your first agent team

Pick one path. Path A is the recommended default for a brand-new user: the
wizard walks you through everything and you don't need any external service
credentials to get a working agent. Path B gets you the ready-made engineering
team, but it requires GitHub and Slack credentials before it will start.

### Path A (recommended): design your own with the setup wizard

```bash
bobi setup my-agent
```

This opens a local web UI (on `127.0.0.1`) that takes you from an idea to an
installed, runnable agent: describe what you want the agent to do, review what
Bobi suggests, connect any services it needs, watch it build the team, then
review and install it. Plain-language descriptions are fine - "watch my
GitHub repo and summarize new issues every morning" is a perfectly good start.

- Name it whatever you like - the examples below use `my-agent`. If you pick a
  different name, substitute it in every later command.
- You can interrupt the wizard anytime and pick up where you left off:

  ```bash
  bobi setup my-agent --resume
  ```

When the wizard finishes, your agent is installed and you can go straight to
Step 4.

### Path B: install the ready-made `eng-team`

`eng-team` is the bundled engineering agent: it triages issues, opens PRs
through a review-and-CI workflow, and watches for merge conflicts and stale
PRs. It treats GitHub and Slack as core services, so have these ready:

1. **A GitHub token** - create a personal access token at
   [github.com/settings/tokens](https://github.com/settings/tokens) with repo
   access (or run `gh auth token` if you already use the GitHub CLI).
2. **Slack app credentials** - a bot token (`xoxb-...`) and signing secret.
   The fastest way is Bobi's generator, which builds an app manifest for you:

   ```bash
   bobi create-slack-bot --app-name "Bobi"
   ```

   Follow its instructions, then see [Slack setup](../skills/slack-setup.md)
   for the full walkthrough if anything is unclear.

Then install:

```bash
bobi agents install eng-team --name eng-team
```

The installer prompts for each credential the team references (`GH_TOKEN`,
`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and optional scoping vars like
`SLACK_CHANNELS`) and saves them to
`~/.bobi/agents/eng-team/run/.env`. Never commit that file anywhere. For
`BOBI_EVENT_SERVER`, leave it blank - Bobi auto-starts a local event server.

> **Tip:** you can leave a prompt blank and fill it in later by editing
> `~/.bobi/agents/eng-team/run/.env`, but `eng-team` will not start until its
> required GitHub and Slack credentials are present. If you just want to see
> Bobi working right now, use Path A instead.

If you used Path B, substitute `eng-team` for `my-agent` in every command
below.

## Step 4: Start the agent and talk to it

Start it:

```bash
bobi agent my-agent start
```

This runs preflight checks, launches a local event server (loopback only -
nothing leaves your machine), and starts the agent as a background daemon.

Check that it's healthy:

```bash
bobi agent my-agent status
bobi agent my-agent doctor
```

`doctor` runs a full health check - runtime layout, credentials, workflows,
monitors - and prints a hint for anything it finds wrong. It's the first thing
to run whenever something misbehaves.

Now talk to your agent. `ask` blocks until it responds:

```bash
bobi agent my-agent ask "What can I help with right now?"
```

`message` is fire-and-forget:

```bash
bobi agent my-agent message "FYI: the staging deploy is paused this week"
```

Hand it a one-off task as a sub-agent:

```bash
bobi agent my-agent subagents launch -w adhoc --role engineer --task "Fix the login bug"
```

(`-w adhoc` runs an open-ended task outside any structured workflow.)

(Roles are defined by the team - run `bobi agent my-agent roles list` to see
what yours has.)

Watch what it's doing:

```bash
bobi agent my-agent events              # recent events and decisions
bobi agent my-agent subagents list      # running sub-agents
bobi agent my-agent transcript show manager   # full session transcript
bobi agent my-agent costs               # spend accounting
```

Stop and restart whenever you like - state persists:

```bash
bobi agent my-agent stop
bobi agent my-agent start
```

At this point you have a working, event-driven agent. Step 5 decides where it
lives long-term.

## Step 5: Deploy it

### Option A: keep it local (simplest)

You already did it - `bobi agent my-agent start` is the local deployment. The
agent runs as a daemon with a loopback event server, reacts to its scheduled
monitors, and answers `ask`/`message` from your terminal. No cloud, no
accounts beyond your Anthropic login.

Two limits to know about:

- The agent only works while your machine is on.
- Inbound webhooks from the public internet (Slack messages, GitHub events)
  can't reach a loopback server. For those, deploy to Fly (Option B) or point
  the agent at a deployed event server.

If local covers your needs, you're done - skip to
[Where to go next](#where-to-go-next).

### Option B: deploy to Fly.io (always-on)

`bobi deploy` packages your agent into a container image and runs it as an
always-on Fly Machine - no Dockerfile, no server config. The deployed instance
holds an outbound WebSocket to an internet-reachable event server (the shared
Bobi cloud Worker by default - you don't need to set anything), so Slack and
GitHub webhooks work out of the box.

**1. Use a released Bobi version.** Deploy from a normal `uv tool install
bobi` / Homebrew install (the instance image pins the version you're running).

**2. Set up Fly.** If you've never used Fly:

```bash
brew install flyctl
fly auth signup     # or: fly auth login
```

New personal Fly orgs may be flagged "high-risk" until you verify a card at
[fly.io/high-risk-unlock](https://fly.io/high-risk-unlock). Don't worry about
getting all of this perfect: `bobi deploy` preflights your Fly setup first and
prints exactly what's missing and how to fix it.

**3. Write a secrets file.** Deployed instances default to API-key auth, so
you need an Anthropic API key (from
[console.anthropic.com](https://console.anthropic.com)) plus every credential
your team uses. For a wizard-built agent with no external services:

```bash
printf 'ANTHROPIC_API_KEY=sk-ant-your-key-here\n' > ./my-agent.env
```

For `eng-team`, include its service credentials too:

```bash
cat > ./eng-team.env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-your-key-here
GH_TOKEN=ghp_your-token-here
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_SIGNING_SECRET=your-signing-secret-here
SLACK_CHANNELS=
EOF
```

Keep these files out of git.

**4. Deploy.** Point `--team` at your installed agent's source and go:

```bash
bobi deploy my-agent --team ~/.bobi/agents/my-agent/src --env-file ./my-agent.env
```

For the bundled `eng-team`, a bare name works - Bobi fetches the package from
its registry:

```bash
bobi deploy eng-team --env-file ./eng-team.env
```

The command provisions the Fly app and volume, builds the image, ships your
team, and starts the agent. First deploys take several minutes (image build
plus first boot). It's idempotent: run the same command again anytime to
update the instance in place.

**5. Verify and operate.** The Fly app name matches your deployment name
(`my-agent` here; it becomes `<fleet>-<name>` only if you set a fleet):

```bash
fly status -a my-agent
fly logs -a my-agent
```

Tear it down when you're done (this deletes the volume, the only copy of the
instance's state):

```bash
bobi destroy my-agent
```

For CI-driven fleets, GitOps, Codex-backed teams, and subscription-auth
deployments, read the full runbook:
[CONTAINERIZED_DEPLOYMENT.md](CONTAINERIZED_DEPLOYMENT.md).

## If you get stuck

Work through these in order:

1. **Run the doctor.** It checks the runtime, credentials, services,
   workflows, and monitors, and prints a fix hint per failure:

   ```bash
   bobi agent my-agent doctor
   ```

2. **Check status and events.** `bobi agent my-agent status` shows whether
   the agent is actually running; `bobi agent my-agent events` shows what it
   last saw and decided.

3. **Fix credentials directly.** Team secrets live in
   `~/.bobi/agents/<name>/run/.env`. Edit that file and restart the agent:

   ```bash
   bobi agent my-agent restart
   ```

4. **Start fresh.** If a session is wedged, wipe it and start clean (your
   workspace files and credentials are kept):

   ```bash
   bobi agent my-agent start --fresh
   ```

5. **Fly deploys:** `fly logs -a <app>` is the first stop. The
   [deployment runbook's troubleshooting list](CONTAINERIZED_DEPLOYMENT.md#manual-ops)
   covers the known failure modes. Re-running `bobi deploy <name>` is safe and
   fixes most partial deploys.

6. **Enlist an AI pair.** Bobi's docs are written to be agent-readable. Open a
   Claude Code or Codex session next to your terminal and paste:

   ```plaintext
   Read https://raw.githubusercontent.com/moda-labs/bobi-agent/main/skills/bobi.md and help me troubleshoot my bobi agent. Here's what's happening: <describe your problem and paste any error output>
   ```

   It can run the diagnostic commands above with you and interpret the output.

7. **File an issue.** If you've found a real bug, open one at
   [github.com/moda-labs/bobi-agent/issues](https://github.com/moda-labs/bobi-agent/issues)
   with the `doctor` output and relevant logs.

## Where to go next

- **Talk to your agent from Slack** instead of the terminal:
  [Slack setup](../skills/slack-setup.md).
- **Connect Linear** so ticket updates drive the agent:
  [Linear setup](../skills/linear-setup.md).
- **Extend or build teams** - roles, workflows, monitors, tools:
  [create-agent skill](../skills/create-agent.md) and
  [BUILDING_AGENT_TEAMS.md](BUILDING_AGENT_TEAMS.md).
- **Full CLI reference:** [skills/bobi.md](../skills/bobi.md).
- **How the event bus works:** [EVENT_SERVER.md](EVENT_SERVER.md).
- **Security model** (read before installing third-party teams):
  [SECURITY.md](SECURITY.md).
