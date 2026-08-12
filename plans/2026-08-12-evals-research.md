# Evals for Bobi — research note

Status: **research / ideation**. Not a plan, not approved work. Written
2026-08-12 to answer one question: how should an agent manager routinely see
how well their agent performs against a range of sample tasks, and get
actionable recommendations on what to change?

Two constraints framed everything below:

- The manager wants a **default option from Bobi** — design, orchestrate, run,
  read the results — without adopting a vendor.
- The manager wants an **export path** to their own harness, so Bobi's default
  is not a trap.

## 1. What the incumbents actually do

| | Shape | Unit of work | Where it sits in the lifecycle |
|---|---|---|---|
| **Braintrust** | Eval-first dev platform: datasets + task fn + scorers → experiments with diffs and regressions. Also traces production and runs *online* scorers on live traces. Their AI assistant "Loop" reads production traces and proposes datasets + scorers. | prompt/input → output, scored | Build-time regression suite, plus production scoring |
| **Arize Phoenix** (OSS) | Observability-first: OTLP/OpenInference tracing, then datasets built *from captured spans*, experiments re-running those datasets, LLM-as-judge and code evaluators attached to datasets. Self-hosts in one container. REST API over spans, datasets, experiments, annotations. | span tree → dataset example → experiment run | Post-hoc: you instrument first, evals fall out of the traces |
| **oqoqo** | Task sets against *real* agents in *isolated cloud sandboxes*. You write a task the way a user would phrase it ("integrate supabase to my webapp…"), declare success criteria, and it runs the task across agents/treatments/trials, cataloguing every tool call, retry, discovery loop, token, and dollar. Supports Claude Code, Codex, Cursor, Copilot, OpenCode, and others. | a realistic task in a sandboxed environment → trajectory + verdict | Pre-production: battle-testing before anyone depends on it |

**The three are complements, not alternatives.** Braintrust and Phoenix
evaluate *outputs given inputs* and are strongest once you have production
traffic to mine. oqoqo evaluates *an agent doing a job in an environment* and
is strongest before you have any traffic at all. Bobi's manager persona needs
the oqoqo axis first (a new agent team has no production traces to mine) and
the Braintrust loop second (promote real failures into the suite).

What is worth stealing, in priority order:

1. **oqoqo's task shape** — a task is a prompt a user would actually send plus
   success criteria plus an environment, not an input/output pair. This is
   exactly a Bobi agent's job.
2. **Braintrust's promotion loop** — a production failure becomes a dataset
   row becomes a permanent regression check. This is what makes evals
   *routine* rather than a one-off exercise.
3. **Phoenix's separation of trace from eval** — traces are the substrate;
   datasets are a view over them; experiments re-run the view. Keeping those
   three layers separate is what makes export possible at all.

## 2. Can we use Phoenix?

Technically yes, with one real caveat and one real gap.

**Caveat — licensing.** The Phoenix *server* is Elastic License 2.0, which
forbids offering it as a hosted or managed service to third parties. That rules
out bundling the Phoenix server into Moda's hosted fleet. It does **not** rule
out: a user self-hosting Phoenix and Bobi exporting to it, or Bobi depending on
`phoenix-otel` / `phoenix-client` / `phoenix-evals`, which are Apache-2.0.
So Phoenix is an **export target and a local-dev target**, not a component of
the product. Verify the license split before writing any code against it.

**Gap — Bobi does not emit traces.** `docs/OTEL.md` is explicit: agent-authored
metrics and logs only, "Not tracing. No spans and no trace context… Not
auto-instrumentation. Bobi's own runtime is not exported as traces." Phoenix
ingests OTLP spans carrying OpenInference semantic conventions. So the
integration is not a connector — it is first building the thing Bobi
deliberately does not have yet.

That is a bigger piece of work than it looks, and it is a *different risk
profile* from `bobi agent otel`: runtime auto-instrumentation emits what the
framework knows, continuously, from a long-lived process, which reopens the
cardinality and credential questions `docs/OTEL.md` settled for a one-shot CLI.

**Recommendation: don't lead with Phoenix.** Lead with a native runner and a
documented flat export; add OTLP tracing as a second phase, at which point
Phoenix, Braintrust, Arize AX, Langfuse and any other OTel backend all light up
at once from the same emission. Building tracing to serve one vendor is the
wrong trade; building it once to serve the standard is the right one.

## 3. The Bobi-shaped problem

Generic eval tooling assumes request → response. **Bobi agents are
event-driven and long-lived.** A Bobi agent's unit of work is:

