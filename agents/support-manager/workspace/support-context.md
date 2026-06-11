# Support context — TEMPLATE

Domain context for the `support-manager` agent. The role prompt stays
generic and reads every product-specific value from here. The support
manager reads this on startup and seeds it into the `support` knowledge
base.

`modastack install` seeds this file into your project's
`workspace/support-context.md` (existing copies are kept). **Fill it in
there** — that filled copy is yours and is not part of the pack.

Secrets are NOT stored here. API keys live in `.modastack/.env` (install
prompts for the `${VAR}`s declared in `agent.yaml`): `$SLACK_BOT_TOKEN`,
`$LINEAR_API_KEY`, `$VENN_API_KEY`, and `$POSTHOG_API_KEY` /
`$POSTHOG_HOST` / `$POSTHOG_PROJECT_ID`.

> Anything in ALL-CAPS or wrapped in <angle brackets> is a fill-in. The
> pack will not behave correctly until these are real values.

---

## What we support

<PRODUCT_NAME> — <one-paragraph description: what it is, who uses it, and
what "broken" looks like to a user (the surfaces/flows that, when they
fail, are real issues)>.

## The codebase

Where the support manager takes its high-level look when investigating a
signal. Read-only — the support manager never edits code.

- **Local path**: <ABSOLUTE_PATH_TO_REPO> (a read-only clone kept on the
  main branch; refresh with `git -C <path> pull --ff-only` before
  investigating — do NOT point this at a working tree you develop in)
- **Stack / entry points**: <languages, frameworks, and the few directories
  where most user-facing logic lives, so investigation starts in the right
  place>
- **Logs / observability**: <where runtime errors live — e.g. PostHog error
  tracking, Sentry — and how the agent reaches them>

## Linear — where tickets go

A real issue becomes a Linear ticket created so the engineering team's agent
(e.g. the eng-team pack) auto-picks it up. The handoff is the labeled
ticket; match the team + label to wherever your engineering agent watches.
The agent authenticates with `$LINEAR_API_KEY` (from `.modastack/.env`).

- **Team**: <TEAM_NAME>
- **Team key**: <KEY> (the issue-ID prefix, e.g. `ENG`)
- **Team UUID**: <TEAM_UUID> (resolve once via the Linear API — see
  `tools/linear.md`)
- **Trigger label**: agent (UUID <AGENT_LABEL_UUID>) — must match the
  engineering pack's `trigger_labels`
- **Initial state**: <STATE_NAME> (UUID <STATE_UUID>; a Triage or
  unstarted-type state the engineering agent picks up)
- **Priority mapping**: crash or data loss affecting many real users ->
  Urgent (1); broken core flow / clear regression -> High (2); degraded but
  usable, or low blast radius -> Normal (3); cosmetic or rare -> Low (4).

## PostHog — what counts as a signal

Read-only via the PostHog API (see `tools/posthog.md`), using
`$POSTHOG_API_KEY` (scope it to this one project, `error_tracking:read` +
`query:read`), `$POSTHOG_HOST`, `$POSTHOG_PROJECT_ID` from `.modastack/.env`.

- **Host**: <https://us.posthog.com or your region/self-hosted API host>
- **Project ID**: <NUMERIC_PROJECT_ID from PostHog -> Project Settings>
- **Watch**: error tracking (exceptions) is always in scope. Plus a
  watchlist of event/insight spikes worth flagging — replace with your real
  event names:
  - <inbound webhook / integration failures — what a spike means>
  - <core-flow failures — the events that mark your product's main loop>
  - <sign-up / onboarding / billing errors>
- **Noise to ignore**: <known-benign error signatures, test/staging/preview
  traffic, bots/crawlers, expected user-cancel paths — list specifics as you
  learn them so the agent does not file them>.

## Email — the support inbox

Read through the Venn Gmail MCP (see `tools/gmail.md`).

- **Inbox / address**: <support@yourproduct.com>. Isolate support mail with
  the Gmail query `to:<support-address> newer_than:1d`.
- **Gmail instance**: <if multiple Gmail accounts are connected via Venn,
  the `server_id` of the support inbox, e.g. `gmail-support`>
- **What's a bug vs not — filter in two steps.** First decide if it's even a
  real customer support email; only then decide bug vs not-a-bug.

  **Step 1 — is it from a real customer about the product?** Drop everything
  else at this gate (do not emit a signal):
  - **Vendor / sales / marketing outreach** — cold growth/partnership/SEO
    pitches from company domains. They address the product as a business and
    report no broken experience.
  - **Automated / no-reply notifications** — receipts, product promos,
    security/login notices from `no-reply@` / `*-noreply@` addresses.
  - Keep only **first-person mail from an individual** describing their own
    experience with the product ("I/we can't…", a personal From address,
    names their own account).

  **Step 2 — bug vs not-a-bug** (only for mail that passed Step 1):
  - **Real issue (file it):** something in the product is broken for the
    user — a core flow failing, a message/notification never arriving, an
    upload/processing step failing, sign-up/onboarding broken, or
    **paid/charged but can't access or use the product**.
  - **Not a bug (log as not-real, with the category):** how-to questions,
    feature requests, quality *opinions* (vs something that failed to
    generate), and pure billing/refund *questions*. "Reset my password" is
    user-support unless it reveals a broken flow.
  - **Important nuance:** "paid but no access / charged but locked out" is a
    **real** issue, NOT a billing question — the user paid and the product
    didn't deliver (a broken entitlement flow). Treat billing as not-a-bug
    only when it's a pure question with no broken experience.

## Slack — where we post

Per-issue alerts and the daily report both go here (`$SLACK_BOT_TOKEN`).

- **Workspace**: <WORKSPACE_ID> (e.g. T0XXXXXXX)
- **Channel**: <CHANNEL_ID> (e.g. C0XXXXXXX — invite the `modastack` bot to
  it. If the bot token lacks `channels:read`, set the raw ID, not the name.)

## Daily report

- **When**: the `daily-report` monitor fires every 24h from agent start.
  Adjust the interval in `.modastack/monitors.yaml` if you want a different
  cadence (intervals are s/m/h/d only — no time-of-day).
- **Contents**: every issue triaged in the window, real and not-real, with
  the verdict, one-line summary, and ticket link for the real ones.

## Voice

- Lead with what's broken and the blast radius, not preamble.
- Specific over vague: name the error, the file, the affected surface.
- No em dashes. No filler. Never close on a summary.
