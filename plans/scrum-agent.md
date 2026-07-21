# Scrum Coordination Agent (`scrum-team`)

> **Status:** Draft (design) — not yet scoped into build tickets
> **Owner:** Luke · **Created:** 2026-07-21 · **Last amended:** - (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

A coordination agent for a **3-person, globally-distributed, multilingual team where
roughly half the coding is done by coding agents**. Its job is not to run Scrum
ceremonies for their own sake — it is to keep **one shared, always-current picture of
state** and hand that picture off cleanly across timezones and languages, so the PM
can drive work forward with the engineers in a coordinated fashion.

The design deliberately **inverts the usual "standup bot" pattern**. It does not ask
humans to type status into a channel. It **derives** what changed from the tools the
team (and its coding agents) already emit events into, then asks humans only for the
judgment those events can't show. The differentiated primitive is a **follow-the-sun
handoff**, not a daily digest.

This is a strong fit for Bobi's event-driven architecture: most of "what changed" is
already on the event bus (commits, PRs, CI, tracker transitions, coding-agent run
completions), so the coordinator assembles state from events rather than from people.

## Design constraints (the team this is for)

- **3 people**, one of whom is the **PM** (a player-coach, in the trenches — not a
  distant manager). Scrum ceremony overhead sized for 7–9 person teams is mostly
  waste here.
- **~50% of coding work is done by coding agents.** Agent work has no voice in a
  standup; the coordinator must represent it. Story points / velocity are meaningless
  when an agent ships a feature in an afternoon.
- **Distributed across the world**, rarely awake at the same time. Async-first is
  mandatory; the timezone *handoff* is the core problem to solve.
- **Different native languages.** Translation is first-class — but the naive
  "auto-translate and hide the original" approach measurably backfires (see Principle 6).
- **Audience: the whole team, in service of the PM keeping work moving.** Resolved by
  full transparency — one shared view, no PM-only back-channel (Principle 7).

## Design principles (research-grounded)

Each principle is a decision, with the evidence behind it. Citations are to the
research gathered for this design (see References).

1. **Coordinate, don't report.** The canonical Scrum failure mode is the standup
   degrading into a status report to an authority figure; updates become generic
   ("worked on X-123, will work on X-129") with no blockers or sprint-goal risk. The
   agent optimizes for surfacing blockers, dependencies, and sprint-goal trend — never
   for compiling "what I did." [scrum.org-antipatterns, age-of-product, geekbot]

2. **Derive, don't ask.** If the agent asks humans to type what changed, it automates
   the anti-pattern and produces output that dies in a Slack channel — "nothing is
   queryable, no integration with your dev tools." Instead it derives "what changed"
   from tracker/PR/CI/agent events and asks humans only for judgment ("this PR's been
   open 3 days — stuck or fine?"). [standin, mindstudio]

3. **Follow-the-sun handoff is the core feature, not a daily digest.** When person A
   ends their day, the agent packages state for person B starting theirs. Explicit
   handoff protocols (what to include, where it lives, what "done" means) "prevent
   delays when someone wakes up to an ambiguous status update." Protect the small
   overlap window; no after-hours pings. [followthesun-wiki, trackingtime, em-tools]

4. **Facilitate judgment; never automate the number.** Estimation and prioritization
   get reference points and scope-creep flags, not authoritative story points — which
   are subjective, team-relative, and already broken by AI-assisted throughput.
   [scrum.org-forum, hutson, atlassian]

5. **Surface scope questions; humans decide.** Roadmap *progress tracking* is a strong
   automate; roadmap *generation / rescoping* is not — strategy needs human judgment,
   context, and trade-off empathy. AI is "an assistant, not an authority." The agent
   raises the flag with evidence and frames the trade-off. [pichler, productboard]

6. **Translate as an aid; preserve the source; flag nuance.** A peer-reviewed HCI study
   found recipients interpreted senders' social intentions *less accurately* — and
   rated the senders *less positively* — reading machine-translated messages than the
   same people's imperfect non-native English. Translate factual/state content freely
   (ticket moved, build failed); **flag** social/nuanced/conflict content as a
   translation to confirm, and always keep the original. Translation also compounds
   hallucination risk — a mistranslated blocker is a wrong fact stated confidently.
   [acm-crosslingual, arxiv-lostintranslation]

7. **Transparency keeps it coordination, not surveillance.** Because the agent serves
   the PM's coordination, the safeguard is that engineers see exactly what the PM sees —
   one shared picture, no PM-only report the engineers write *for*. The moment a
   PM-only view exists, the status-report anti-pattern returns, harder to detect in a
   distributed team. [scrum.org-antipatterns]

8. **Verify before asserting.** No claim without a traceable source artifact. AI
   note-takers routinely invent owners, deadlines, and action items; anything not tied
   to a real ticket/PR/message is treated as false until verified. This gate now covers
   translation too. [gotranscript, managednerds]

9. **Propose, don't execute (for anything with a blast radius).** "Who's accountable
   when the agent reassigns the wrong task?" is unanswered in the field, so it's
   answered by design: state changes (moving tickets, reassigning, editing the roadmap)
   are *proposed* with a one-click human confirm, logged as events, attributed, and
   reversible. [aidevdayindia]

10. **Right-size to 3 people.** No burndown theater, no velocity charts, no formal
    estimation ceremony. A tiny team abandons process-for-its-own-sake. Everything
    async-first; reserve synchronous time for genuine collaboration and retros.
    [em-tools, trackingtime]

## Non-goals (explicitly out of scope)

- Story points, velocity tracking, burndown charts.
- Autonomous roadmap generation or autonomous rescoping.
- Conflict resolution, mentoring, or removing political/organizational impediments —
  the agent **detects and routes** these to a human; it does not attempt them.
- Always-on retro "listening." A retro facilitation workflow may be added later, but
  an always-listening agent chills honest discussion and creates AI "trust ambiguity"
  (AI mistakes can't be challenged like human ones). [hbr-psychsafety]
- Being the source of truth for code. The tracker + repo remain canonical; the agent
  reads and reflects them.

---

## Shape in Bobi terms

A single persistent **coordinator** plus one bounded **synthesizer** worker, driven by
tracker/repo/chat events and a handful of time-based monitors (ceremonies are
scheduled). Handoffs between steps are explicit contracts. State the agent reasons
against lives in structured, agent-readable workspace files.

### Roles

- **coordinator** (persistent, entry point, human-facing). Receives events, maintains
  the shared state model, detects blockers and sprint-goal drift, answers questions in
  each person's language, and escalates human-only issues. Does the light, reactive,
  interactive work in real time. Proposes state changes; never executes silently.

- **synthesizer** (bounded worker, launched by scheduled workflows). Produces the
  token-heavier deliverables — the follow-the-sun handoff package, the weekly planning
  brief, the roadmap-progress report — each **verified against source events** and
  translated per recipient with the source preserved. Short-lived; writes a handoff and
  exits.

### Services & events

- **tracker** (Linear or GitHub Issues) — issue state, assignments, comments, epics/
  cycles. Source of sprint/epic truth.
- **github** — PR / commit / CI / review events; the substrate where coding-agent work
  shows up. Coding agents are attributed as team members (see `workspace/team.md`).
- **slack** — where humans talk to the coordinator (multilingual intake) and where
  handoffs/briefs are posted.

Deterministic `auto_dispatch` rules route the events that must always trigger a
workflow (e.g. a `changes_requested` review → `blocker-escalation`) before the
coordinator LLM sees them, per the eng-team pattern.

### Workflows (`workflows/*.yaml`)

- **`daily-handoff`** — the follow-the-sun "standup." Triggered per-person at their
  local end-of-day / start-of-day boundary (see `handoff-window` monitor). Steps:
  1. `gather` (synthesizer) — collect all events since this person's last handoff from
     tracker/PR/CI/agent-run sources. Derive-not-ask.
  2. `verify` (synthesizer) — drop any item not tied to a source artifact (Principle 8).
  3. `assemble` (synthesizer) — build the handoff to the contract below: what moved,
     what's now unblocked *for you*, what's awaiting *your* review, decisions needed,
     and the sprint-goal trend.
  4. `translate` (synthesizer) — render into the recipient's language, preserving the
     source; flag any nuanced/social item (Principle 6).
  5. `post` (notify) — post to the shared surface **and** direct the recipient, within
     their working hours only.

- **`sprint-planning`** — semi-continuous / weekly, interactive. Steps: synthesize last
  period's actuals from events → produce a facilitation brief tied to epics + sprint
  goal → post → `await` PM+Eng input (multilingual) → **propose** the agreed plan into
  the tracker (human confirms) → refresh the sprint-goal artifact. The risky
  "read the meeting transcript and codify it" mode is **not** built as an autonomous
  step; if added, it must pass the verify gate and a human confirm.

- **`roadmap-review`** — scheduled (weekly). Steps: compute epic progress from tracker
  and agent throughput → detect scope drift and recent learnings → produce a progress
  report with **scope-change flags framed as questions** (Principle 5) → post for human
  decision. Proposes nothing to the roadmap autonomously.

- **`blocker-escalation`** — event/monitor triggered. Classify: a mechanical blocker
  (stuck PR, failed CI, stalled agent run) the agent can surface and chase, vs. a
  human-only impediment (conflict, cross-team/political dependency) it must **route to
  the right human**, in their language, and track to resolution.

- **`adhoc`** — always included; open-ended task handler.

### Monitors (`monitors/defaults.yaml`)

Ceremonies are time-based, so most triggers are monitors, not webhooks.

- **`handoff-window`** — runs hourly; for each teammate whose local time crosses their
  configured end-of-day (or start-of-day) boundary, fires `scrum/handoff.due` for that
  person → launches `daily-handoff`. (Mechanical time check; no LLM per interval.)
- **`sprint-goal-drift`** — twice daily; flags in-progress items stale beyond a
  threshold, blocked items, and epics trending over original scope. Fires
  `scrum/goal.drift` only when something is found.
- **`stale-work`** — flags stuck PRs, failed CI left unattended, and **stalled
  coding-agent runs** (agent work that opened nothing / errored). Fires
  `scrum/work.stalled`.
- **`planning-brief-due`** — weekly, before the planning session (`at:` + `tz`), fires
  `scrum/planning.due` → `sprint-planning`.
- **`roadmap-review-due`** — weekly, fires `scrum/roadmap.due` → `roadmap-review`.

Prefer mechanical `check:` polls + a `relevance:` gate over LLM-per-interval monitors
wherever the signal can be pulled from the tracker/repo mechanically (the two-tier
semantic gate, per `docs/MONITORS.md`).

---

## The follow-the-sun handoff contract

This is the "explicit handoff protocol" the research demands (Principle 3). It is the
`handoff` contract between the `assemble`/`translate` steps and the `post` step, and the
definition of a complete handoff.

```yaml
handoff:
  required:
    - recipient            # who this handoff is FOR
    - since_ts             # boundary of the last handoff to this person
    - moved                # what changed since then (each item -> source_ref)
    - unblocked_for_you    # items now actionable by the recipient
    - awaiting_your_review # PRs / specs waiting on the recipient
    - decisions_needed     # explicit questions requiring a human call
    - sprint_goal_trend    # on-track / at-risk / off-track, with the why
    - source_refs          # every claim's traceable artifact (verify gate)
  optional:
    - translated_from      # original language + preserved source text
    - nuance_flags         # items flagged as translations to confirm
    - agent_work           # coding-agent activity attributed to its identity
```

**Definition of "done" for a handoff:** every item in `moved`, `unblocked_for_you`,
and `awaiting_your_review` carries a `source_ref`; `decisions_needed` is non-empty only
when a real decision is pending; `sprint_goal_trend` cites the items driving it; nothing
is posted outside the recipient's working hours.

## State the agent reasons against (`workspace/`)

The sprint goal needs a **home** — a structured, agent-readable artifact — or "trending
toward the goal" degrades into vibes (Principle 8 / the earlier gap analysis).

- **`workspace/sprint-goal.md`** — the current sprint goal, its epics, the **definition
  of "blocked"** and the **definition of "done,"** and the acceptance signals. Seeded as
  a template; the team fills it in. Read by every ceremony.
- **`workspace/team.md`** — per-teammate config the agent cannot infer: **timezone,
  native language, working hours**, and role (PM / Eng). Plus the **identities of the
  coding agents** so their work is attributed in `agent_work` rather than appearing
  ownerless. This file powers the handoff windows, translation targeting, and
  after-hours suppression.
- **`workspace/roadmap.md`** (or a link to the tracker's epics) — the epics the
  roadmap-review reports progress against. Progress is derived; scope changes are
  proposed to humans, never written autonomously.

## Guardrails (`context/`)

Read on demand by roles (cheap until read), per the create-agent guidance.

- **`context/verification.md`** — the verify-before-assert gate: no claim without a
  source artifact; treat untraceable items as false; the gate covers translation output.
- **`context/translation.md`** — translate factual/state content freely; flag
  nuanced/social/conflict content as a translation to confirm; always preserve the
  source; watch for "translationese" and politeness/directness drift across languages.
- **`context/autonomy.md`** — propose-not-execute for state changes; one-click human
  confirm; every state change emitted as an attributed, reversible event; the
  transparency rule (one shared view, no PM-only back-channel).
- **`context/working-hours.md`** — respect each teammate's hours; protect the overlap
  window; no after-hours pings; rotate any synchronous asks for fairness.

## Proposed pack structure

```
agents/scrum-team/
├── agent.md                       # team description + setup
├── agent.yaml                     # tracker + github + slack; entry_point: coordinator
├── roles/
│   ├── coordinator/ROLE.md        # persistent human-facing coordinator
│   └── synthesizer/ROLE.md        # bounded worker for handoffs/briefs/reports
├── tools/
│   ├── slack.md                   # chat intake + posting
│   ├── github.md                  # PR/commit/CI/review reads
│   └── tracker.md                 # Linear or GitHub Issues reads + proposed writes
├── workflows/
│   ├── adhoc.yaml
│   ├── daily-handoff.yaml
│   ├── sprint-planning.yaml
│   ├── roadmap-review.yaml
│   └── blocker-escalation.yaml
├── monitors/
│   └── defaults.yaml              # handoff-window, drift, stale-work, ceremony timers
├── context/
│   ├── verification.md
│   ├── translation.md
│   ├── autonomy.md
│   └── working-hours.md
└── workspace/
    ├── sprint-goal.md             # user fills in
    ├── team.md                    # user fills in (tz / language / hours / agent ids)
    └── roadmap.md                 # user fills in / links tracker epics
```

### `agent.yaml` sketch

```yaml
version: "0.1.0"
entry_point: coordinator
chat: slack
max_concurrent_agents: 4

services:
  - name: linear            # or github issues as the tracker
    events: true
    required: true
  - name: github
    events: true
    required: true
  - name: slack
    events: true
    required: true

roles:
  synthesizer: {effort: high}   # verification + translation is the hard part

auto_dispatch:
  - event: github.pull_request_review
    match: {review_state: changes_requested}
    workflow: blocker-escalation
    cooldown: 1800
```

---

## Open decisions

1. **Tracker: Linear or GitHub Issues?** Both are supported seams; the choice sets
   where epics/cycles and the sprint goal live. (Bobi already has a Linear integration
   and eng-team's GitHub-issues default.)
2. **Handoff cadence:** per-person at each timezone boundary (follow-the-sun, richer)
   vs. a single daily team digest (simpler, less differentiated). This design assumes
   the former.
3. **Retro:** ship a retro facilitation workflow now, or leave it out until the async
   coordination core is proven? Default: leave out (non-goal above).
4. **Coding-agent attribution source:** how the coding agents' runs are identified on
   the event bus so `agent_work` attributes correctly (their bot identities in
   `workspace/team.md`, or a run-registry event).

## Build sequencing (when scoped into tickets)

- [ ] Scaffold `scrum-team` pack (roles, `agent.yaml`, `adhoc`, workspace templates).
- [ ] `workspace/` schemas + the verify/translation/autonomy/working-hours context files.
- [ ] `daily-handoff` workflow + `handoff-window` monitor + the handoff contract.
- [ ] `sprint-planning` workflow (interactive, propose-not-execute).
- [ ] `roadmap-review` workflow + `roadmap-review-due` monitor.
- [ ] `blocker-escalation` workflow + `sprint-goal-drift` / `stale-work` monitors.
- [ ] Real-Claude e2e for the handoff path (the brain-dependent leg: verification +
      translation + sprint-goal-trend synthesis), per CLAUDE.md's "one mechanism, two
      brains" acceptance bar.

## Amendments

- (none yet)

## References

Research gathered 2026-07-21. Practitioner/authority sources weighted above vendor
blogs; vendor claims flagged inline.

- **Standup anti-patterns (status-report failure mode):** Scrum.org — Daily Scrum
  Anti-Patterns <https://www.scrum.org/resources/blog/daily-scrum-anti-patterns-20-ways-improve>;
  Stefan Wolpers, age-of-product <https://age-of-product.com/stand-up-anti-patterns/>;
  Geekbot <https://geekbot.com/blog/daily-standup-anti-patterns/>.
- **Async-tool limits (not queryable / no tool integration):** StandIn —
  Best Async Standup Tools 2026 <https://www.standin.co/blog/best-async-standup-tools-2026>;
  Stepsize <https://stepsize.com/blog/geekbot-alternatives-comparing-async-daily-standup-software>.
- **Issue trackers as agent infrastructure / agents as team members:** MindStudio
  <https://www.mindstudio.ai/blog/issue-trackers-ai-agent-infrastructure-jira-linear>;
  Scrum.org — AI-Augmented Scrum
  <https://www.scrum.org/resources/blog/ai-augmented-scrum-framework-when-half-your-team-autonomous-agents>.
- **Estimation broken by AI throughput:** Scrum.org forum
  <https://www.scrum.org/forum/scrum-forum/94752/how-approach-story-point-estimation-advent-ai-dev-acceleration-tools>;
  Michael Hutson (Medium)
  <https://medium.com/@michael.hutson_85041/your-agile-metrics-dont-work-anymore-and-it-s-not-your-fault-f00c3f4d1b83>;
  Atlassian <https://www.atlassian.com/agile/project-management/estimation>.
- **Roadmap: assistant not authority:** Roman Pichler — AI and Product Strategy
  <https://www.romanpichler.com/blog/ai-and-product-strategy/>;
  Productboard <https://www.productboard.com/blog/using-ai-for-product-roadmap-prioritization/>.
- **Follow-the-sun / distributed async:** Wikipedia — Follow-the-sun
  <https://en.wikipedia.org/wiki/Follow-the-sun>;
  TrackingTime <https://trackingtime.co/remote-work/remote-work-best-practices.html>;
  Engineering Manager Tools <https://www.em-tools.io/managing-teams/managing-across-time-zones>.
- **Cross-language misunderstanding (translation backfire):** ACM — Cross-lingual
  Pragmatic Misunderstandings in Email <https://dl.acm.org/doi/10.1145/3512976>;
  arXiv — Lost in Translation <https://arxiv.org/pdf/2306.07377>.
- **AI summary hallucination (invented owners/dates):** GoTranscript
  <https://gotranscript.com/en/blog/ai-meeting-summary-qa-checklist>;
  ManagedNerds <https://managednerds.com/artificial-intelligence/ai-meeting-notes-are-lying-why-summaries-miss-the-one-thing-that-matters/>.
- **Accountability of autonomous agents (vendor-flagged):** aidevdayindia
  <https://aidevdayindia.org/blogs/generative-ai-for-scrum-master/impact-of-agentic-ai-on-the-scrum-master-role.html>.
- **Psychological safety with AI teammates:** HBR
  <https://hbr.org/2026/02/how-to-foster-psychological-safety-when-ai-erodes-trust-on-your-team>.
</content>
</invoke>