> an **event** (Slack message, GitHub webhook, Linear ticket, monitor firing,
> a direct `bobi agent <n> ask`) arriving at an agent in some **state**,
> producing a **run** whose outcome is usually an observable **side effect** —
> a PR opened, a Linear comment posted, a Slack reply, a workflow advanced to a
> human gate.

Three consequences, and they are the whole design:

**(a) The eval unit is `event → run → effect`, not `prompt → completion`.**
This is closer to oqoqo than to Braintrust.

**(b) Assertions can be on effects, not on text.** This is Bobi's structural
advantage over a generic eval harness. "Did it open a PR against the right
repo", "did it reach workflow step 4", "did it call the `linear` tool", "did it
stay under $0.40" are deterministic checks. LLM-as-judge is then reserved for
the genuinely fuzzy part (was the reply *good*), rather than carrying the whole
verdict. Cheaper, less flaky, and more legible than a rubric score alone.

**(c) The recommendation problem is tractable here in a way it is not for a
vendor.** Braintrust can tell you a score dropped. It cannot tell you *which
knob to turn*, because it does not know your agent's composition. Bobi does:
the levers are exactly the package contents — `agent.md`, `roles/`, `tools/`
and the tool library, `monitors/`, `workflows/`, `context/` and the KB. A
failed check can be attributed to a lever because the framework owns the
lever. **This is the differentiated part of the feature, and it is the part
neither Braintrust nor Phoenix nor oqoqo can do for a Bobi team.**

## 4. What already exists to build on

More than expected. The substrate is largely there; what is missing is the
scoring layer and the loop.

| Existing | What it gives an eval system |
|---|---|
| `bobi/webapp/runs.py` — the unified runs fold | One row shape over sessions, workflow runs, and monitor runs, already carrying status, tokens, cost, error, duration. That is most of an eval result record. |
| `GET .../subagents/{s}/transcript` (`docs/RUN_DRILLDOWNS.md`) | The debugging view: timestamped messages **and tool calls**, with error flags. That is the trajectory oqoqo charges for. |
| `bobi/brain/stub.py` alongside `claude.py` / `codex.py` | The "one mechanism, two brains" seam from `CLAUDE.md`. An eval runner is a third consumer of it: the harness itself is proven on `stub`, the scored runs go through the real brain. |
| The reference image + ephemeral event server (`container.yml`) | CI already spins the published image with a real Worker-sourced event server and drives one real ask round-trip on two brains. That *is* an eval sandbox, minus the task set and the scoring. |
| Agent packages (`agents/<team>/`) | The distribution unit. Evals belong here, next to `workflows/` and `monitors/`, so a team ships its own benchmark. |
| `bobi/costs.py`, spend window | Cost and token accounting per run, already durable. Eval reports get spend for free. |
| `bobi/otel/` | The wire format and identity labels, already solved. Tracing would extend it, not start from scratch. |

## 5. A model for Bobi evals

Three layers, deliberately separable, roughly in build order.

### L1 — the suite (design + orchestrate + run)

An eval suite is **part of the agent package**, versioned with the prompts it
tests:

```
agents/<team>/evals/
  triage-quality.yaml      # a task set
  fixtures/
    gh-issue-crash.json    # the synthetic event to publish
```

A task declares the event, the environment, and how to judge:

```yaml
task: triages a crash report to the right owner
given:
  event: fixtures/gh-issue-crash.json     # published onto the agent's bus
  repo_state: fixtures/repo-crash.tar     # optional world state
expect:
  - tool_called: linear                   # deterministic
  - effect: linear.issue.created
  - not: { tool_called: Bash }
  - budget: { max_cost_usd: 0.40, max_turns: 12 }
  - judge: "the issue names the failing module and assigns a human owner"
trials: 3                                 # non-determinism is the point
```

Run shape mirrors the container job that already exists: isolated `BOBI_HOME`,
ephemeral event server, install the package, publish the fixture event, wait
for the run to settle, read the run record and transcript, score.

CLI, consistent with the existing `bobi agent <name> <group>` shape:

```
bobi agent <name> evals run [--suite triage-quality] [--trials 3] [--brain claude|stub]
bobi agent <name> evals report [--compare <prior-run>]
bobi agent <name> evals export --format jsonl|phoenix|braintrust
```

Two things matter here and are easy to get wrong:

- **Trials, not runs.** Agents are non-deterministic; a single pass is noise. A
  suite reports pass rate across N trials, and *variance* is itself a finding —
  an agent that succeeds 2/3 of the time is a different problem from one that
  fails cleanly.
- **Treatments.** The comparison the manager wants is "same tasks, one thing
  changed": a different model, an edited `agent.md`, a role added, a tool
  removed. That is oqoqo's framing and it is the right one. It also falls out
  naturally from the package being the unit — a treatment is a package diff.

