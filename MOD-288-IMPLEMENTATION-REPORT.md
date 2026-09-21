# MOD-288 Implementation Report

Status: verified local implementation, not published

Date: 2026-09-21

Branch: `fix/MOD-288-local-service-supervision`

Base: `origin/main` at `8ac368f04cc8a090695e60a883c8fcaac5a1b70c`

## Outcome

MOD-288 is implemented locally as an opt-in user service for macOS and Linux.
The generated service runs Bobi's shipped supervisor, starts with the user's
service session, and lets the operating system restart the supervisor after a
failure. Bobi's `stop` and `restart` commands now cooperate with the selected
service manager.

Nothing has been pushed. No pull request, issue, service, credential, or
production resource was created or changed.

## Authority and Scope

The implementation contract was derived from these sources:

- [Linear MOD-288](https://linear.app/moda-labs/issue/MOD-288/local-deployments-have-no-process-supervision-manager-never-restarts)
  is the product authority. It is currently In Progress and links to GitHub
  issue #869.
- [GitHub #869](https://github.com/moda-labs/bobi-agent/issues/869) is the
  technical umbrella. Its 2026-08-03 maintainer update says the supervisor now
  ships in the wheel and narrows the remaining problem to OS service-manager
  integration.
- [GitHub #925](https://github.com/moda-labs/bobi-agent/issues/925) requires
  launchd lifecycle parity so `stop` and `restart` do not fight a macOS
  `KeepAlive` service.
- [GitHub #926](https://github.com/moda-labs/bobi-agent/issues/926) requires a
  CLI primitive that generates local macOS and Linux services. Its maintainer
  discussion recommends implementing #925 and #926 together because service
  generation is unsafe on macOS without lifecycle delegation.
- [GitHub #927](https://github.com/moda-labs/bobi-agent/issues/927) rejected
  self-supervision of the separate local event-server process. It is excluded.
- [GitHub #928](https://github.com/moda-labs/bobi-agent/issues/928) covers
  in-place upgrade staleness and was completed separately. It is excluded.
- [GitHub #933](https://github.com/moda-labs/bobi-agent/issues/933) covers
  lifecycle event journaling and was closed as not planned. It is excluded.

The implementation therefore covers only the remaining #925 and #926 work:

1. Generate and operate a user-level systemd unit on Linux.
2. Generate and operate a user-level LaunchAgent on macOS.
3. Make Bobi lifecycle commands cooperate with those generated services.
4. Document the operator commands.

## Problem Before This Change

Bobi already shipped the supervisor command:

```bash
bobi agent <name> supervise -- --foreground
```

That command can supervise and relaunch the manager, but a normal local
`bobi agent <name> start` launch does not invoke it. It starts the manager as a
detached process. The supervisor therefore was not automatically started at
login or boot on a local machine.

Linux had a limited lifecycle seam for an operator-authored `bobi.service`, but
Bobi did not generate the unit. macOS had neither LaunchAgent generation nor
launchd-aware `stop` and `restart` behavior.

The observable consequences were:

- A local manager could remain dead after an unexpected exit.
- A local manager did not automatically return with the user's service
  session.
- There was no supported Bobi command to install the shipped supervisor as an
  OS-managed local service.
- Adding a macOS `KeepAlive` service without lifecycle delegation would make
  `bobi agent <name> stop` fight launchd and immediately respawn the process.

## Implementation Contract

### Required behavior

- `bobi agent <name> install-service` writes and starts a user-level service.
- Linux writes `~/.config/systemd/user/bobi.service`.
- macOS writes
  `~/Library/LaunchAgents/com.moda-labs.bobi.plist`.
- Both services execute
  `bobi agent <name> supervise -- --foreground`.
- The service receives the selected runtime root as its working directory and
  receives `BOBI_HOME` and `PATH` explicitly.
- stdout and stderr go to the existing manager log.
- Linux uses `Restart=on-failure` and `RestartSec=30`.
- macOS uses `RunAtLoad`, `KeepAlive`, and `ThrottleInterval=30`.
- Reinstalling the service is safe and applies the newly generated definition.
- `bobi agent <name> uninstall-service` stops and removes the generated
  service.
- Normal `stop` delegates only to a service that is currently active.
- `restart` delegates when a generated service exists, including a stopped
  LaunchAgent that must be bootstrapped again.
- `stop --force` preserves the direct process-signal escape hatch.
- Existing direct start, stop, and restart behavior remains available when no
  generated service exists.

### Safety and compatibility constraints

- Installation and removal are user-level only. Running either as root is
  rejected so the command does not create a root-owned service in a user's
  service location.
- Service definitions are written atomically with Bobi's shared
  `atomic_write_text` durability helper.
- The existing Linux service name, `bobi.service`, is preserved.
- Service-manager failures become CLI errors; the CLI does not print a false
  success message.
- No credentials are copied into the unit or plist.
- The feature is opt-in. Installing or upgrading Bobi does not automatically
  create a service.
- `VERSION` and `CHANGELOG.md` are unchanged.

## File-by-File Changes

### `bobi/service_manager.py`

This new module owns the OS-specific service contract.

- Defines the stable names `bobi.service` and `com.moda-labs.bobi`.
- Resolves the installed `bobi` executable. If it is not on `PATH`, it falls
  back to the current Python interpreter with `-m bobi.cli`.
- Builds the supervisor argument vector once for both platforms.
- Renders a systemd user unit with safe shell quoting.
- Renders a LaunchAgent plist with `plistlib`, avoiding hand-written XML.
- Includes `~/.local/bin` and the current process `PATH`, with duplicate path
  entries removed.
- Detects whether a service is active separately from whether its generated
  definition exists.
- Maps lifecycle actions to `systemctl --user` and `launchctl`.
- Parses the managed PID for the restart success message.
- Installs, reloads, starts, restarts, stops, disables, and removes the service
  through one platform boundary.
- Converts missing binaries, timeouts, and non-zero service-manager results
  into actionable errors.

The active/configured distinction is important:

- `active_manager()` answers whether `stop` should delegate now.
- `configured_manager()` answers whether `restart` should use the generated
  service even if it is stopped.

This prevents a stopped service definition from breaking the old direct stop
fallback while still allowing `restart` to bring that service back.

### `bobi/cli.py`

The CLI now exposes and consumes the service-manager boundary.

- Adds `bobi agent <name> install-service`.
- Adds `bobi agent <name> uninstall-service`.
- Registers both only under the agent-scoped command group.
- Routes normal `stop` through an active systemd or launchd service.
- Keeps `stop --force` on the existing direct `stop_team` path.
- Routes `restart` through a generated systemd or launchd service.
- Preserves `restart --fresh`: Bobi clears the manager session before asking
  the OS service manager to restart the supervisor.
- Reports the service PID when available and `unknown` otherwise.
- Converts service operation failures to Click errors and does not claim that
  a failed restart succeeded.

### `tests/test_service_manager.py`

This new focused suite covers the service contract without mutating the host:

- Exact supervisor command in both generated definitions.
- systemd restart policy, environment, and log destination.
- launchd label, argument vector, keepalive policy, throttle, and environment.
- Idempotent Linux install and restart of an existing service.
- Reload of an existing macOS LaunchAgent.
- Root-owned installation refusal.
- Restart of an installed but unloaded LaunchAgent via `bootstrap`.
- The active/configured distinction for an unloaded LaunchAgent.
- Linux uninstall order: disable and stop, remove, then daemon reload.
- launchd-aware stop delegation.
- `stop --force` bypass behavior.
- launchd-aware restart and PID reporting.
- Failure propagation without a false success message.
- CLI install and uninstall command routing.

### `tests/test_cli.py`

The agent command help assertion now requires `install-service` and
`uninstall-service`, preventing the commands from being accidentally removed
from the public CLI surface.

### `docs/QUICKSTART.md`

The local deployment section now explains that an always-on macOS or Linux
machine can install the user service, what command it runs, and how to remove
it.

### `skills/bobi.md`

The command reference now lists the install and uninstall commands alongside
the existing agent lifecycle commands.

## Generated Service Behavior

The exact executable, agent name, runtime paths, and environment values are
resolved at installation time. The following examples use placeholders.

### Linux systemd user unit

Path:

```text
~/.config/systemd/user/bobi.service
```

Representative output:

```ini
[Unit]
Description=Bobi Agent my-agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/absolute/path/to/bobi agent my-agent supervise -- --foreground
WorkingDirectory=/home/me/.bobi/agents/my-agent/run
Environment=BOBI_HOME=/home/me/.bobi
Environment=PATH=/home/me/.local/bin:...
StandardOutput=append:/home/me/.bobi/agents/my-agent/run/state/manager.log
StandardError=append:/home/me/.bobi/agents/my-agent/run/state/manager.log
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Installation executes these operations:

```bash
systemctl --user daemon-reload
systemctl --user enable --now bobi
```

If `bobi.service` was already enabled, installation also restarts it so the
new definition takes effect.

Removal executes:

```bash
systemctl --user disable --now bobi
systemctl --user daemon-reload
```

The unit file is removed between those two operations.

### macOS LaunchAgent

Path:

```text
~/Library/LaunchAgents/com.moda-labs.bobi.plist
```

Representative properties:

```xml
<key>Label</key>
<string>com.moda-labs.bobi</string>
<key>ProgramArguments</key>
<array>
  <string>/absolute/path/to/bobi</string>
  <string>agent</string>
  <string>my-agent</string>
  <string>supervise</string>
  <string>--</string>
  <string>--foreground</string>
</array>
<key>RunAtLoad</key>
<true/>
<key>KeepAlive</key>
<true/>
<key>ThrottleInterval</key>
<integer>30</integer>
```

The full plist also contains the runtime working directory, `BOBI_HOME`,
`PATH`, and manager log paths.

Installation bootstraps the plist into the current user's GUI domain:

```bash
launchctl bootstrap gui/$(id -u) \
  "$HOME/Library/LaunchAgents/com.moda-labs.bobi.plist"
```

If the LaunchAgent is already loaded, installation writes the replacement
plist, performs `bootout`, and bootstraps it again.

Normal restart uses:

```bash
launchctl kickstart -k "gui/$(id -u)/com.moda-labs.bobi"
```

If the plist exists but is not loaded, Bobi uses `bootstrap` instead. Removal
boots out a loaded job and deletes the plist.

## Operator Instructions

### Install and start supervision

```bash
bobi agent my-agent install-service
```

Expected result:

- Linux: the command prints the generated `bobi.service` path.
- macOS: the command prints the generated plist path.
- The OS service manager starts the supervisor immediately.
- The supervisor starts and watches the manager.
- The manager log remains at the existing Bobi manager log path.

### Check the installed service

Linux:

```bash
systemctl --user status bobi
systemctl --user show bobi --property=MainPID --value
journalctl --user-unit bobi
```

macOS:

```bash
launchctl print "gui/$(id -u)/com.moda-labs.bobi"
```

For both platforms, also use Bobi's own commands:

```bash
bobi agent my-agent status
bobi agent my-agent doctor
```

### Stop and restart

```bash
bobi agent my-agent stop
bobi agent my-agent restart
bobi agent my-agent restart --fresh
```

Behavior after installation:

- `stop` asks the active OS service manager to stop the service.
- `restart` asks the configured OS service manager to restart it.
- `restart --fresh` clears saved manager session state before restarting.
- `stop --force` bypasses service-manager delegation and directly signals the
  manager. Because a configured service may still have a restart policy,
  `--force` is an emergency process escape hatch, not the normal way to keep a
  supervised service stopped.

### Remove supervision

```bash
bobi agent my-agent uninstall-service
```

This stops and removes the generated user service. It does not uninstall Bobi,
delete the agent package, or remove agent state.

## Before and After

| Scenario | Before | After |
|---|---|---|
| Install local OS supervision | Manual unit authoring only | One Bobi command generates and starts it |
| Linux manager failure | Only supervised if operator supplied a compatible unit | Generated unit runs Bobi supervisor and restarts on failure |
| macOS manager failure | No supported launchd integration | Generated KeepAlive LaunchAgent runs Bobi supervisor |
| User session starts | Bobi manager is not restored automatically | Installed user service starts with the service session |
| Normal stop under macOS KeepAlive | Direct stop can be undone by launchd | Bobi delegates stop to launchd |
| Restart of stopped macOS service | No Bobi path | Bobi bootstraps the installed plist |
| No service installed | Existing direct lifecycle | Existing direct lifecycle remains the fallback |

## Verification Evidence

All verification ran in the isolated task worktree. Service-manager calls were
stubbed in focused tests, so the suite did not install or remove a real host
service.

| Verification | Result |
|---|---|
| `pytest tests/test_service_manager.py tests/test_cli.py -q --timeout=30` | 101 passed |
| `pytest tests/test_import_boundaries.py tests/test_packaging.py -q --timeout=30` | 64 passed |
| `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` | 5394 passed, 11 skipped |
| `pytest tests/integration/test_manager_lifecycle.py::TestManagerStartStop -q -k stub --timeout=120` | 8 passed, 8 deselected |
| `git diff origin/main...HEAD --check` | Passed before this report commit |
| `codegraph sync` | Completed after the implementation changes |

The pytest runs emitted existing temporary-directory cleanup warnings. There
were no product test failures.

The integration proof used the repository's public stub brain in an isolated
`BOBI_HOME`. A real Claude session was not required because this change affects
process lifecycle and OS command routing, not model behavior.

## Local Commits

| Commit | Subject | Purpose |
|---|---|---|
| `3339b4107ac8445847ad066e5b1fd29d56b17a12` | `fix: add local service supervision for MOD-288` | Service generation, lifecycle delegation, docs, and tests |
| `487639cf967ba6d8995e2cd02edf5581882cf144` | `test: cover local service removal` | Linux uninstall regression proof |
| `ef1bd60df897c525a2362deea09d424aadaf953e` | `fix: preserve no-service lifecycle fallback` | Separate active and configured manager detection |

The commit containing this report is documentation-only and follows those
implementation commits.

## Limitations and Risks

- No real host service was installed, removed, or reboot-tested. The generated
  definitions and command sequences are covered deterministically; final
  platform smoke testing is still appropriate before release.
- The implementation manages one stable `bobi` service per user. Installing a
  service for another agent replaces that user's generated definition. This
  preserves the pre-existing `bobi.service` compatibility contract rather than
  introducing a multi-service naming migration.
- The service supervises the Bobi manager through the shipped supervisor. It
  does not supervise the separate local event-server process.
- `KeepAlive=true` on macOS expresses persistent service intent. A forced
  direct manager kill may be repaired by the supervisor or launchd; use normal
  `stop` or `uninstall-service` to intentionally keep it down.
- The generated service captures the resolved Bobi executable, `BOBI_HOME`,
  runtime root, and `PATH` at install time. Re-run `install-service` after
  moving the installation or changing those locations.
- This change does not add upgrade-staleness notifications, lifecycle event
  journaling, or new deployment policy.
- The branch is still based on the current fetched `origin/main`, but it has
  not passed remote CI or independent PR review because it has not been
  published.

## Repository State at Report Creation

- Worktree:
  `/Users/zodinet17/workspace/moda-bobi/resources/bobi-agent/worktrees/MOD-288-local-service-supervision`
- Branch: `fix/MOD-288-local-service-supervision`
- Implementation head before this report: `ef1bd60df897c525a2362deea09d424aadaf953e`
- Distance from fetched `origin/main`: 3 commits ahead, 0 behind.
- Original checkout: not modified by this task continuation.
- External state: no push, PR, issue update, service mutation, or credential
  mutation.

## What Remains

The requested local endpoint is complete once this report is committed and the
final worktree checks pass. Publication remains intentionally gated.

To publish later, an explicit user authorization is required for that turn.
The expected first push command is:

```bash
git push -u origin fix/MOD-288-local-service-supervision
```

After publication, the remaining workflow is:

1. Create a PR that links MOD-288 and closes the applicable GitHub issues.
2. Run required remote CI and, if desired, real macOS/Linux service smoke tests.
3. Obtain an independent current-head review verdict and human approval.
4. Land only with green required checks and the repository's landing gate.
5. Update and close the trackers only through the authorized landing workflow.
