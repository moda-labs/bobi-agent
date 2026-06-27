# Spec — pluggable agent "brain" (Claude Code / Codex / Gemini / Grok)

- **Issue:** [moda-labs/bobi-agent#485](https://github.com/moda-labs/bobi-agent/issues/485) — epic (work breakdown lives in the issue, not separate tickets).
- **Type:** feature (framework / runtime abstraction)
- **Status:** 🟡 PROPOSED — research + Phase 0 spike done, no production code yet.
- **Author:** design session (Zach + Claude)
- **Goal:** let a team choose which agentic CLI drives its agents — Anthropic
  Claude Code (today's only brain), OpenAI Codex, Google Gemini, xAI Grok — behind
  one framework-internal interface, without the runtime hardcoding the
  `claude-agent-sdk`.

> **Scope note.** This is *brain-swap* (drive a different agent CLI / loop), not
> *model-swap* (keep one loop, point it at a different LLM). The distinction
> decides everything below — see §2.

---

## 1. Where Claude is wired in today

The framework is built on **one dependency** — `claude-agent-sdk>=0.2.87`
(`pyproject.toml:32`) — and specifically on its *persistent client* shape:
`ClaudeSDKClient` → `connect(prompt)` → `query(text)` → `async for msg in
receive_response()` → `disconnect()`, with `resume=<session_id>`. That client and
its message types (`AssistantMessage`, `ResultMessage`, `TextBlock`,
`StreamEvent`, `HookMatcher`) appear in five places:

| Layer | File | Coupling |
|---|---|---|
| Manager session | `bobi/session.py` | Long-lived client; **context rotation** (`_rotate()` ~ln 236); decision-log re-inject into system prompt; mid-turn `query()` injection |
| Sub / supervised agents | `bobi/subagent.py` | One-shot + supervised loops; `PreToolUse` `HookMatcher("AskUserQuestion")` deferral (~ln 239); `skills="all"` |
| Workflow steps | `bobi/workflow/orchestrator.py` | Persistent client, per-step `query()`, resume-by-id |
| Setup wizard | `bobi/setup/llm.py` | One-shot streaming via `query()`; parses `content_block_delta`/`text_delta` |
| MCP validation | `bobi/validate.py` | `client.get_mcp_status()` |

**The coupling that actually matters is the *shape* of the SDK, not "it's
Anthropic."** Concretely, the seams an abstraction must cover:

1. **Persistent interactive client** — `connect/query/receive/disconnect` + live
   mid-turn injection + context rotation. This is the hard one (§3).
2. **Message / stream shapes** — `AssistantMessage`/`ResultMessage`/`TextBlock`;
   `content_block_delta`/`text_delta` deltas (`setup/llm.py:50`).
3. **`ResultMessage` fields** — `session_id`, `total_cost_usd`, `duration_ms`,
   `num_turns`, `model_usage`, `api_error_status`. Provider hardcoded
   `"anthropic"` (`session.py:386`).
4. **System prompt** — `{"type":"preset","preset":"claude_code","append":…}`
   literally everywhere. No other CLI has "presets."
5. **Tooling model** — `permission_mode="bypassPermissions"`, `skills="all"`, the
   `AskUserQuestion` deferral hook. Each CLI has a different sandbox/approval model.
6. **MCP** — config format + `get_mcp_status()` discovery differ per CLI.

Auth maps cleanly across all four (subscription/login vs API-key env var), and
`cli_path` is already a seam.

---

## 2. Prior art — what exists, and what to (not) reuse

Researched 2026-06-24. Two axes; don't conflate them.

### Brain-swap (what we want)

- **Agent Client Protocol (ACP)** — Zed's JSON-RPC-over-stdio standard for
  client↔agent (the "LSP for coding agents"): session lifecycle, prompt turns,
  tool calls + permissions, streaming. <https://agentclientprotocol.com/>.
  Real momentum (ACP Registry launched Jan 2026; Zed + JetBrains; Google ships
  **native** ACP in Gemini CLI). **But:** Claude Code & Codex are *not* native —
  they're driven via Zed's **TypeScript** adapters (`claude-code-acp`,
  `codex-acp`) run as subprocesses. The official **Python** SDK
  (`agent-client-protocol`, v0.10.1) is real but thin and bundles no Claude/Codex
  adapters. So from Python: native ACP client, but you'd spawn TS adapters for
  Claude/Codex — i.e. **ACP would *downgrade* our Claude path from the
  first-party SDK to a third-party shim.**
- **claw-orchestrator** (TS) / **pi-builder** (TS) / **tmuxlet** (Rust) — run the
  real CLIs as subprocesses behind one runtime; some expose an OpenAI-compatible
  HTTP proxy a Python caller could drive. Useful reference, none Python-native.

### Model-swap (NOT what we want — keep one loop, change the LLM)

- **opencode** (`opencode serve`, OpenAPI HTTP), **Charm Crush**,
  **claude-code-router**. These swap the *model* under their own agent loop; they
  do not run "Codex's brain" or "Gemini's brain." opencode-over-HTTP is a viable
  *single* brain option, but not an abstraction over the other three.

### Headless JSON convention

Every major CLI now has a non-interactive NDJSON event stream + resume — but
**no shared schema**. Claude `claude -p --output-format stream-json`; Codex
`codex exec --json`; Gemini `gemini -p --output-format stream-json`; Grok
`grok -p --output-format streaming-json`. Structurally analogous (lifecycle +
token-delta + tool-call + result events), field names all differ. ACP is the only
effort to standardize the *shape*; raw `--json` modes are per-vendor.

**Conclusion:** there is no drop-in Python library that drives all four brains.
The realistic choices are (a) adopt ACP and eat the TS-adapter glue + Claude
downgrade, or (b) own a thin per-CLI adapter layer. We recommend (b) — see §4.

---

## 3. Brain capability matrix (the feasibility constraint)

Reference baseline = `claude-agent-sdk`: persistent in-memory session, resume by
opaque id, per-turn dollar cost, MCP, published Python SDK.

| | Persistent session driven from Python | Published Py SDK | One-shot exec + resume | NDJSON stream | MCP | $ cost field | Full-auto flag |
|---|---|---|---|---|---|---|---|
| **Claude Code** | ✅ `claude-agent-sdk` (first-party) | ✅ mature | ✅ | ✅ | ✅ | ✅ | `bypassPermissions` |
| **Codex** | ✅ via `app-server` | 🟡 `openai-codex` **beta 0.1.0b3** | ✅ `codex exec resume` | ✅ `--json` | ✅ | ❌ tokens only | `--yolo` |
| **Gemini** | 🟡 ACP `--experimental-acp` (no SDK — raw JSON-RPC) | ❌ | ✅ `--resume` | ✅ `stream-json` | ✅ | ❌ tokens only | `--approval-mode=yolo` |
| **Grok (Build)** | 🟡 `grok agent stdio` ACP (beta, no SDK) | ❌ | ✅ `--resume` | ✅ `streaming-json` | ✅ | ❌ tokens only | `--always-approve` |

Key reads:

- **Only Claude (and beta Codex) give a true persistent programmatic session via a
  published SDK.** Gemini/Grok persistence exists *only* as the experimental ACP
  wire protocol.
- **The one-shot `exec --json … --resume` contract is universal.** It maps cleanly
  to our *stateless* paths (sub-agents, workflow steps, setup) — fresh process per
  turn, replay-from-rollout for continuity.
- **It maps awkwardly to the persistent manager loop** (rotation + live mid-turn
  injection assume a hot socket). For non-Claude brains the manager would either
  cold-start a process per turn or hold an ACP/`app-server` session open.
- **No one but Anthropic returns dollar cost** — every adapter must compute cost
  from token counts × a per-model price table (we already have provider/model
  fields in `record_cost`).
- Grok models are OpenAI-compatible at `api.x.ai/v1`, so "Grok the brain" is far
  less mature than "Grok the model under another brain" — deprioritize Grok-brain.

---

## 4. Proposed design — a `BrainClient` protocol (own it; don't adopt ACP wholesale)

Define a framework-internal protocol; keep `claude-agent-sdk` as the default
adapter so **Claude behavior is byte-for-byte unchanged**; add other brains as
adapters. ACP becomes *one possible adapter transport* (good fit for Gemini/Grok
persistent sessions), not the foundation — so we never downgrade the Claude path.

### 4.1 The interface

A `Protocol` capturing exactly the six seams from §1:

```python
class BrainSession(Protocol):
    async def connect(self, prompt: str | None = None) -> None: ...
    async def query(self, text: str) -> None: ...
    def receive_response(self) -> AsyncIterator["BrainMessage"]: ...
    async def disconnect(self) -> None: ...

@dataclass
class BrainMessage:            # normalized AssistantMessage | ResultMessage
    kind: Literal["assistant", "result", "stream_delta"]
    text: str = ""
    # result-only:
    session_id: str | None = None
    is_error: bool = False
    api_error_status: int | None = None
    usage: BrainUsage | None = None     # input/output/cache tokens
    cost_usd: float | None = None       # adapter computes if CLI omits

class BrainFactory(Protocol):
    def make(self, *, cwd, system_prompt, resume, extra) -> BrainSession: ...
    # capability flags so callers degrade gracefully:
    supports_persistent_session: bool
    supports_live_injection: bool
    supports_mcp: bool
```

- `system_prompt` is **normalized to a string** at the protocol boundary. The
  Claude adapter wraps it back into `{"type":"preset","preset":"claude_code",
  "append":…}`; Codex writes `AGENTS.md`; Gemini writes `GEMINI.md`. Callers stop
  constructing the preset dict (removes seam #4 from `session.py`, `subagent.py`,
  `orchestrator.py`).
- Cost recording takes the adapter's `provider`/`model`/`cost_usd` (kills the
  hardcoded `"anthropic"`, seam #3).
- Transient-error classification (`api_error_status`) becomes a per-adapter hook
  (seam #5 of the original map) — each CLI signals overload/retry differently.

### 4.2 Adapters

1. **`ClaudeBrain`** — wraps today's `claude-agent-sdk` calls verbatim. The
   refactor target: move the SDK calls out of `session.py`/`subagent.py`/
   `orchestrator.py` and behind this. **Zero behavior change** is the acceptance
   bar for phase 1.
2. **`CodexBrain`** — start with the `openai-codex` Python SDK (`app-server`,
   persistent) for parity; fall back to `codex exec --json … resume <id>` for the
   stateless paths. Cost = tokens × price table.
3. **`GeminiBrain` / `GrokBrain`** — `… -p --output-format stream-json --resume`
   for stateless paths; ACP (`--experimental-acp` / `grok agent stdio`) only if/when
   we need hot manager sessions. Both behind the same protocol.

### 4.3 Selection

New optional field in `agent.yaml` (per team, default `claude`):

```yaml
brain:
  kind: claude            # claude | codex | gemini | grok
  model: <optional override>
```

Resolved at session-construction time → picks the `BrainFactory`. Auth keys
(`OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`) flow through the existing
`${VAR}` / `.env` machinery and the #385 Fly-secrets reconcile (no new secret
path). `cli_path` generalizes to per-brain CLI resolution (`sdk.py:52`).

---

## 5. Phased plan

**Phase 0 — spike (de-risk, throwaway). ✅ DONE 2026-06-24.** Drove Codex CLI
(`codex-cli 0.134.0`, ChatGPT-login auth) through the *stateless* path end-to-end
in a throwaway `/tmp` dir. **All four contract points confirmed:**

- **Headless NDJSON.** `codex exec --json` emits a clean event stream:
  `thread.started{thread_id}` → `turn.started` → `item.started/completed`
  (`item.type` ∈ `agent_message` | `file_change` | `mcp_tool_call`) →
  `turn.completed{usage}`. Maps directly onto the `BrainMessage` normalization in §4.1.
- **Tool use.** A "create hello.txt" task produced `file_change` items and the file
  on disk (`--sandbox workspace-write`).
- **Resume + context continuity.** `codex exec resume <thread_id> …` retained prior
  context — reversed `brain-spike-ok` → `ko-ekips-niarb` from memory of what it
  wrote, same `thread_id`. Resume is the session-id seam for the stateless paths.
- **MCP.** Inlined `@modelcontextprotocol/server-everything` via
  `-c 'mcp_servers.everything.command="npx"' -c 'mcp_servers.everything.args=[…]'`.
  Codex connected, discovered tools, resolved a colloquial "add" ask to the real
  tool `get-sum`, called it `{a:17,b:25}` and returned `42`. `mcp_tool_call` items
  carry `{server, tool, arguments, result, error, status}`.

**Gotchas the adapter must handle (found in the spike):**

1. **stdin.** `codex exec` reads stdin when piped and *hangs* waiting for EOF —
   the adapter must pass the prompt as an arg and redirect stdin from `/dev/null`.
2. **Approval gate.** In non-interactive `exec`, tool/MCP calls are **auto-cancelled**
   (`"user cancelled MCP tool call"`) unless approvals are bypassed. Need
   `--dangerously-bypass-approvals-and-sandbox` (or `approval_policy="never"` +
   a sandbox mode) — the Codex analog of Claude's `bypassPermissions` (seam #5).
3. **`resume` has a narrower flag set** than `exec` — no `-C/--cd` or `-s/--sandbox`;
   set cwd via process cwd and sandbox via `-c sandbox_mode=…`.
4. **No dollar cost.** `usage` is token counts only
   (`input_tokens`/`cached_input_tokens`/`output_tokens`/`reasoning_output_tokens`)
   — confirms §3; the adapter computes cost from a price table.

**Verdict:** the stateless exec/NDJSON/MCP/resume contract is solid and cleanly
normalizable — Phase 1/2 are unblocked. Not yet tested (deferred to Phase 3):
persistent `app-server` hot session, live mid-turn injection, context rotation.

**Phase 1 — extract `BrainClient`, Claude as default (no behavior change). ✅
DONE.** Landed the `bobi/brain/` package — the provider-agnostic contract
(`BrainSession`/`BrainFactory` protocols + normalized `AssistantText` /
`TurnResult` / `StreamDelta` / `BrainCost` / `DeferredTool`), a `get_brain(kind)`
selector (default `claude`), and the `ClaudeBrain` adapter (behavior-preserving
SDK translation, lazy `claude_agent_sdk` imports + a one-shot `stream_once` and a
`get_mcp_status` capability). **All five integration sites migrated** —
`session.py` (manager loop + rotation/recovery), `subagent.py` (supervised loop +
deferrals), `workflow/orchestrator.py` (per-step drive), `setup/llm.py` (one-shot
stream), `validate.py` (MCP probe). **`claude_agent_sdk` is now imported in
exactly one place** (`brain/claude.py`), the sole exception being the
Claude-specific `_make_defer_hook` (the `HookMatcher`/`AskUserQuestion` deferral
— open Q5). Cost attribution now reads the brain's `provider`, not a hardcoded
`"anthropic"`. **Full unit suite green: 2269 passed / 7 skipped** (2258 + 11 new
`test_brain.py`); unit mocks moved from raw SDK messages to the brain seam (and
the `get_cli_path` patches re-pointed to `bobi.sdk`, the adapter's read
site). **Latent bug found + preserved + tracked:** the SDK types `model_usage`
as `dict[str, Any]`, but the legacy code iterates it as a list-of-objects, so
real-run per-model cost attribution has always recorded empty model + 0 tokens.
Preserved verbatim (zero-behavior-change) with a guard test; fixing the dict
shape is the one Phase-1 follow-up. **Deferred (Phase 2 cleanup):** normalize the
`{"preset":"claude_code"}` system-prompt dicts to a plain string at the boundary
(today passed opaquely); the call sites still build the preset dict.

**Phase 2 — Codex on the stateless paths.** `CodexBrain` for sub-agents + workflow
steps (where exec+resume fits naturally). Integration test: a workflow step
completes + writes a valid handoff driven by Codex. Prove MCP + tool use + cost.

**Phase 3 — the persistent manager loop.** Hardest, do last. Decide per-brain
strategy: Codex `app-server` hot session vs. resume-from-rollout per turn; how
context rotation + decision-log re-inject + mid-turn injection behave when there's
no live socket. May need a brain capability flag that routes the manager loop down
a different code path for non-persistent brains.

**Phase 4 (optional) — Gemini / Grok.** Same stateless contract; ACP transport for
hot sessions if Phase 3 proves it's needed. Grok-brain deprioritized (less mature
than Grok-as-model).

---

## 5b. Brain selection + headless subscription auth (landed 2026-06-24)

**Brain selection in `agent.yaml`. ✅ DONE.** A team picks its brain with a
top-level block:

```yaml
brain:
  kind: codex        # claude (default) | codex | …
  model: gpt-5-codex # optional override (parsed; threading deferred)
```

`Config` parses `brain` + exposes `brain_kind`. At the agent process entry
(`cli._run_from_config`) `set_process_brain(cfg.brain_kind)` exports
`BOBI_BRAIN`, which `get_brain()` reads (precedence: explicit arg →
`BOBI_BRAIN` → `claude`) — so the choice propagates to in-process and
subprocess agents with zero churn at the call sites, exactly like
`BOBI_AUTH`. An unknown kind fails loud at session construction.

**Codex subscription auth on a headless box. ✅ FEASIBLE + bootstrap core
landed.** Verified empirically: `codex login --device-auth` prints a fixed
device URL (`https://auth.openai.com/codex/device`) + a one-time code
(`XXXX-XXXXX`) and then **polls** until the human authorizes — no code is pasted
back (unlike Claude's flow). `~/.codex/auth.json` carries a `refresh_token`, so
it self-renews — a once-per-machine ceremony like Claude (§6.1).

`auth_bootstrap.py` is now **brain-aware** (a per-kind `SubscriptionLogin` spec:
login command, credential path, the API key that would shadow OAuth, and the
flow shape). Two flows:
- **Claude — `paste_back`:** scrape URL → post to Slack → human pastes the code
  back over the event bus → write to the pty. (unchanged)
- **Codex — `device_poll`:** scrape URL **and** code → post both to Slack → wait
  for the CLI to poll-authorize → verify `~/.codex/auth.json`.

Driven by `BOBI_BRAIN`/`brain.kind`; credential path, shadow-key guard
(`OPENAI_API_KEY` for codex), and the login CLI all follow the brain. Unit-tested
(`tests/test_auth_bootstrap.py`: codex credential path, URL+code scrape, the full
device-poll bootstrap, and the `OPENAI_API_KEY`-shadow refusal); the Claude path
is byte-for-byte preserved.

**Deploy-side wiring. ✅ DONE (2026-06-24).** Landed alongside the `CodexBrain`,
unit-tested + shellcheck-clean:
1. `docker-entrypoint.sh`: reads `BOBI_BRAIN`; brain-aware shadow-key guard
   (`OPENAI_API_KEY` for codex via indirect expansion), a `~/.codex` → `/data/codex`
   volume symlink (mirrors the `~/.claude` → `CLAUDE_CONFIG_DIR` link) so the
   ChatGPT subscription survives a redeploy, and the first-boot check now waits on
   the brain's credential file (`auth.json` vs `.credentials.json`).
2. `deploy.py`: `DeployConfig.brain` resolved from the team's `agent.yaml`
   `brain.kind`; the api_key *required* and subscription *forbidden* key are the
   brain's (`_brain_api_key`); `--brain` flows to the provisioner.
3. `provision-instance.sh`: `--brain` arg → `BOBI_BRAIN` in the instance
   `[env]`; brain-aware auth-key invariants + login fallback hint.

**MVP deployment example created:** `agents/codex-test/` (minimal Slack-only
single-manager team, `brain: {kind: codex}`) and
`deployments/codex-test.yaml.example` (app `ci-codex-test`,
`auth: subscription`). It is intentionally example-only, not part of the release
deploy matrix. Deploy manually with `bobi deploy codex-test --login-channel
<private-channel>` once a Slack app, a Fly app, and the ChatGPT device login are
in place — the first boot runs the codex device-auth bootstrap.

(Alternative to the device-flow: provision `~/.codex/auth.json` directly as a
volume file — the refresh token keeps it alive — if an interactive Slack ceremony
is unwanted.)

---

## 6. Open questions

1. **Manager loop for non-persistent brains.** Is a cold-start-per-turn manager
   acceptable (latency, lost in-memory context), or do we require an `app-server`/
   ACP hot session — making "supports the manager role" a brain prerequisite?
2. **Cost.** Maintain a per-model price table in-repo (drift risk) or accept
   token-only attribution for non-Claude brains? `costs --by model` already exists.
3. **Tool/skill parity.** Claude's `skills="all"` exposes a specific built-in
   toolset (Read/Edit/Bash/…). Codex/Gemini have their own. Do role prompts assume
   Claude tool *names*? (Audit `tools/*.md` + role prompts for hardcoded tool refs.)
4. **MCP discovery.** Replace `get_mcp_status()` validation with a per-brain probe,
   or skip preflight MCP validation for brains that lack a status call?
5. **`AskUserQuestion` deferral.** The `PreToolUse` hook is Claude-specific. Is
   interactive deferral required for non-Claude brains, or manager-only (Claude)?
6. **ACP later?** If ACP's Python story matures and first-party adapters appear,
   does `BrainClient` collapse onto an ACP transport — and is that strictly better
   than per-CLI adapters? Keep the protocol ACP-compatible in shape to leave the door open.

---

## 7. Recommendation

Build **our own thin `BrainClient` protocol with per-CLI adapters (§4)**, not a
wholesale ACP adoption — because ACP would force the Claude path off its
first-party SDK onto a TS shim, the Python ACP story is immature, and a per-adapter
layer matches bobi's existing CLI-first philosophy ([[project_cli_first_capabilities]]).
Keep the protocol *shaped* like ACP so we can swap in an ACP transport per-adapter
(natural for Gemini/Grok hot sessions) without redesigning.

Sequence by where the brains actually fit: the **stateless** sub-agent/workflow
paths port to any brain via the universal `exec --json … --resume` contract; the
**persistent manager loop** is the real constraint and only Claude (SDK) and Codex
(beta `app-server`) satisfy it cleanly today. So: spike Codex stateless first
(Phase 0), extract the abstraction with zero Claude regression (Phase 1), then add
brains outward-in from the easy paths.