### L2 — recommendations

The differentiated layer. Take the failed checks, the trajectories, and the
package composition, and produce **lever-attributed** suggestions:

- repeated wrong-tool selection → the tool's guide in `tools/` is ambiguous, or
  the tool should not be in this role's set
- correct intent, wrong output shape → `agent.md` / role prompt
- ran long and expensive on a class of task → a monitor or `script_cache`
  automation should be handling it instead of a full session
  (`docs/AGENT_OVERVIEW.md` already prices this)
- correct behaviour that stalled at a human gate → workflow design, not agent
  quality
- fact not known → `context/` or KB gap

Implementation is itself an agent turn: feed the failure set plus the package
manifest to a session with a prompt that can only propose edits to files that
exist in the package. Output is a diff proposal, not prose. **Whether it
applies is the manager's call — never automatic.**

This layer is only credible if L1's checks are specific. Another reason to
prefer deterministic effect assertions over a single judge score.

### L3 — production feedback

Sample real runs, score them with the same scorers, and offer any failure as a
**promotion** into the suite — the Braintrust loop, run on Bobi's own runs
read model rather than on a vendor's traces. This is what makes "routinely see
how well my agent is performing" true over time: the benchmark grows toward
whatever the agent actually gets wrong in the field.

Deliberately last: it needs L1's scorers and a real fleet, and it is the layer
with the sharpest privacy question (production events contain customer data;
promoting one into a committed suite fixture commits that data).

## 6. Export

Two paths, different cost and different reach. Do them in this order.

**Now — flat export.** One documented, stable JSONL record per trial: task id,
suite, package version, brain/model, trial index, each check and its verdict,
the trajectory (already available from the transcript reader), tokens, cost,
duration, and the run/session ids. `--format jsonl` is the substrate;
`--format phoenix` and `--format braintrust` are thin mappers onto their
dataset/experiment upload APIs. Cheap, no new runtime surface, and it is
genuinely enough for "my own harness."

**Later — OTLP tracing.** Emit the agent's turns, tool calls, and workflow
steps as OpenInference-conventioned spans. One emission serves Phoenix,
Braintrust, Arize AX, Langfuse, and any OTel backend. This is a real feature
with a real risk profile — continuous emission from a long-lived process, not
a one-shot CLI — and should be scoped as such, not smuggled in as "the Phoenix
integration."

## 7. Open questions

1. **Sandbox cost and location.** L1 wants isolated environments per trial. The
   container image gets us there locally and in CI; a hosted fleet running a
   suite of 30 tasks × 3 trials against a real brain is real money. Who pays,
   and is there a `stub`-brain smoke tier that proves the harness without
   spending?
2. **Fixture provenance.** Realistic events are the whole value, and the most
   realistic ones are real customer events. What is the redaction story before
   a fixture is committed to a package?
3. **Judge independence.** LLM-as-judge on the same model family as the agent
   under test. Acceptable, or does the judge need a different brain?
4. **Does a default suite ship?** A generic "does this agent do agent things"
   suite in `bobi/templates/` would give a new team a baseline on day one, but
   risks becoming the thing people optimize instead of their actual job.
5. **Where does the manager read this?** The agent page already has runs,
   overview, and spend. Evals as a fourth tab, or a separate surface?
6. **Buy vs build for the sandbox layer specifically.** oqoqo already runs
   Claude Code and Codex in managed sandboxes with trajectory capture — the two
   brains Bobi supports. If they expose an API, L1's *execution* could be
   theirs while the task definitions, the recommendations, and the package
   attribution stay ours. Worth a conversation before building sandbox
   orchestration.

## Sources

- [Braintrust](https://www.braintrust.dev/) · [how to eval](https://www.braintrust.dev/articles/how-to-eval) · [agent observability guide 2026](https://www.braintrust.dev/articles/agent-observability-complete-guide-2026)
- [Arize Phoenix](https://github.com/Arize-ai/phoenix) · [datasets & experiments quickstart](https://arize.com/docs/phoenix/datasets-and-experiments/quickstart-datasets) · [experiments REST API](https://arize.com/docs/phoenix/sdk-api-reference/rest-api/api-reference/experiments) · [self-hosting license](https://arize.com/docs/phoenix/self-hosting/license)
- [oqoqo](https://oqoqo.ai/) · [Product Hunt listing](https://www.producthunt.com/products/oqoqo)
- In-repo: `docs/OTEL.md`, `docs/RUNS_VIEW.md`, `docs/RUN_DRILLDOWNS.md`, `docs/AGENT_OVERVIEW.md`, `docs/MONITORS.md`, `bobi/webapp/runs.py`, `bobi/brain/stub.py`
