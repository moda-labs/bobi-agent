# gstack

Headless-browser QA and dogfooding toolbox. Drives a real Chromium via
Playwright to load pages, interact, screenshot, and verify state — for testing a
deployed site, reproducing a bug with evidence, or attaching UI proof to a PR.

Installed at `~/dev/gstack`, with its skills linked under `~/.claude/skills/`
(and `~/.codex/skills/`) under a `gstack-` namespace: `gstack-browse` and
`gstack-qa` are the core pair, alongside the `gstack-plan-*-review` and
`gstack-office-hours` planning helpers. The compiled browse daemon is
`~/.claude/skills/gstack/browse/dist/browse`.

gstack is a browser-QA toolbox here, not a lifecycle. Its upstream
shipping/reviewing/landing/second-opinion skills are removed at install time:
the engineering lifecycle is owned by the team's own skills and guides. Do
not look for gstack equivalents.

## Check it's available

```bash
test -e ~/.claude/skills/gstack-browse/SKILL.md && \
  test -e ~/.claude/skills/gstack-qa/SKILL.md && \
  test -x ~/.claude/skills/gstack/browse/dist/browse && echo ok
```

This is the quick probe; the authoritative contract (including the removed
lifecycle skills staying absent) is the dependency's `success` check, which
`bobi doctor` runs.

## Use it

Invoke the skills the normal way (e.g. the `gstack-browse` skill to open a URL
and get a screenshot, `gstack-qa` to test a flow end-to-end). See each skill's
`SKILL.md` under `~/.claude/skills/` for its commands.

## Notes

- gstack drives Chromium; on a host that enforces the AppArmor
  unprivileged-userns restriction, the sandbox needs
  `kernel.apparmor_restrict_unprivileged_userns=0` set on the host.
- The install is version-pinned. Bump the pins in the catalog entry
  (`bobi/tool_library/gstack/tool.yaml`) deliberately, not in a team.
