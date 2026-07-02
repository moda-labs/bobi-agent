# gstack

Headless-browser QA and dogfooding toolchain. Drives a real Chromium via
Playwright to load pages, interact, screenshot, and verify state — for testing a
deployed site, reproducing a bug with evidence, or reviewing a UI change.

Installed at `~/dev/gstack`, with its skills linked under `~/.claude/skills/`
(the `browse`, `qa`, `ship`, and `review` skills). The compiled browse daemon is
`~/.claude/skills/gstack/browse/dist/browse`.

## Check it's available

```bash
test -e ~/.claude/skills/browse/SKILL.md && \
  test -x ~/.claude/skills/gstack/browse/dist/browse && echo ok
```

## Use it

Invoke the skills the normal way (e.g. the `browse` skill to open a URL and get a
screenshot, `qa` to test a flow, `review` for a pre-landing diff review). See each
skill's `SKILL.md` under `~/.claude/skills/` for its commands.

## Notes

- gstack drives Chromium; on a host that enforces the AppArmor
  unprivileged-userns restriction, the sandbox needs
  `kernel.apparmor_restrict_unprivileged_userns=0` set on the host.
- The install is version-pinned. Bump the pins in the catalog entry
  (`bobi/tool_library/gstack/tool.yaml`) deliberately, not in a team.
