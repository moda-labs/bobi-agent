# Release Runbook

This is the production bugfix release recipe for `bobi` plus the Moda
agent-team fleet. Use it when a fix has merged to `main` and needs to reach the
Fly-hosted agents.

> **Repo split:** this repo releases the PUBLIC product only (wheel, PyPI,
> Homebrew). The deploy side - `bobi-deploy` package, container image, Fly
> fleet canary, Cloudflare Worker deploy - lives in the private
> `moda-labs/bobi-deploy` repo and releases through ITS train, pinned to the
> public release. `bobi-deploy` is never published to PyPI (the name is held
> by a defensive stub that fails loudly at install); distribution is the
> private channel only. A `uv tool install bobi` has no `bobi deploy`; that
> is the product line. Before the first private-channel bobi-deploy release:
> raise its `bobi>=` floor to the first public release with the carve-out
> seams (0.40.0 satisfies the pin but predates bobi.build/bobi.config, and
> its CLI still mounts a built-in `build` command that silently shadows the
> plugin's entry point - built-ins win in bobi.cli's plugin group).

> **Dev channel (#740 Track A):** every fully-green push to `main`
> fast-forwards the `dev` branch (the `promote-dev` job in `ci.yml`).
> `bobi-deploy`'s CI/staging track `dev` — NOT a released tag — so merging
> public work is enough for private CI to build against it; no release is
> needed per feature. A formal release (this runbook) is required only for
> the production cut: the private release train pins the exact published
> `bobi==<version>` from PyPI at dispatch time.

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
  npm test -- --run test/core.spec.ts
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

The public release workflow must go green:

- subscription-login smoke
- release wheel build (with the import/`--version` sanity check)
- PyPI publish
- Homebrew formula bump + bottle-URL smoke

Then run the PRIVATE release train in `moda-labs/bobi-deploy` (its runbook
lives there): event-server Worker deploy + identity check, fleet canary
build/smoke against the just-published wheel, GHCR base image publish
(`ghcr.io/moda-labs/bobi:<version>`). Do not continue to the Moda fleet pin
until BOTH trains are green - the public train alone carries no functional
fleet proof.

If PyPI was just published, allow a short propagation delay before installing
the new version from another repo.
(github.com/orgs/moda-labs/packages) so consumers can pull without a token;
visibility persists across releases.

Spot-check the published base image AS A CONSUMER - log out of GHCR first so
the pull proves anonymous access works (a logged-in maintainer pull succeeds
even while the package is still private):

```bash
docker logout ghcr.io
docker run --rm --entrypoint bobi ghcr.io/moda-labs/bobi:<version> --version
```

The full run contract is in the private deploy repo's
`docs/CONTAINERIZED_DEPLOYMENT.md` (repo split).

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
release version/SHA that the private deploy repo's release train deployed.

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

The validation message doubles as the fleet's release announcement, so write
it for the humans in the channel: lead with a plain-language changelog and
tuck the machine-checkable marker at the end. Derive the highlights from this
release's `CHANGELOG.md` entry, rewritten as what changed for the people
using the team - not commit-log or PR jargon.

```bash
MARKER="ENG_TEAM_E2E_$(date -u +%Y%m%d_%H%M%S)"
printf '%s\n' "$MARKER" > /tmp/bobi-engteam-marker

TEXT=$(cat <<EOF
:rocket: *bobi <version> is live on this fleet*

What's new:
• <highlight 1, plain language - e.g. "you can now search cold memories with the recall-memory command">
• <highlight 2>
• <highlight 3>

<@U0BCVME6Z60> quick post-deploy check: reply in this thread with ${MARKER} OK to confirm you're receiving events on the new build.
EOF
)
export TEXT

venn --json tools execute \
  -s moda-labs-slack \
  -t chat_postMessage \
  --confirm \
  -a "$(python3 -c 'import json, os; print(json.dumps({"channel": "C0BAEN48KQR", "text": os.environ["TEXT"], "link_names": True}))')"
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
