# General Coding Rules

These apply to every project.
A repo's own `AGENTS.md` / `CLAUDE.md` adds repo-specific guidance on top of these.

## Code

- Prefer quality, simplicity, robustness, scalability, and long-term maintainability.
- Keep a single code path for doing any one thing.
- Review code for simplicity.
- If something looks off, fix it along the way, even if it is unrelated to the current task.
- Do not estimate engineering effort in human time scales. Coding as an agent is fast and cheap.

## Bug fixes

- A CI failure or production bug means there is an integration test gap.
- Reproduce the bug first, on edge / as close to real production usage as possible. This makes sure you find the real problem so your fix actually solves it.
- Write the failing test that proves the bug is reproduced, then write the fix.

## Testing

- Write integration tests that mimic the real user experience as closely as possible.
- When a feature's correctness depends on a real external dependency, prefer an e2e test that exercises the REAL dependency, not only a stub or mock. This is a judgement call per feature (usually the implementor's, and the implementor is usually the agent): not every feature warrants one - add it when the real dependency is where the risk actually lives.
- When working on tests, review the existing tests and keep coverage complete but non-redundant.
- When end-to-end testing a product UI, be picky about styling. It should look perfect.

## Proof of Work

- Every changeset requires a proof-of-work checkpoint attached to its PR: concrete evidence that the change works. Passing tests, a demonstrated working feature, and UI screenshots all count. State what you verified and how.
- Unit tests matter, but end-to-end integration tests better prove a feature actually works.
- Any PR that adds or changes an API surface (endpoint, RPC command, interface method, event payload) must show the new wire shape in the PR description for review.
  An example JSON response/payload taken from the real implementation is sufficient; follow it with one-line field semantics (units, vocabularies, null conventions) where they are not self-evident.
- Any PR that changes frontend code must attach a visual of the affected UI, captured from the real running app (not mockups).
- Prefer a short GIF walkthrough of the flow over still screenshots when the toolchain is available: it proves the feature actually works, not just a frozen state. Record headless with Playwright (`record_video_dir`) driving the real app, then transcode the `.webm` to GIF with ffmpeg (two-pass palette for quality). Fall back to stills when Chromium or ffmpeg is unavailable, and if even headless capture is impossible (a minimal runtime container with no browser), describe what you verified and attach test output.
- This toolchain is a dev/CI dependency, not a runtime one: it lives where PR work happens (the `[dev]` extra plus `playwright install chromium`, and CI runners that ship ffmpeg), not in minimal production images. Do not assume it is present; check, and degrade as above.
- Attach the captures (GIF or PNG) even when headless, using a git-hosted raw-URL strategy (GitHub's native upload needs a browser):
  - Host them on a throwaway orphan branch named `qa-assets`, using the same branch name in every repo. It is never merged, so the main tree stays image-free.
  - Build and push that branch with git plumbing (`hash-object -w`, `mktree`, `commit-tree`, `update-ref`), so no working tree or index is touched. The branch is disposable and can be deleted after merge.
  - Name files by PR or feature so one branch holds many PRs' assets without collision (e.g. `734-spend-flow.gif`).
  - Embed them in the PR body as `![alt](https://raw.githubusercontent.com/<owner>/<repo>/qa-assets/<file>)`, and verify each URL returns `200` with an `image/*` content type first. A GIF embeds inline this way; a raw `.webm`/`.mp4` does not, so convert to GIF.
- The raw-URL strategy renders inline only for public repos. GitHub's image proxy cannot fetch a private repo's raw URLs, so for a private repo attach via a browser upload when one is available, else state in the PR that inline captures were not possible headless.

## Markdown and writing

- Never use the em dash "—". Use a plain dash "-" instead.
- Prefer concise, to-the-point wording. Output the minimum understandable words.
- When writing or substantially editing long Markdown, put each full sentence on its own line. Preserve normal Markdown structure, but avoid wrapping bullet sentences onto one physical line.

## Commits and releases

- Never auto-add your agent name as co-author in commit messages.
- Never manually modify CHANGELOG.md, VERSION, or any other auto-generated or release-managed files, unless the task is explicitly a release.
