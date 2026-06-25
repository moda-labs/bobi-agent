# Release Runbook

This is the production bugfix release recipe for `modastack` plus the Moda
agent-team fleet. Use it when a fix has merged to `main` and needs to reach the
Fly-hosted agents.

## 1. Sync `modastack`

```bash
cd ~/dev/modastack
git switch main
git pull --ff-only
git status --short
```

The worktree should be clean before the release bump.

## 2. Cut the `modastack` release

Pick the next patch/minor version. For a patch release, update:

- `VERSION`
- `pyproject.toml`
- `CHANGELOG.md`

Example:

```bash
$EDITOR VERSION pyproject.toml CHANGELOG.md
```

Keep the changelog entry concrete enough for an on-call agent to understand why
the release exists and which PRs it contains.

Run focused checks for the touched area plus the event server smoke if the fleet
or Slack/Event Server path is involved:

```bash
.venv/bin/python -m pytest tests/test_brain_codex.py tests/test_session.py tests/test_event_subscription.py -q

cd event-server
PATH="$HOME/.nvm/versions/node/v24.4.1/bin:$PATH" \
  npm test -- --run test/core.spec.ts test/index.spec.ts
cd ..
```

Commit and push the release bump:

```bash
git add VERSION pyproject.toml CHANGELOG.md
git commit -m "chore(release): cut <version>"
git push origin main
```

Publish the GitHub Release. This tag/release event is the gate that builds the
wheel, runs canary, publishes to PyPI, deploys the event server, and rolls the
release-owned fleet.

```bash
gh release create v<version> --target main --title "v<version>" --notes-file -
```

Watch it to completion:

```bash
gh run list --repo moda-labs/modastack --workflow release.yml --limit 3 \
  --json databaseId,displayTitle,status,conclusion,url

gh run watch <run-id> --repo moda-labs/modastack --interval 20
```

Do not continue to the Moda fleet pin until the release workflow is green,
including:

- subscription-login smoke
- release wheel build
- canary build/smoke
- PyPI publish
- event server deploy
- fleet roll jobs

If PyPI was just published, allow a short propagation delay before installing
the new version from another repo.

## 3. Bump `moda-agent-teams`

The Moda fleet has its own deploy pin. Update it after the `modastack` release
is available on PyPI.

```bash
cd ~/dev/moda-agent-teams
git switch main
git pull --ff-only
git status --short
```

Update both pins:

- `.github/workflows/deploy-agent-teams.yml`: `MODASTACK_VERSION`
- `.github/workflows/lint.yml`: pinned `pip install "modastack==..."`

Verify compose against the exact released package:

```bash
tmpdir=$(mktemp -d)
python3 -m venv "$tmpdir/venv"
"$tmpdir/venv/bin/pip" install -q "modastack==<version>" pyyaml
"$tmpdir/venv/bin/python" scripts/verify-tool-library.py
rm -rf "$tmpdir"
```

If pip cannot find the just-published version, wait 30-60 seconds and retry.

Commit, push, and dispatch the fleet rebuild:

```bash
git add .github/workflows/deploy-agent-teams.yml .github/workflows/lint.yml
git commit -m "ops: bump modastack deploy pin to <version>"
git push origin main

gh workflow run "Deploy agent teams" --ref main -f rebuild=true
gh run list --repo moda-labs/moda-agent-teams --workflow "Deploy agent teams" \
  --limit 3 --json databaseId,status,conclusion,url
gh run watch <run-id> --repo moda-labs/moda-agent-teams --interval 20
```

The deploy run should show green jobs for:

- `basketbot`
- `eng-team`
- `zachs-personal-assistant`

## 4. Inspect Fly startup

Check Eng Team first because it exercises GitHub, Linear, Slack, Codex, and
project-lead startup.

```bash
fly logs -a moda-eng-team --no-tail | tail -n 180
```

Expected healthy signals:

- preflight checks pass for GitHub, Slack, and Linear
- Slack workspace resolves to the Eng Team app and channel
- event subscription includes `slack:T0952RZRZ0X:app:A0BDLA833MW:C0BAEN48KQR`
- WebSocket/event client connects
- health endpoint reports the director as `running` or `idle`, not `error`

Useful health check:

```bash
fly ssh console -a moda-eng-team -C \
  "/bin/bash -lc 'curl -s http://127.0.0.1:<health-port>/health || true'"
```

The current health port is logged as:

```text
Manager health endpoint on port <port>
```

Known failure signatures:

- `Invalid API key` before re-registration: stale event deployment credentials.
- `Subscription update failed (...) — re-registering`: expected recovery path
  after stale credentials.
- Canary `modastack ask` fails while opening the reply channel with
  `POST /deployments` `500`: check the Cloudflare Worker tail. If it shows an
  internal Durable Object URL with `__modastack_internal=undefined`, the Worker
  is missing `INTERNAL_DO_SECRET`. Restore it with `wrangler secret put
  INTERNAL_DO_SECRET`, redeploy the event server if needed, then rerun the
  canary smoke.
- `Separator is not found/found, and chunk is longer than limit`: Codex JSON
  stream reader limit regression.
- Slack placeholder stays at `Evaluating...`: event reached Slack, but the
  brain/session has not completed the turn.

## 5. Validate Slack E2E with Venn

Use the local Venn CLI and the repo `.env` without printing secrets:

```bash
cd ~/dev/modastack
set -a; source .env; set +a
```

Send a unique marker to `#bobi-eng-team` and explicitly mention Eng Team:

```bash
MARKER="ENG_TEAM_E2E_$(date -u +%Y%m%d_%H%M%S)"
printf '%s\n' "$MARKER" > /tmp/modastack-engteam-marker

venn --json tools execute \
  -s moda-labs-slack \
  -t chat_postMessage \
  --confirm \
  -a "{\"channel\":\"C0BAEN48KQR\",\"text\":\"<@U0BCVME6Z60> release validation ${MARKER}: please reply in this channel with ${MARKER} OK.\",\"link_names\":true}"
```

Record the returned Slack `ts`, then fetch the thread:

```bash
venn --json tools execute \
  -s moda-labs-slack \
  -t conversations_replies \
  -a '{"channel":"C0BAEN48KQR","ts":"<thread_ts>","limit":10}'
```

A successful validation has:

- reply text: `<MARKER> OK`
- `app_id`: `A0BDLA833MW`
- `user`: `U0BCVME6Z60`
- `bot_id`: `B0BCKMMQN1H`

Also check the logs for the same marker path:

```bash
fly logs -a moda-eng-team --no-tail | tail -n 160
```

Expected:

- `Event queued: slack/slack.mention`
- `Delivering 1 event(s) to moda-director-project`
- Slack `chat.postMessage` `200 OK`

Optional cross-routing check:

```bash
fly logs -a ci-codex-test --no-tail | tail -n 100
```

The Eng Team marker should not appear in Codex Test logs.

## 6. Closeout

Before handing off, capture:

- modastack release URL
- release workflow run URL
- moda-agent-teams deploy run URL
- Slack marker and thread timestamp
- whether Eng Team replied with the correct app/user IDs
- any residual issues, such as duplicate final replies or slow startup

If validation finds a new production blocker, fix it in `modastack` with a
regression test, open and merge a focused PR, then repeat this runbook with the
next patch version.
