# Release Runbook

This is the production bugfix release recipe for `bobi` plus the Moda
agent-team fleet. Use it when a fix has merged to `main` and needs to reach the
Fly-hosted agents.

> **Repo-split phase 1 caveat:** the deploy commands now live in the separate
> `bobi-deploy` package (`bobi_deploy/`, its own wheel). It is NOT on PyPI and
> real releases never go there - anything published to PyPI is public forever,
> which conflicts with this package going closed-source, and a stale public
> copy next to a private index is a dependency-confusion setup. Distribution
> is a private index or git+ssh pin only. A `uv tool install bobi` from the
> next release therefore has no `bobi deploy`; that is the product line.
> Before the first private-channel release: raise bobi_deploy/pyproject.toml's
> `bobi>=` floor to the bobi release that ships the carve-out seams (0.40.0
> satisfies the pin but predates bobi.build/bobi.config). And claim the
> `bobi-deploy` name on PyPI with a defensive stub (or rename the package):
> it is squattable today and `deploy-init`-scaffolded fleet workflows
> pip-install it by name inside CI jobs holding FLY_API_TOKEN. CI and this
> runbook's fleet steps are unaffected (they install both packages from the
> checkout).

## 1. Sync `bobi`

```bash
cd ~/dev/bobi-agent
git switch main
git pull --ff-only
git status --short
```

The worktree should be clean before the release bump.

## 2. Cut the `bobi` release

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
gh run list --repo moda-labs/bobi-agent --workflow release.yml --limit 3 \
  --json databaseId,displayTitle,status,conclusion,url

gh run watch <run-id> --repo moda-labs/bobi-agent --interval 20
```

The release workflow deploys the Cloudflare event server, then checks the
authoritative fleet event-server URL by fetching `<event_server>/health`. Set
the repo variable `FLEET_EVENT_SERVER_URL` when production fleet config lives
outside this repo; otherwise the workflow falls back to this repo's
`deployments/defaults.yaml`. The health payload must report the wheel version
and source git SHA just deployed. If this fails, stop: production is pointed at a
different Worker or the new Worker has not propagated.

Do not continue to the Moda fleet pin until the release workflow is green,
including:

- subscription-login smoke
- release wheel build
- canary build/smoke
- PyPI publish
- GHCR base image publish (`ghcr.io/moda-labs/bobi:<version>`; `:latest` moves
  when this version is the repo's latest non-prerelease release)
- event server deploy
- event server identity check against the fleet `event_server` URL
- fleet roll jobs

If PyPI was just published, allow a short propagation delay before installing
the new version from another repo.

One-time setup (first release only): the first push creates the GHCR package
as private. Make it public in the package settings
(github.com/orgs/moda-labs/packages) so consumers can pull without a token;
visibility persists across releases.

Spot-check the published base image AS A CONSUMER - log out of GHCR first so
the pull proves anonymous access works (a logged-in maintainer pull succeeds
even while the package is still private):

```bash
docker logout ghcr.io
docker run --rm --entrypoint bobi ghcr.io/moda-labs/bobi:<version> --version
```

The full run contract is in `docs/CONTAINERIZED_DEPLOYMENT.md`.

## 3. Bump `moda-agents`

The Moda fleet has its own deploy pin. Update it after the `bobi` release
is available on PyPI.

```bash
cd ~/dev/moda-agents
git switch main
git pull --ff-only
git status --short
```

Update both pins:

- `.github/workflows/deploy-agent-teams.yml`: `BOBI_VERSION`
- `.github/workflows/lint.yml`: pinned `pip install "bobi==..."`

Worker identity check: if the event-server Worker name or URL changes, move all
external entry points together before validating the release:

- `moda-agents` `deployments/defaults.yaml` `event_server`
- Slack app request URLs
- GitHub App webhook URL
- Linear webhook URL

After any move, fetch the fleet URL directly and confirm `/health` reports the
release version/SHA that the `bobi-agent` release workflow deployed.

Verify compose against the exact released package:

```bash
tmpdir=$(mktemp -d)
python3 -m venv "$tmpdir/venv"
"$tmpdir/venv/bin/pip" install -q "bobi==<version>" pyyaml
"$tmpdir/venv/bin/python" scripts/verify-tool-library.py
rm -rf "$tmpdir"
```

If pip cannot find the just-published version, wait 30-60 seconds and retry.

Commit, push, and dispatch the fleet rebuild:

```bash
git add .github/workflows/deploy-agent-teams.yml .github/workflows/lint.yml
git commit -m "ops: bump bobi deploy pin to <version>"
git push origin main

gh workflow run "Deploy agent teams" --ref main -f rebuild=true
gh run list --repo moda-labs/moda-agents --workflow "Deploy agent teams" \
  --limit 3 --json databaseId,status,conclusion,url
gh run watch <run-id> --repo moda-labs/moda-agents --interval 20
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
- Canary `bobi agent <name> ask` fails while opening the reply channel with
  `POST /deployments` `500`: check the Cloudflare Worker tail. If it shows an
  internal Durable Object URL with `__bobi_internal=undefined`, the Worker
  is missing `INTERNAL_DO_SECRET`. Restore it with `wrangler secret put
  INTERNAL_DO_SECRET`, redeploy the event server if needed, then rerun the
  canary smoke.
- `Separator is not found/found, and chunk is longer than limit`: Codex JSON
  stream reader limit regression.
- Slack typing indicator stays active: event reached Slack, but the
  brain/session has not completed the turn or did not clear typing on reply.

## 5. Validate Slack E2E with Venn

Use the local Venn CLI and the repo `.env` without printing secrets:

```bash
cd ~/dev/bobi-agent
set -a; source .env; set +a
```

Send a unique marker to `#bobi-eng-team` and explicitly mention Eng Team:

```bash
MARKER="ENG_TEAM_E2E_$(date -u +%Y%m%d_%H%M%S)"
printf '%s\n' "$MARKER" > /tmp/bobi-engteam-marker

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

## 6. Closeout

Before handing off, capture:

- bobi release URL
- release workflow run URL
- moda-agents deploy run URL
- Slack marker and thread timestamp
- whether Eng Team replied with the correct app/user IDs
- any residual issues, such as duplicate final replies or slow startup

If validation finds a new production blocker, fix it in `bobi` with a
regression test, open and merge a focused PR, then repeat this runbook with the
next patch version.
