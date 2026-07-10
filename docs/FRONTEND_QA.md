# Frontend QA

The `bobi app` unified web app is the local-only vanilla web UI. `bobi setup`
and local `bobi agent <name> ui` are deep-link aliases into that app. These
routes do not have a hosted preview deployment in normal PRs. Do not treat the
absence of a hosted preview URL as a QA blocker for changes limited to these
local surfaces.

For changes under `bobi/webapp/`, `bobi/setup/webui/`, `bobi/webui_common/`, or
other code that changes local UI routes, static mounting, token checks, or
Host-guard behavior, run the local Playwright e2e suite instead:

```bash
pytest tests/e2e/test_setup_ui.py
```

These tests boot the real FastAPI apps on loopback with fake deterministic
backends, then drive Chromium through the same token and Host-guard paths used
by the CLI-launched UIs. For shared paths such as `bobi/webui_common/` or
cross-cutting server/security changes, also run the server and daemon unit
suites listed below.

If Playwright is unavailable in the environment, install the dev dependencies and
Chromium before retrying:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
```

For changes confined to `bobi/webapp/`, run its server and daemon unit suites
(`tests/test_webapp_server.py`, `tests/test_webapp_daemon.py`) and smoke-test
via `bobi app start`.

Use manual loopback smoke testing when behavior is not covered by the e2e tests:

```bash
bobi app start
bobi setup
bobi agent <name> ui
```

For `bobi agent <name> ui`, use an agent that is already installed in the local
`$BOBI_HOME`; the command opens `bobi app` at `#/agents/<name>`. Open the printed
localhost URL, exercise the changed flow, and capture any screenshots from that
local browser session. For every PR that touches these frontend surfaces, attach
local QA proof to the PR with concise context (prefer a GIF walkthrough, else
screenshots; see "Attaching UI proof headless" below). For PRs that require
local UI QA, treat missing local prerequisites, browser launch failures, or
failing e2e coverage as QA blockers and report the exact missing prerequisite.
Treat a missing hosted preview as expected for these local-only UIs.

## Attaching UI proof headless

Prefer a short GIF walkthrough of the changed flow over still screenshots: it
proves the feature works, not just a frozen state. Record it headless with
Playwright (`record_video_dir`) driving the real app, then transcode the `.webm`
to GIF with ffmpeg (two-pass palette for quality). Fall back to stills only when
Chromium or ffmpeg is unavailable.

GitHub's native image upload needs a browser, which is unavailable in a headless
agent container. Attach the capture via the git-hosted raw-URL strategy instead
(this repo is public, so raw URLs render inline):

- Host the file on a throwaway orphan branch named `qa-assets` (never merged, so
  `main` stays image-free). Build it with git plumbing (`hash-object -w`,
  `mktree`, `commit-tree`, `update-ref`) so no working tree or index is touched;
  the branch is disposable and can be deleted after merge.
- Name files by PR so one branch holds many PRs' assets (e.g. `734-spend-flow.gif`).
- Embed in the PR body as
  `![alt](https://raw.githubusercontent.com/moda-labs/bobi-agent/qa-assets/<file>)`,
  and verify each URL returns `200` with an `image/*` content type first. A GIF
  embeds inline this way; a raw `.webm`/`.mp4` does not, so convert to GIF.

This is the repo-agnostic convention from the core `~/AGENTS.md` "Proof of Work"
rule; see there for the private-repo caveat.
