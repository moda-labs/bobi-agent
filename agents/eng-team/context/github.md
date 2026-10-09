# GitHub Context

Use GitHub as the default tracker and PR source unless an overlay configures a
different tracker.

- Issues assigned to the team or labeled `agent` route to `issue-lifecycle`.
- Pull request reviews, inline review comments, and PR comments route to
  `pr-feedback` only for fleet-authored PRs when they contain actionable requested-change text.
  Human-authored PR feedback reaches the director without launching an
  address-phase worker. Question-only PR or issue comments must be answered directly.
- Closed pull requests route to `pr-closed`, which re-reads the PR's merge
  state and then cleans up the worktree, branch, and linked issue. A PR closed
  without merging keeps its branch and worktree for a human decision.
- CI failures on open pull requests route to `build-failure`.
- Include owner/repo references and URLs in worker tasks so workers can fetch
  source context directly.
