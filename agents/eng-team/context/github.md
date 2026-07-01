# GitHub Context

Use GitHub as the default tracker and PR source unless an overlay configures a
different tracker.

- Issues assigned to the team or labeled `agent` route to `issue-lifecycle`.
- Pull request reviews, inline review comments, and actionable PR comments route
  to `pr-feedback`.
- Closed pull requests route to `pr-closed` for deterministic cleanup and issue
  closure when applicable.
- CI failures on open pull requests route to `build-failure`.
- Include owner/repo references and URLs in worker tasks so workers can fetch
  source context directly.
