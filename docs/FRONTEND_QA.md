# Frontend QA

The `bobi app` unified web app, the `bobi setup` UI, and `bobi agent <name> ui`
are local-only vanilla web UIs. They do not have a hosted preview deployment in
normal PRs. Do not treat the absence of a hosted preview URL as a QA blocker
for changes limited to these local surfaces.

For changes under `bobi/webapp/`, `bobi/setup/webui/`, `bobi/agentui/`,
`bobi/webui_common/`, or other code that changes local UI routes, static
mounting, nonce/token checks, or Host-guard behavior, run the local Playwright
e2e suite instead:

```bash
pytest tests/e2e/test_setup_ui.py tests/e2e/test_agent_ui.py
```

These tests boot the real FastAPI apps on loopback with fake deterministic
backends, then drive Chromium through the same nonce, token, and Host-guard paths
used by the CLI-launched UIs. If the diff is confined to `bobi/setup/webui/`,
run `tests/e2e/test_setup_ui.py`. If the diff is confined to `bobi/agentui/`,
run `tests/e2e/test_agent_ui.py`. For shared paths such as `bobi/webui_common/`
or cross-cutting server/security changes, run both files.

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
`$BOBI_HOME`. Open the printed localhost URL, exercise the changed flow, and
capture any screenshots from that local browser session. For every PR that
touches these frontend surfaces, attach the local QA screenshots to the PR with
concise context. For PRs that require local UI QA, treat missing local
prerequisites, browser launch failures, or failing e2e coverage as QA blockers
and report the exact missing prerequisite. Treat a missing hosted preview as
expected for these local-only UIs.
