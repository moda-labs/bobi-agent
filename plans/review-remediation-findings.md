# Review-remediation findings appendix

Companion to `plans/review-remediation.md`. One entry per finding from the July 2026 two-pass full-repo review (281 agents; every finding adversarially verified). IDs: `D###` = defect pass, `Q###` = quality pass. Status: confirmed = verifier independently traced it; plausible = survived verification but not fully traced (builder re-verifies before fixing); unverified = past the verification cap (builder verifies the evidence first).


## Q001 — `bobi/cli.py:3361` (structure, high, confirmed, phase deferred)

**Agent-scoped commands are declared on `main`, then bulk-copied into the `agent`/`agents` groups and popped from `main` via three name-list loops at the bottom of the module, plus five redundant `main.add_command(...)` calls that the loops immediately undo.**

- Detail: Every command that ends up under `bobi agent <name>` is defined with `@main.command()`/`@main.group()`, re-registered onto `agent` by the loops at lines 3361-3374, and finally popped from `main.commands` at 3376-3381; `main.add_command(workflows)` (2296), `roles` (2321), `monitors` (2652), `event_server_cmd` (2755), and `kb` (3291) are no-ops (the group decorator already registered them) that are then popped anyway. Adding a new agent command today requires knowing to also edit the string lists or the command silently lands in the wrong group. The simpler shape: decorate directly with `@agent.command()`/`@agent.group()` (and `@agents.command()` for `install`), delete the move/pop loops and the redundant add_command calls (~30 lines of wiring). The final command tree is identical, so behavior including `--help`, plugin dispatch through `_PluginGroup`, and tests that walk `cli_main.commands["agent"]` (tests/test_tool_guides.py) is preserved.
- Evidence: Read lines 2296, 2321, 2652, 2755, 3291 (redundant add_command after decorator registration) and 3361-3381 (move+pop loops); grep of tests shows only tests/test_tool_guides.py walks the final tree via cli_main.commands, which is unchanged by direct decoration.

## Q040 — `bobi/cli.py:3392` (simplification, medium, confirmed, phase deferred)

**Sixteen commands are defined with @main.command(), then relocated into the `agent`/`agents` groups and popped from `main` via three parallel hand-maintained name lists at module bottom.**

- Detail: The add-then-pop dance (cli.py:3392-3412) exists only because the commands are decorated onto the wrong group. Decorating them directly with @agent.command() / @agent.group() / @agents.command() (the `agent` group is defined at line 296, before every relocated command; `agents` at 1763 just needs to move above `install`) produces the identical command tree with ~20 fewer lines, no always-true `if _cmd_name in main.commands` guards, and no risk of a new command silently staying top-level because someone forgot to extend three string lists. Behavior preserved: same Command objects end up attached to the same groups; nothing inspects main.commands between definition and the pops (module import is atomic).
- Evidence: Read cli.py:378 (`@main.command()` on start), 596 (install), 1846 (skill stays on main) and the relocation loops at 3392-3412; every name in the pop list at 3407-3412 appears in one of the two add lists, so `main` never actually exposes them.

## Q106 — `bobi/events/server.py:273` (structure, medium, plausible, phase deferred)

**server.py is documented as the 'Local event server launcher' but two-thirds of it (lines 273-795) is the registration/bubble protocol client plus three channel-credential registrations.**

- Detail: The module has two independent halves with disjoint consumers: (a) the Node launcher — _find_event_server_dir/_needs_build/_needs_install/_build_local/ensure_running/health — imported by cli.py, doctor.py, service.py; (b) the event-server protocol client — BubbleRejected/UnauthorizedTopics/ensure_bubble/register/authorize_resources/deregister/register_slack_workspaces/register_whatsapp_numbers/register_discord_apps — imported by subagent.py:1178, inbox.py:185/217, auth_bootstrap.py:250/417. Nothing in half (b) touches half (a). bobi/events/ already splits along protocol seams (signing.py, gateway.py, client.py, publish.py), so moving the client half to e.g. bobi/events/registration.py follows the existing house layout, halves an 800-line file, and is a pure move (re-export from server.py or update ~6 import sites) with behavior unchanged.
- Evidence: grep of importers: cli/doctor/service import only ensure_running/health/_is_loopback_or_tls; subagent/inbox/auth_bootstrap import only ensure_bubble/register/authorize_resources/register_*/BubbleRejected/deregister. No cross-references between the halves except health() used by ensure_running.

## Q103 — `bobi/subagent.py:530` (simplification, high, plausible, phase deferred)

**run_phase_blocking and spawn_adhoc are the same session-lifecycle skeleton written twice (~150 combined lines), with spawn_adhoc alone constructing AgentResult three near-identical times.**

- Detail: Both functions do: emit started → compose 'You are a {label} agent…' append text + long-term-memory prompt → load team config → resolve mcp/model/effort → build Session with identical extra_options ({skills: all, max_turns: 200, mcp/model/effort splats}) → session.start(prompt, timeout) → build AgentResult from session fields → session.stop() → emit finished. The only real deltas are the prompt/append wording, phase name, the role prompt, and spawn_adhoc's persistent-join branch. A single `_run_session_blocking(name, prompt, append_parts, *, phase, run_key, project, timeout, role, requested_by, subscribe=None, persistent=False, model='', effort='')` helper reduces each public function to ~15 lines of prompt/name assembly. Within spawn_adhoc the persistent (765-777) and non-persistent-ok (779-789) AgentResult constructions differ only in `success`/`error`, and the failed-to-start construction is repeated verbatim between the two functions — one result-builder closure removes all five copies. Behavior preserved: same Session options, same emit ordering, same result fields.
- Evidence: Read of subagent.py 530-617 vs 663-799: identical Session(extra_options={skills, max_turns:200, mcp/model/effort}), identical start/stop/emit bracket, five AgentResult constructions differing only in success/error/phase; the task brief itself flags this overlap.

## Q002 — `bobi/subagent.py:1152` (structure, high, confirmed, phase deferred)

**~350 lines of event-subscription plumbing (Subscription, _start_event_subscription, _resolve_self_github_login) live in the subagent executor module but are core session/event infrastructure consumed by session.py via private-name imports.**

- Detail: session.py (the lower-level module) imports `_start_event_subscription` from bobi.subagent at lines 1100 and 1136, and `_load_long_term_memory_prompt` at line 620 — an inverted dependency where the session core reaches up into the executor for underscore-private helpers. `_load_long_term_memory_prompt` is itself a 6-line delegate over bobi.memory.load_long_term_memory + format_long_term_memory_prompt and belongs in bobi/memory.py as a public function. Moving Subscription/_start_event_subscription/_resolve_self_github_login to a new bobi/events/subscription.py (they already import exclusively from bobi.events.*, bobi.config, bobi.http) and the memory-prompt loader into bobi.memory shrinks the 2.1k-line subagent.py by ~360 lines, removes the session→subagent import edge, and leaves subagent.py as purely the executor it claims to be. Pure code motion — behavior unchanged.
- Evidence: grep: bobi/session.py:620 `from bobi.subagent import _load_long_term_memory_prompt`; bobi/session.py:1100,1136 `from bobi.subagent import _start_event_subscription`. subagent.py:641-655 shows _load_long_term_memory_prompt is a thin wrapper over bobi.memory. _start_event_subscription (1152-1461) touches nothing subagent-specific.

## Q003 — `bobi/workflow/orchestrator.py:348` (structure, high, confirmed, phase deferred)

**_run_workflow_async is a ~600-line function with 8 nested closures and a monolithic step loop; it has clean seams for extraction that would make the RC#1-4 accretion legible.**

- Detail: The function mixes five separable concerns, each already delimited by comment banners: (1) session bootstrap with resume-fallback retry (lines 544-580), (2) the model/effort/agent switch block inside the prompt-step path (lines 746-822, ~75 lines including its own nested resume-fallback), (3) prompt-step execution + handoff validation/retry + output capture (lines 832-891), (4) await-step suspend/persist (lines 704-740), and (5) the terminal-status finally block. The nested defs (_effective_step_model, _effective_step_effort, _is_prompt_step, _first_prompt_step, _continuation_prompt, _make_session, _exhaust_step) close over ~10 mutable locals (client, current_model, current_effort, current_agent, visit_counts, saved_id...), which is why every RC# fix had to be patched inline rather than into a unit-testable helper. Better shape: a small execution-state object (workflow, ctx, session_name, current_model/effort/agent, client) with methods per step type, or module-level helpers taking explicit params returning (next_idx | failure). The while loop becomes a thin dispatcher on step type. Behavior is preserved because the seams are already control-flow-isolated: each block communicates with the rest only through step_idx, run_failed/failure_error, and the current_* session state. The repeated 4-line failure epilogue (set failed_step, set run_failed, _emit_step_failed, return False) at 5 sites collapses into one helper in the process.
- Evidence: Read the full function (lines 348-943): five comment-delimited blocks, 8 nested defs, and the same failure epilogue at lines 628-631, 650-655, 692-700, 816-822, 841-845, 866-870.

## Q036 — `event-server/core/src/core.ts:320` (structure, medium, confirmed, phase deferred)

**core.ts is a 2,490-line god-file whose internal section banners are already module seams the repo has split along before (adapters/, channels.ts, conversation.ts) — the remaining sections should be files.**

- Detail: The file contains at least six independent concerns separated by banner comments: Slack workspace records + bot-resolution helpers (lines 103-217, 2346-2490), signature/bubble auth + rejection counters (406-710), the inbound webhook pipeline incl. ingest rate limiting (809-1299), deployment registration/subscription handlers (1301-1506), ingest-token mint/list/revoke (1508-1653), resource grants (1655-1865), and the /channels send/typing/history handlers + per-channel credential resolution (1867-2344). These sections import almost nothing from each other beyond StorageAdapter/HandlerResult/NormalizedEvent. The repo already executed this exact move: lines 320-333 show adapters were extracted to adapters/*.ts with re-export shims ("Re-exported here so existing imports from core continue to work"), and channels.ts/conversation.ts/circuit-breaker.ts live as sibling modules. Splitting e.g. auth.ts, webhook-pipeline.ts, grants.ts, ingest-tokens.ts, slack-workspace.ts, channels-handlers.ts with the same re-export shim preserves every import path and all behavior, and stops every feature (#488, #618, #628, #639, #640, #656 all landed here) from growing one file.
- Evidence: wc -l: core.ts = 2490 lines vs the next-largest core module at 794 (channels.ts); banner comments at lines 30, 254, 335, 406, 630, 712, 809, 1508, 1655 mark the seams; lines 320-333 document the prior adapters extraction with back-compat re-exports.

## Q004 — `event-server/src/local.ts:478` (structure, high, confirmed, phase deferred)

**The HTTP route table (path match + auth-mode prologue + core-handler binding) is hand-rolled per transport, so local.ts and the Worker entry each maintain a parallel copy of the same security-sensitive dispatch.**

- Detail: handleRequest is a ~250-line if-chain where each route re-states its auth mode: four routes hand-roll readBody+parseJson+readBubbleAuthHeaders (POST /deployments, /events/{topic}, /slack/workspaces, /__test/resource-grants), others go through bubbleAuthedJson, two use Bearer-apiKey. The private Worker (bobi-deploy/event-server/src/index.ts, 828 lines) mirrors the exact same route list with its own copy of bubbleAuthedJson (its line 354) and the same per-route auth wiring. The codebase already has the right pattern for exactly this problem: the #639 webhook pipeline (WEBHOOK_SOURCES + matchWebhookSource + handleWebhookRequest in core.ts) is a declarative source table both transports call, so a webhook route structurally cannot exist unverified. The same shape — a core ROUTES table of {method, pattern, auth: "none"|"api-key"|"bubble"|"bubble-optional", handler} plus one transport-neutral dispatch(method, path, rawBody, headerGet) → HandlerResult|null — would collapse local.ts's if-chain to a body-read + one dispatch call and eliminate the second copy in the Worker. Behavior is preserved because the routes, auth modes, and handlers are identical today; only the binding moves into core. Today every new endpoint (e.g. the three /channels routes, the ingest-token trio) is added twice with matching auth wiring, and a mismatch is a cross-transport security bug.
- Evidence: local.ts:478-731 if-chain; four near-identical readBody/parseJson/readBubbleAuthHeaders blocks at 539-549, 574-585, 588-609, 715-726; grep of ~/dev/bobi-deploy/event-server/src/index.ts shows the same handlers dispatched from its own if-chain with a duplicated bubbleAuthedJson at line 354; core.ts:1010-1299 (WEBHOOK_SOURCES/handleWebhookRequest) is the in-repo precedent for table-driven transport-shared routing.

## Q129 — `tests/integration/conftest.py:44` (structure, low, unverified, phase deferred)

**_provision_bobi_env's contract explicitly pushes BOBI_HOME/BOBI_ROOT save/restore onto every caller ('Caller owns BOBI_HOME/BOBI_ROOT save/restore around the yielded value'), so the identical 14-line try/finally restore dance is repeated in three fixtures.**

- Detail: claude_bobi_env (conftest L177-191), stub_bobi_env (conftest L222-238), and instructions_bobi_env (test_team_instructions.py L28-44) each snapshot old_home/old_root and replay the same four-way pop-or-restore in finally. Converting _provision_bobi_env into a @contextmanager that performs the snapshot before mutating os.environ and the restore in its own finally deletes all three copies and makes it impossible for the next caller (every new '#779-style' suite that needs its own scaffold, as test_team_instructions did) to forget the restore. Behavior preserved: the save/restore semantics move verbatim into the helper; fixtures become 'with _provision_bobi_env(...) as env: yield env'.
- Evidence: conftest.py L44 docstring states the caller-owns contract; read all three call sites — the try/finally blocks are structurally identical; test_team_instructions.py L15 already imports the private helper cross-module, confirming third-party callers exist and inherit the burden.

## Q005 — `tests/integration/test_channel_gateway.py:125` (structure, high, confirmed, phase deferred)

**The isolated local-event-server environment (project dir + minimal agent.yaml + free port + ensure_running + readiness wait + bubble mint via _post_register/save_bubble_state + service registration + pid-file SIGTERM teardown) is re-solved as a bespoke ~40-line module fixture in six gateway/live integration files instead of being owned by a shared harness.**

- Detail: test_channel_gateway.gateway_env (L125), test_whatsapp_gateway (L155-205), test_discord_gateway (L290-341), test_slack_live.live_env (L69-142), test_slack_socket_live.live_socket (L118-175), and test_slack_socket_mode (L475-537) each hand-roll the identical sequence, differing only in the stub they wire, the BOBI_ES_* extra_env keys, and which registration call runs. test_inbox_transport.py adds two more copies (inbox_event_server L34, fast_eviction_event_server L258) and test_event_isolation.iso_project (L105) a ninth. The better shape: a context-manager/fixture factory in tests/integration/conftest.py (which already owns free-port allocation at L52 and _provision_bobi_env), e.g. local_event_server(base, *, extra_env=None, register=None) that yields (project, es_url, bubble) and owns the health wait and pid-file teardown. Each test file keeps only its stub and its registration payload. Behavior is preserved because every copy performs the same steps in the same order; only the parameterized parts differ. This is the hot maintenance path for every new channel integration (Slack, WhatsApp, Discord so far — each one re-copied the scaffold).
- Evidence: grep for _post_register/save_bubble_state shows the mint+persist pair in 7 integration files; grep for ensure_running shows 9 integration files each pairing it with their own port alloc, readiness poll, and an identical pid-file SIGTERM teardown block; read all six gateway env fixtures and confirmed they differ only in extra_env keys and registration call.

## Q111 — `tests/integration/test_slack_socket_mode.py:323` (structure, medium, plausible, phase deferred)

**Four integration files re-build the same recording-HTTP-stub scaffold — ThreadingHTTPServer + inner BaseHTTPRequestHandler with log_message suppression, a JSON _respond helper, a recorded calls list, and start/stop methods — around what is really just a per-service route table.**

- Detail: _SlackStub (test_channel_gateway.py L39-124), _SlackRestStub (test_slack_socket_mode.py L323-395), _GraphStub (test_whatsapp_gateway.py L55+), and _RestStub (test_discord_gateway.py L191+) each carry ~35 lines of identical server plumbing before any service-specific behavior appears; test_gateway_brain.py and test_gateway_openai_brain.py hand-roll the same concern at the raw BaseHTTPRequestHandler level. A shared RecordingHTTPStub base in a tests/integration helper module (holding port alloc, the threading server, _respond, _read_body, calls recording, start/stop, and a named() query) would leave each file with only its route dispatch — the part a reader actually needs to see to understand the test. Behavior preserved: the scaffolding is semantically identical across copies; only do_GET/do_POST route bodies differ, and those stay in each file.
- Evidence: grep 'class .*Stub' across tests/ lists the six stub classes; read _SlackStub and _SlackRestStub in full — both contain byte-similar _respond/log_message/start/stop/ThreadingHTTPServer blocks around different route tables; _GraphStub and _RestStub confirmed same skeleton via their do_POST/_respond signatures.

## D073 — `bobi/events/drain.py:349` (bug, low, confirmed, phase 1)

**monitor.error inbox pushes omit the on_done=batch_ack.attach() completion callback, so their event seq is ACKed at push time instead of after processing, violating the #688 ack-after-processing contract.**

- Detail: A batch containing only a monitor.error event: open_batch(seq) takes one ref, the Message is pushed without on_done (lines 349-353), batch_ack.close() drops the last ref and the watermark ACKs the seq immediately. If the session crashes after the push but before processing the message, the server never replays it (already ACKed) and the monitor failure notice is lost until the counter's every-3rd re-push. Every other push branch in the same loop (inbox line 296-303, policy line 319-326, event-bus line 404-408) passes on_done=batch_ack.attach().
- Evidence: drain.py:349-353 `inbox.push(Message(id=_msg_id(), sender="monitor-error", text=text,))` with no on_done, vs drain.py:302 and drain.py:406 which pass `on_done=batch_ack.attach() if batch_ack else None`.

## D001 — `bobi/session.py:972` (bug, high, confirmed, phase 1)

**_process_message ACKs a message even when its turn died on a dead transport, so the message is lost on restart instead of replayed — the exact #688 bug class the ACK-after-processing design exists to prevent.**

- Detail: A Slack mention is delivered to the session; mid-turn the claude subprocess dies (broken pipe/OOM-kill). _drain_turn's non-decode exception path (session.py:824-831) swallows the exception, sets state='error', and returns normally. _process_message then continues past `response = await self._drain_turn()` (line 944), responds if wait=True, and unconditionally calls `await self._ack_processed(msg)` (line 972) — advancing the event-server cursor. The supervisor restarts the wedged process, but the ACKed message is never replayed: the mention is silently lost. Contrast with the outer except (lines 973-978) and the drop path (lines 904-917), which deliberately do NOT ack so a restart replays.
- Evidence: _ack_message docstring (session.py:224-231): 'Only called on paths where the message was actually processed (#688) - a dropped or failed message must not ack, so a restart replays it.' Yet _drain_turn's dead-transport handler (lines 824-831) sets state='error' without raising, and _process_message line 972 acks unconditionally after _drain_turn returns; _last_is_error is not set on that path so the retry loop (line 951) is also skipped.

## D021 — `bobi/session.py:1205` (bug, medium, confirmed, phase 1)

**Session.start() blocks for the full timeout even after the session thread has already crashed, because it waits only on _ready and never checks thread liveness.**

- Detail: The brain CLI is missing or connect() raises immediately with no resume id: _run raises, _thread_target catches it and sets state 'error', and the thread exits within seconds. But _ready is never set, so start() sits in `self._ready.wait(timeout=timeout)` for the entire timeout — up to 3600s for spawn_adhoc / run_phase_blocking (subagent.py:594, 755) — before returning False. A workflow phase whose launch fails instantly stalls dispatch for an hour instead of failing fast.
- Evidence: session.py:1196-1208: start() has no `self._thread.is_alive()` check in the wait; session.py:1059-1072: _thread_target catches the crash, sets _state='error', exits the thread without setting _ready.

## D003 — `bobi/session.py:1214` (bug, high, confirmed, phase 1)

**Session.stop() cannot shut down a session whose startup turn is still in flight: _keep_alive is created only after the startup drain (line 1044), so stop() silently no-ops the shutdown signal and the session thread plus its brain subprocess keep running forever.**

- Detail: A phase's startup turn exceeds effective_timeout, so session.start() returns False and run_phase_blocking calls session.stop() (subagent.py:594, 614). At that moment self._keep_alive is still None (it is only assigned at session.py:1044, after _ready.set()), so `if self._keep_alive: self._keep_alive.set()` does nothing; join(15) expires while the turn is running. When the startup turn eventually finishes, _run blocks on `await self._keep_alive.wait()` with a live connected client — inside a long-lived workflow-orchestrator process this leaks a running agent (state='waiting_input', claude subprocess alive, burning tokens) for every timed-out phase, while the phase was reported as 'session failed to start'. The same race exists on the normal path in the window between _ready.set() (1039) and _keep_alive creation (1044).
- Evidence: session.py:1039-1044: `self._ready.set()` precedes `self._keep_alive = asyncio.Event()`; session.py:1213-1216: `stop()` guards with `if self._keep_alive:` and has no other mechanism to make _run exit; the inbox loop only breaks when `self._keep_alive and self._keep_alive.is_set()` (session.py:988).

## D067 — `bobi/subagent.py:511` (bug, low, confirmed, phase 1)

**_run_agent_supervised's `except asyncio.TimeoutError` handler is unreachable — the timeout parameter is never enforced inside the coroutine, and the only caller's outer asyncio.wait_for delivers CancelledError (a BaseException) which neither this handler nor `except Exception` catches, so the TERMINAL_FAILED timeout persist never runs.**

- Detail: A monitor check hangs past its timeout. _run_verdict_agent_blocking's wait_for (subagent.py:1803-1810) cancels the inner task: inside _run_agent_supervised a CancelledError is raised at the receive_response await, skips both except clauses (lines 511-520), runs only the finally-disconnect, and propagates. The `result.error = _timeout_error(timeout)` / `_persist_terminal(..., TERMINAL_FAILED, error='subprocess timeout after Ns')` bookkeeping at 511-514 is dead code — the entry's state.json never records the terminal failure or the timeout reason; the caller only flips status='error' via registry.update (line 1812), which sets no error detail and no terminal marker via mark_terminal.
- Evidence: subagent.py:343-527: no asyncio.wait_for/timeout applied to the receive_response loop inside the function; sole caller at subagent.py:1803-1810 wraps it in wait_for, whose cancellation raises CancelledError inside the task (BaseException since Python 3.8), not asyncio.TimeoutError.

## D002/Q011 — `bobi/subagent.py:601` (bug, high, confirmed, phase 1)

**run_phase_blocking (and spawn_adhoc's non-persistent path at line 784) reports success=True and persists TERMINAL_COMPLETED when the startup turn's transport died mid-drain, because Session._drain_turn's dead-transport path never sets _last_is_error.**

- Detail: A workflow phase starts; during the startup turn the brain subprocess dies. Session._drain_turn catches the exception, sets _state='error' but leaves _last_is_error at its initial False (session.py:285, 824-831). Session._run still reaches self._ready.set() (session.py:1039), so session.start() returns True. run_phase_blocking computes success=not session._last_is_error → True with error='' (subagent.py:601-606), _emit_session_finished persists TERMINAL_COMPLETED and posts agent/session.completed — a crashed phase is recorded as a clean completion and the workflow advances on partial/empty output.
- Evidence: session.py:798-831: the non-decode drain exception path only does _set_state('error') + registry.update(status='error'); _last_is_error is untouched (initialized False at session.py:285). subagent.py:597-607 builds AgentResult solely from session._last_is_error / _last_response; spawn_adhoc repeats the same pattern at subagent.py:779-789.
- Cross-pass (Q011): run_phase_blocking and spawn_adhoc reach into six private Session attributes (_last_is_error, _total_duration_ms, _total_cost_usd, _total_turns, _last_response, _thread) to build results and to join the persistent session.

## D119 — `agents/dogfood-content-review/agent.yaml:4` (config, low, confirmed, phase 2)

**Pack declares `chat: slack` and the manager role routes "Slack DM" events, but no slack service is declared, so no Slack events are ever subscribed and `bobi reply` has no bot token.**

- Detail: Team starts with services github + email only -> ingress subscriptions derive solely from services with events:true (inbound_event_sources), so no slack:<workspace> topic is subscribed -> the "Slack DM requesting content work" / "Slack DM asking a question" rows in roles/manager/ROLE.md (routing table) can never fire, and any attempted Slack reply lacks SLACK_BOT_TOKEN.
- Evidence: bobi/ingress.py:52 (subscriptions from cfg.event_services only); agents/dogfood-content-review/agent.yaml:22-34 (services: github, email only) vs roles/manager/ROLE.md routing table rows referencing Slack DMs; contrast with eng-team/personal-assistant agent.yaml which declare the slack service with credentials.

## D060 — `agents/dogfood-content-review/roles/manager/ROLE.md:16` (doc-drift, medium, confirmed, phase 2)

**Manager routing table instructs dispatching workflows `pr-feedback` (line 16) and `pr-merged` (line 17) that do not exist in this pack.**

- Detail: A PR review with changes requested or a merged PR arrives -> the manager follows its routing table and launches `-w pr-feedback` / `-w pr-merged` -> workflow lookup fails since agents/dogfood-content-review/workflows/ contains only content-lifecycle.yaml, dogfood-content-review.yaml, research-task.yaml, smoke-test.yaml; the event goes unhandled or errors.
- Evidence: agents/dogfood-content-review/workflows/ directory listing (4 files, no pr-feedback/pr-merged); tests/test_dogfood_content_review_pack.py test_workflows_present asserts exactly that 4-file set.

## D015 — `agents/dogfood-content-review/workflows/dogfood-content-review.yaml:35` (bug, high, confirmed, phase 2)

**Route condition `issues_count > 0` uses the `>` operator, which the workflow condition parser does not support, so the fix step is skipped whenever the audit finds 2+ issues.**

- Detail: Audit step hands off issues_count: 3 -> route_fixes evaluates "3 > 0": _parse_comparison only handles ==/!=/in/not-in and falls through to the bare-truthy check, where "3".lower() in ("true","1","yes") is False -> workflow routes to `done`, which comments that the content passed review and closes the issue despite 3 findings. Only issues_count == 1 accidentally routes to `fix`.
- Evidence: bobi/workflow/variables.py:130-156 (_parse_comparison supports only ==, !=, in, not in, then bare truthy where only "true"/"1"/"yes" are True); flat substitution of issues_count happens via bobi/workflow/orchestrator.py:876-878.

## D017 — `agents/eng-team/agent.yaml:126` (config, high, confirmed, phase 2)

**auto_dispatch rule `event: github.issues.assigned` matches an event type that is never emitted (the adapter emits `github.issues` with fields.action="assigned"), so the deterministic issue-lifecycle dispatch on issue assignment is dead.**

- Detail: GitHub sends webhook header `issues` with action `assigned` -> event-server normalizes it to type `github.issues` -> AutoDispatchRule.matches compares event type exactly (`github.issues` != `github.issues.assigned`) -> rule never fires. The agent.yaml comment claims these rules fire "guaranteeing the workflow is launched regardless of prompt compliance", but assignment dispatch silently depends entirely on the director LLM. Rule should be `event: github.issues` + `match: {action: assigned}` like the sibling pull_request rules.
- Evidence: event-server/core/src/adapters/github.ts:103 (`type: github.${eventHeader}`, eventHeader is "issues"); bobi/events/reactor.py:46-53 (exact type equality in matches()); no code anywhere appends the action to the type (repo-wide grep); the only other `github.issues.assigned` reference is the correspondingly dead task-builder branch at bobi/events/reactor.py:312.

## D016 — `agents/eng-team/workflows/pr-closed.yaml:14` (bug, high, confirmed, phase 2)

**Route condition `merged == true` references bare variable `merged`, which is never in the flat scope (it arrives only as input.merged), so the close-issue step never runs for merged PRs.**

- Detail: PR merged -> auto_dispatch launches pr-closed with input_fields {merged: True, ...} -> cleanup native action returns only {status, paths_removed, branch} (the only keys set_flat'ed) -> evaluate_condition leaves bare word "merged" unresolved, compares "merged" == "true" -> False -> always routes to `done`; the linked issue is never closed by the deterministic path. Condition needs `${{ input.merged }} == true`.
- Evidence: bobi/workflow/variables.py:92-104 (_resolve_flat resolves bare names only from _flat scope); bobi/workflow/orchestrator.py:665-667 and 876-878 (only step results are set_flat; input scope at line 211-214 is never flattened); bobi/workflow/cleanup.py:64 (result dict has no `merged` key).

## D027 — `bobi/workflow/orchestrator.py:88` (bug, medium, confirmed, phase 2)

**try_resume_for_event claims the run before checking the workflow exists; if the workflow name is no longer in the installed pack, the claimed run is orphaned permanently with no recovery path.**

- Detail: A run suspends under workflow 'issue-lifecycle'; the team package is updated and the workflow is renamed/removed; the awaited event arrives. run.claim() succeeds (state file renamed to <id>.resuming.json with status 'resuming'), then find_workflow returns None and the function returns False. find_waiting skips status!='waiting', and CLI resume does WorkflowRun.load(<id>) which reads <id>.json — FileNotFoundError. The run can never be resumed or even listed correctly again, and the registry entry stays 'waiting' forever.
- Evidence: orchestrator.py:82-91: run.claim() at line 82 precedes dispatcher.find_workflow at line 88; the error path (lines 89-91) returns without renaming the .resuming.json back or marking the run failed. state.py:76-104 shows claim renames <id>.json away; state.py:117-133 shows find_waiting filters on status=='waiting'.

## D005 — `bobi/workflow/orchestrator.py:231` (bug, high, confirmed, phase 2)

**A suspended (await) run emits agent/workflow.completed: _run_workflow_async returns True on suspend and both callers treat True as terminal success.**

- Detail: Any workflow with an await step: the await branch (orchestrator.py:704-740) persists the run, emits agent/workflow.suspended, and returns True. run_workflow then sees success=True and emits agent/workflow.completed with a duration ('Workflow X completed for Y in Ns'); resume_workflow does the same at line 325-332 AND stamps the old run record status='completed'. Bus consumers (manager, monitors, webapp) see suspended immediately followed by completed, so a dormant run waiting on an external event reads as terminally finished. docs/WORKFLOW_ENGINE.md's event table says workflow.completed means 'Run reaches a terminal outcome'.
- Evidence: orchestrator.py:735-740 (suspended=True; return True) vs run_workflow:230-237 (if success: emit agent/workflow.completed) and resume_workflow:324-332 (run.status='completed'; emit completed). No suspended-vs-completed distinction is returned to the callers. Test tests/test_orchestrator.py:1213-1229 only asserts session.* events are suppressed, not workflow.completed.

## D024 — `bobi/workflow/orchestrator.py:314` (bug, medium, confirmed, phase 2)

**A launch-time --role override (and the current agent identity) is lost across suspend/resume: resume_workflow never passes role to _run_workflow_async, though launch_model/launch_effort are deliberately persisted.**

- Detail: Launch 'subagents launch -w issue-lifecycle --role reviewer' on a workflow whose steps declare agent: engineer and that contains an await step. Pre-suspend, role='reviewer' wins for the agent prompt AND model/effort resolution (run_workflow docstring: 'each wins over every step-level and config-level value for the whole run'). After the event resume, _run_workflow_async runs with role='' so steps run under the step-declared engineer agent prompt and engineer's configured model — the run silently changes identity and model mid-workflow. The resumed session also reconnects the old transcript under the new agent's system prompt, violating the agent-isolation rule at orchestrator.py:751-760.
- Evidence: resume_workflow signature (line 256-262) has no role param and the _run_workflow_async call at lines 314-320 omits role=; contrast the _runtime scope (lines 306-312, 714-719) which explicitly persists launch_model/launch_effort for exactly this reason ('a launch-time --model/--effort override survives suspension'), while role is only stored on the registry entry (line 190) and never read back.

## D029 — `bobi/workflow/orchestrator.py:546` (bug, medium, confirmed, phase 2)

**An exception from _make_session in the initial connect loop escapes both the retry try and the terminal-honesty try/finally, leaving the registry entry stuck 'running' with no session.failed or workflow.failed emitted.**

- Detail: prepare_brain_runtime() or resolve_agent_prompt() raises inside _make_session (e.g. the runtime-guard EPERM class of failure, or an unresolvable agent prompt). The call at line 546 sits before the per-attempt try (line 550) and before the main try (line 582), so the exception propagates out of asyncio.run and out of run_workflow: no agent/session.failed, no agent/workflow.failed, no registry.mark_terminal — the entry stays 'running' until the dead-man reconciler times it out and mis-reports it. This contradicts the documented contract (WORKFLOW_ENGINE.md 'Terminate honestly': the finally emits the truthful terminal event 'on any failure path').
- Evidence: orchestrator.py:544-549: 'for attempt in range(2): ... client = _make_session(...)' is followed by 'try:' at line 550; the mark_terminal/emit finally (lines 905-942) belongs to the try opened at line 582, after the loop. run_workflow (line 221) does not guard asyncio.run either.

## D025 — `bobi/workflow/orchestrator.py:848` (bug, medium, confirmed, phase 2)

**Stale handoff files are never cleared before a prompt step, so a turn that fails to write the handoff silently validates against a previous visit's (or previous run's) file and routes on stale outputs.**

- Detail: Route-loop case: 'review' rejects and jumps back to 'implement' (max_iterations loop). On the second visit the agent finishes the turn without rewriting handoff-implement.yaml; _read_handoff returns visit 1's file, validation passes instantly, and visit-1 outputs (e.g. status: done) drive the route again — the retry loop is bypassed and routing repeats the stale decision. Cross-run case: session name is deterministic (wf-<workflow>-<repo>-<run_key>, line 116-119) and registry.register only mkdir's the session dir (sdk.py:288-291), so relaunching a failed run for the same run_key reuses last run's handoff files the same way.
- Evidence: orchestrator.py:848 reads session_handoff_path(session_name, step.name) after the turn; nothing in the prompt-step path (lines 742-891), register (sdk.py:288-291), or _setup_worktree deletes handoff-<step>.yaml before the turn.

## D028 — `bobi/workflow/orchestrator.py:873` (bug, medium, confirmed, phase 2)

**A non-mapping handoff YAML (string or list) crashes the run with AttributeError instead of entering the designed missing-fields re-prompt path.**

- Detail: The agent writes prose to the handoff file (yaml.safe_load returns a str) that happens to contain the required field names as substrings, e.g. required=[complexity] and file text 'the complexity is low'. _validate_handoff's 'f not in handoff' does a substring check on the str and passes, then outputs capture calls handoff.get(k, '') — str has no .get — AttributeError propagates to the outer except (line 895) and the whole workflow fails with 'Workflow error: str object has no attribute get', with no step.failed retry and no handoff re-prompt (the exact malformed-output case MAX_HANDOFF_RETRIES exists for).
- Evidence: _read_handoff (lines 1183-1192) returns yaml.safe_load(content) or {} — only YAMLError is caught, non-dict results pass through; _validate_handoff (line 1195-1197) uses 'in' which is substring/element membership for str/list; line 873-875 then calls handoff.get(k, '').

## Q017/D026 — `bobi/workflow/variables.py:92` (simplification, medium, confirmed, phase 2)

**_resolve_flat resolves bare condition variables by regex text-substitution into the expression string before parsing, when the recursive-descent parser it feeds could simply look the names up itself.**

- Detail: The parser (_parse_value line 197) already tokenizes bare words as discrete units — resolving a bare identifier against the _flat scope at that point (pass the scope dict through _eval_expr/_parse_*) is strictly simpler and eliminates the entire textual-substitution hazard class: (a) re.sub with str(val) as replacement misinterprets backslashes in values; (b) \b-bounded substitution rewrites flat keys appearing inside quoted string literals ('the status field' with a flat key `status` gets rewritten); (c) values containing spaces or the words 'and'/'or'/'in' corrupt the token stream before the parser ever runs (a handoff output like complexity='medium or large' changes the parse tree). Behavior is preserved for every intended case (bare word == literal, in-list, truthy check — all exercised in tests/test_variables.py) because lookup-at-parse produces the same string values the substitution was trying to splice in, minus the corruption cases. ${{scope.key}} resolution can stay as the pre-pass it is today. Only one production caller (orchestrator.py:635), so the change is contained.
- Evidence: variables.py lines 92-104 (re.sub over the raw expression per flat key) vs _parse_value lines 197-199 which already isolates bare words; sole caller ctx.evaluate_condition at orchestrator.py:635; tests in tests/test_variables.py and tests/test_orchestrator.py:379-391 cover only simple single-word values.
- Cross-pass (D026): Route conditions are evaluated by textually substituting step-output values into the expression before parsing, so any multi-word output value mangles the grammar and mis-evaluates the condition.

## D038 — `bobi/brain/codex_config.py:214` (bug, medium, confirmed, phase 3)

**write_codex_config rewrites $CODEX_HOME/config.toml non-atomically, so a crash mid-write truncates the file and permanently loses the foreign (user/operator) settings the module promises to preserve.**

- Detail: First session construction after an MCP set change triggers a rewrite of config.toml (which also carries foreign keys: model settings, profiles, anything the operator or `codex mcp add` wrote). The process is killed mid `path.write_text(rendered)` (supervisor restart, OOM, deploy roll) -> config.toml is left truncated: foreign content is gone for good and the next `codex exec` reads a torn/invalid TOML. The sibling managed-block renderer, bobi/brain/instructions.py:170-174, uses temp-file + os.replace specifically because 'a process killed mid-write would otherwise truncate' foreign-owned content.
- Evidence: bobi/brain/codex_config.py:206-215: `existing = path.read_text() ...; if rendered != existing: home.mkdir(...); path.write_text(rendered)` — no temp+rename; contrast bobi/brain/instructions.py:168-174 (`tmp.write_text(...); os.replace(tmp, path)`) and its docstring rationale.

## Q039 — `bobi/monitors/scheduler.py:1470` (consistency, medium, confirmed, phase 3)

**Durable JSON state is persisted in two conflicting styles: crash-safe tmp+os.replace in some modules vs plain write_text in others, including within the same monitors subsystem.**

- Detail: The house pattern for crash-safe state is tmp write + os.replace, with the rationale spelled out in bobi/monitors/script_cache_checks.py:601-609 ('so a crash or fleet-churn kill mid-write can't truncate it into corrupt JSON') and also used in bobi/workflow/state.py:95, bobi/launch_admission.py:325, and bobi/brain/instructions.py:174. Deviants write the same class of state file with bare write_text(json.dumps(...)): scheduler._save_state (this line), bobi/spend_governor.py:52, bobi/events/client.py:43 (event cursor), bobi/sdk.py:291/312/358, bobi/setup/state.py:231. The cost is concrete for the scheduler: its own _load_state (line 1461) treats corrupt JSON as 'resetting', so a kill mid-write silently drops all monitor last_run/last_spawn state and monitors re-fire; the cursor and spend-window files have the same torn-write exposure. Better shape: one shared write_json_atomic(path, obj) helper (paths.py is a natural home) used by both camps — same bytes at the same path, just via tmp+replace, so behavior is preserved except that torn writes can no longer occur.
- Evidence: grep of write_text(json.dumps across bobi/ found 13 plain-write sites in 10 files, vs the atomic tmp+os.replace pattern at script_cache_checks.py:601-609 (with docstring naming the corruption risk), workflow/state.py:80-97, launch_admission.py:324-325, brain/instructions.py:174; scheduler.py:1461-1464 shows the load side already handling 'Corrupt monitor state ... resetting'.

## D034 — `bobi/setup/state.py:231` (bug, medium, confirmed, phase 3)

**SetupState.save writes setup.json with a bare non-atomic write_text and no locking while handlers run concurrently in FastAPI's threadpool, so an interleaved or interrupted save corrupts the file and load() then silently returns None, losing all resume state.**

- Detail: Two web UI actions land concurrently (every sync route runs in the threadpool and calls state.save; the async /api/message stream also saves mid-turn): both open setup.json with 'w' (truncate) and interleave writes, or the process is killed (Ctrl-C on the foreground server) mid-write. The file is left as truncated/interleaved non-JSON. On `bobi setup --resume`, SetupState.load hits json.JSONDecodeError and returns None, so serve() prints 'No setup in progress to resume' — the entire in-progress setup (spec, transcript, credentials-saved list) is silently discarded instead of resumed.
- Evidence: state.py:227-231 `path.write_text(json.dumps(data, indent=1))` (no temp-file+rename, no lock); state.py:238-242 load() returns None on JSONDecodeError; webui/server.py routes are sync `def` (threadpooled per the module docstring, server.py:4-6) and nearly every handler calls state.save(project) on the shared SetupState instance.

## D087 — `bobi/spend_governor.py:83` (bug, low, confirmed, phase 3)

**record_invocation is an unlocked, non-atomic read-modify-write of spend_governor.json, so concurrent recorders lose timestamps and the rolling-hour cap undercounts; the plain write_text can also leave torn JSON that resets the window to empty.**

- Detail: Two dispatch threads finish launches concurrently (record_invocation at subagent.py:1089 runs outside _LAUNCH_ADMISSION_LOCK, and CLI-initiated spawns run in separate processes): both _load_state the same list, both append their own timestamp, and the second _save_state overwrites the first — one invocation vanishes from the runaway-loop backstop. A crash mid write_text leaves invalid JSON, which _load_state maps to [], zeroing the whole window.
- Evidence: spend_governor.py:83-89 load→prune→append→save with no file lock (contrast launch_admission.record_init_health, which takes an fcntl lock and writes tmp+os.replace); _save_state (lines 48-54) uses bare state_file.write_text. Caller ordering at bobi/subagent.py:1012 (_check_spend_governor, pre-lock) and 1088-1089 (record_invocation, post-lock).

## D092 — `bobi/workflow/state.py:36` (duplication, low, confirmed, phase 3)

**The atomic-write pattern (serialize, write tmp sibling, os.replace over target) is independently re-implemented in at least five places instead of one shared atomic_write_text helper.**

- Detail: Each site hand-rolls its own tmp naming and error handling, so durability fixes don't propagate: workflow/state.py:44 uses .{run_id}.json.tmp, script_cache_checks.py:607 uses .with_suffix('.json.tmp'), launch_admission.py:323 embeds pid+time_ns, brain/instructions.py:172 uses .bobi-tmp — while other state writers (e.g. monitors/scheduler._save_state at scheduler.py:1470, spend_governor._save_state at spend_governor.py:52) write non-atomically and can be truncated by the very crash the five copies each individually guard against. A single shared helper would consolidate the five copies and give the non-atomic writers the fix for free.
- Evidence: Five sites: bobi/workflow/state.py:43-46; bobi/monitors/script_cache_checks.py:600-609 (_save_trusted_state) and 837-841 (pin path); bobi/launch_admission.py:321-325 (record_init_health); bobi/brain/instructions.py:170-174 (write_instructions).

## Q062/D071 — `bobi/workflow/state.py:89` (simplification, low, confirmed, phase 3)

**WorkflowRun.claim() writes the temp file before attempting the atomic claim, forcing a lose-path cleanup branch and a two-step double os.replace dance.**

- Detail: Current order: write tmp → os.replace(src, dst) (the actual claim) → os.replace(tmp, dst), with a FileNotFoundError handler that must also unlink the orphaned tmp. Reordering to claim first — os.replace(src, dst); then tmp.write_text(data); os.replace(tmp, dst) — preserves every guarantee (single winner still decided by the one atomic rename of src; updated status still lands atomically via the tmp rename) while deleting the loser-side tmp cleanup entirely: a losing process now does zero writes. Same exception surface, same file states observable by find_waiting (status='waiting' file gone the moment a winner exists).
- Evidence: state.py:76-104; the tmp file exists only to carry the status='resuming' update, which is only needed after the claim is won; tests/test_workflow_state.py:185-231 exercise claim semantics (winner/loser/find_waiting exclusion), all of which hold under the reordering.
- Cross-pass (D071): claim()'s two-step rename leaves a crash window in which the run becomes permanently unresumable: after os.replace(src,dst) the .resuming.json still holds status 'waiting' until the second replace overwrites it.

## D009 — `bobi/brain/codex.py:369` (bug, high, confirmed, phase 4)

**CodexBrain.make_session clears the bobi-managed mcp_servers block from $CODEX_HOME/config.toml whenever a call site omits options['mcp_servers'], silently stripping MCP tools from every live codex session.**

- Detail: Codex-brained team with MCP deps: manager boot (service.py:650 passes cfg.mcp_servers) renders servers into config.toml. A monitor check/gate fires (_run_agent_supervised, bobi/subagent.py:402) or a workflow step starts (orchestrator _make_session, bobi/workflow/orchestrator.py:441) — both build options without mcp_servers. make_session then computes mcp_servers={} but config_has_managed_block(home)=True, so write_codex_config({}, home) removes the managed block. Every codex turn is a fresh `codex exec` subprocess that re-reads config.toml at start, so the manager and all other sessions immediately lose their MCP servers mid-flight, with no error, until a session that does pass mcp_servers re-renders — config flaps on every monitor interval.
- Evidence: bobi/brain/codex.py:367-370 `mcp_servers = opts.get("mcp_servers") or {}` ... `if mcp_servers or config_has_managed_block(home): write_codex_config(mcp_servers, home)`; render_config with empty set returns foreign-only text (codex_config.py:192-197). Omitting call sites verified: bobi/workflow/orchestrator.py:441 `options = {"max_turns": 200, "skills": "all"}` and bobi/subagent.py:402-406 (no mcp_servers key), vs. sites that do pass it (bobi/subagent.py:588, 747; bobi/service.py:650).

## D041 — `bobi/build_render.py:237` (bug, medium, confirmed, phase 4)

**The image-build `verify: requires` step sets BOBI_VERIFY_PHASE as an unexported shell variable (`BOBI_VERIFY_PHASE=build; <check>`), so any check that runs a subprocess reading the env var never sees the build phase — diverging from dep_bootstrap.preflight, which exports it in the real environment.**

- Detail: A team writes a requires check as a script, e.g. `check: python3 tools/verify.py`, that branches on os.environ['BOBI_VERIFY_PHASE'] per the documented two-tier contract (tool_library.py:14-18). During the image build the subprocess sees the var unset (confirmed: `bash -lc 'BOBI_VERIFY_PHASE=build; python3 -c ...'` prints None), so it takes the runtime-tier branch — e.g. a credentialed probe in the credential-less build — and the image build fails spuriously (or passes when the stricter build branch should have run). The same check behaves differently under dep_bootstrap.preflight (dep_bootstrap.py:331 sets env['BOBI_VERIFY_PHASE']), so the two verify surfaces of the one `success` contract disagree. The shipped codex entry survives only because its check uses inline shell expansion of ${BOBI_VERIFY_PHASE:-}.
- Evidence: build_render.py:237 `shlex.quote('BOBI_VERIFY_PHASE=build; ' + entry.check)` vs dep_bootstrap.py:331 `env_base["BOBI_VERIFY_PHASE"] = phase` (exported). Bash repro shows subprocess gets None while `$BOBI_VERIFY_PHASE` expands to 'build' in the same shell.

## Q122/D064 — `bobi/cli.py:789` (dead-code, low, unverified, phase 4)

**`bobi setup --resume` is parsed, documented in help ('Resume an interrupted setup'), then immediately discarded with `del resume`.**

- Detail: Since setup was rerouted through the webapp (the command just opens #/setup in the browser), the flag does nothing — but --help still advertises it and the docstring says '`--resume` picks up where you left off'. Either remove the option or stop documenting behavior it no longer has; if the web UI auto-resumes, the flag and its help text are pure dead surface.
- Evidence: Read bobi/cli.py:786-814: @click.option('--resume', ...) at line 790, `del resume` at line 800, body only opens the webapp URL. grep '^\s*del ' bobi/cli.py confirms this is the only discarded param.
- Cross-pass (D064): `bobi setup --resume` is documented as resuming an interrupted setup but the flag is a declared no-op (`del resume`).

## D018 — `bobi/cli.py:2710` (bug, medium, confirmed, phase 4)

**`bobi agent <name> event-server stop` crashes with an unhandled traceback on a corrupt/empty event-server.pid and on PermissionError, instead of cleaning up.**

- Detail: State: state/event-server.pid exists but is empty or contains garbage (e.g. partial write after a crash). Run `bobi agent eng event-server stop` -> `int(pid_file.read_text().strip())` raises ValueError, printing a raw traceback and leaving the stale pid/port files in place; every subsequent stop hits the same crash. Similarly, a pid owned by another user raises uncaught PermissionError from os.kill.
- Evidence: cli.py:2710 `pid = int(pid_file.read_text().strip())` with only `except ProcessLookupError` at 2714. Contrast the manager paths which defend against exactly this: cli.py:963-967 (`except (ValueError, OSError): click.echo("Invalid PID file — cleaning up.")`) and service.py stop_team (reads via read_pid, handles invalid pid + PermissionError).

## D066 — `bobi/cli.py:3019` (bug, low, confirmed, phase 4)

**`bobi agents update` (update-all path) exits 0 even when every pack update fails, so scripts/CI cannot detect failure.**

- Detail: With cached packs present and the network/registry down, `bobi agents update` prints '  <pack> — failed: ...' to stderr for each pack inside the per-pack try/except but never raises SystemExit, returning exit code 0. The single-pack path (cli.py:3001-3003) correctly exits 1 on the same failure, so the two invocation forms report contradictory exit codes for identical failures.
- Evidence: cli.py:3009-3020: `except Exception as e: click.echo(f"  {pack['name']} — failed: {e}", err=True)` with no exit-code tracking after the loop; contrast cli.py:3001-3003 `click.echo(f"Failed: {e}", err=True); raise SystemExit(1)` in the named-pack branch.

## D065 — `bobi/cli.py:3110` (bug, low, confirmed, phase 4)

**`bobi agents browse` crashes with 'Unknown format code s' when a registry.yaml declares an unquoted numeric version (e.g. version: 1.0), because the yaml-parsed float is fed to a `:8s` format spec.**

- Detail: A third-party registry added via `bobi agents add-registry` publishes agents/registry.yaml with `version: 1.0` (YAML parses this as float, not string). `bobi agents browse` -> f"v{version:8s}" raises ValueError('Unknown format code s for object of type float') and the whole browse command dies with a traceback instead of listing packs. The `local_v == version` comparison at cli.py:3104 also silently mismatches str vs float.
- Evidence: bobi/registry.py:418-421 `_list_remote_single` returns `{**info}` verbatim from yaml.safe_load with no str() coercion of version; cli.py:3099 `version = pack.get("version", "?")` then cli.py:3110 `f"  {name:20s} v{version:8s} [{status}]"`.

## D039 — `bobi/compose.py:659` (bug, medium, confirmed, phase 4)

**_merge_keyed_list crashes with a raw TypeError when an overlay list removes and re-adds the same keyed entry, because the tombstone (None) left at the entry's slot is passed to _deep_merge_dict.**

- Detail: An overlay layer wanting to wholesale-replace an inherited service (the natural idiom, since same-key entries field-merge) writes `services: [{name: x, remove: true}, {name: x, command: ...}]`. Line 656 sets result[index['x']] = None but leaves 'x' in `index`; the re-add hits line 659 and _deep_merge_dict(None, entry) -> dict(None) raises TypeError, crashing `bobi agents install`/deploy with an unhandled traceback instead of composing or raising ComposeError.
- Evidence: Reproduced: `_merge_keyed_list([{'name':'x','cmd':'a'}], [{'name':'x','remove':True},{'name':'x','cmd':'b'}], 'name')` raises TypeError: 'NoneType' object is not iterable at compose.py:698 via compose.py:659. Line 656 tombstones without removing the name from `index`.

## D040 — `bobi/compose.py:767` (bug, medium, confirmed, phase 4)

**_prune_one performs no validation of prune names, so an absolute path or `..` in a `prune:` entry deletes files/directories outside the compose staging dir on the host.**

- Detail: Any layer's agent.yaml (including a base team fetched from the registry) declaring `prune: {roles: ["/Users/dev/project"]}` makes line 767 compute `dest / "roles" / "/Users/dev/project"` == Path("/Users/dev/project") (pathlib absolute-join replaces the base), and line 769 shutil.rmtree()s it during `bobi agents install`/deploy on the host. `prune: {tools: ["../../../../home/user/file"]}` similarly escapes via lines 776-788 (unlink/rmtree). SECURITY.md calls team packs trusted code, but compose deleting arbitrary host paths outside the image it is freezing exceeds even that model and turns a typo into host data loss.
- Evidence: Verified `Path('/tmp/stage') / 'roles' / '/etc/passwd'` == Path('/etc/passwd'). Grep across bobi/ shows no other site validates prune names (only compose.py handles `prune:`); reject_path_from (compose.py:795) guards only `from:`, and validate.py never inspects prune.

## D085 — `bobi/config.py:500` (bug, low, confirmed, phase 4)

**Config._parse crashes on null-valued YAML keys: `event_server:` with an empty value raises AttributeError (None.get) and `spend_cap:` raises TypeError (int(None)), bricking Config.load and with it every start/status/dispatch path.**

- Detail: A team author leaves `event_server:` or `spend_cap:` in agent.yaml with the value commented out/empty (YAML null). Config.load raises instead of returning a config with defaults, so `bobi agent <name> start` and everything else that loads config dies with a traceback rather than a validation message.
- Evidence: Reproduced in the repo venv: Config._parse on 'agent: demo\nevent_server:\n' -> AttributeError "'NoneType' object has no attribute 'get'" (line 500 `event_server.get("url", "")` after `raw.get("event_server", {})` returns None for a present-but-null key); 'spend_cap:\n' -> TypeError at line 518 `int(raw.get("spend_cap", 0))`.

## D086 — `bobi/costs.py:183` (bug, low, confirmed, phase 4)

**rollup_costs guards token fields against non-numeric values via _tok but not the cost fields, so a string total_cost_usd (or cost_usd) in one state.json raises TypeError and crashes the whole fold — contradicting the module's own must-not-500 invariant.**

- Detail: One hand-edited or corrupt sessions/<name>/state.json carries "total_cost_usd": "0.5". `data.get("total_cost_usd") or 0.0` returns the string (truthy), then `summary.total_cost_usd += cost` raises TypeError, so the spend web endpoint / `bobi costs` fails for the entire team instead of skipping the one bad session.
- Evidence: costs.py:148-153 _tok exists precisely because "a hand-edited state.json can carry a string count, and the fold backs a web endpoint that must not 500 on one malformed session", yet line 183 `cost = data.get("total_cost_usd") or 0.0` and line 199 `usage_cost = usage.get("cost_usd") or 0.0` apply no isinstance guard before `summary.total_cost_usd += cost` (line 189).

## D020 — `bobi/doctor.py:35` (bug, medium, confirmed, phase 4)

**run_doctor unconditionally runs the Claude CLI and Claude auth checks as required failures, so doctor reports broken health (exit 1) on hosts running codex/gateway-brain teams that legitimately have no claude binary.**

- Detail: A team with `brain: {kind: codex}` (or an OpenAI-format gateway) installed on a host without the claude CLI: `bobi agent <n> doctor` -> _check_claude_cli returns ok=False required=True and _check_claude_auth returns 'claude not installed' -> doctor prints errors and exits 1 even though the team's actual brain is healthy. The rest of the stack is brain-aware (validate.py _check_brain handles kind: codex/gateway; cli.py:499 reads Config.brain_kind to drive dep installs), but doctor never consults the brain config.
- Evidence: doctor.py:35-36 append _check_claude_cli()/_check_claude_auth() unconditionally with required=True default (doctor.py:24); brain-kind awareness exists at validate.py:150-219 (_check_brain, engine == 'codex' branches) but is never used to gate these two checks.

## D019 — `bobi/doctor.py:386` (bug, medium, confirmed, phase 4)

**_check_event_server probes hardcoded http://localhost:8080, producing a false required failure (doctor exit 1) for remote-configured instances that haven't registered yet and for local servers on non-default ports.**

- Detail: Config has event_server_url pointing at the production worker (e.g. https://bobi-events...), agent not yet started so state/deployments/ has no *.json -> `cfg.event_server_url and registered` is False -> falls through to health('http://localhost:8080') -> 'Event server: not running' as a required failure, doctor exits 1 with a hint to start the LOCAL server, which is wrong guidance. Same false failure when the local server runs on a configured non-8080 port: `bobi agent <n> event-server status` finds it via _selected_local_event_server_port (cli.py:113-148) but doctor says not running.
- Evidence: doctor.py:380-391: only returns 'remote' when deployments are registered, then `url = "http://localhost:8080"` unconditionally. cli.py:2731-2752 (event_server_status) shows the correct logic: treat a remote configured URL as remote regardless of registration and resolve the local port from the port file/config.

## D075 — `bobi/events/adapters.py:106` (bug, low, confirmed, phase 4)

**_parse_github_url uses a substring match for 'github.com', so GitHub Enterprise hosts like github.company.com are mis-parsed into a garbage org/repo slug and a wrong subscription topic.**

- Detail: Remote URL https://github.company.com/acme/widget.git: 'github.com' matches the first 10 chars of the hostname, split('github.com')[-1] yields 'pany.com/acme/widget', producing slug 'pany.com/acme' and auto-detected subscription 'github:pany.com/acme'. The agent subscribes to a topic no event will ever match (and #488 grant authorization then fails against the real GitHub API, dropping the topic with a misleading credential warning) instead of skipping the non-github.com remote.
- Evidence: adapters.py:101-110: `if "github.com" in url: parts = url.split("github.com")[-1].lstrip(":/").split("/")` — no host-boundary check; 'github.company.com'[:10] == 'github.com'.

## D031 — `bobi/events/server.py:706` (bug, medium, confirmed, phase 4)

**register_slack_workspaces ignores the HTTP status of the signed POST /slack/workspaces and logs success (returning [team_id]) even when the server rejected the registration.**

- Detail: Event server restarts and forgets the bubble (or otherwise returns 403/500 for the registration). signed_request returns the response without raising on status, so the function falls through to log.info('Registered Slack workspace ...') and returns [team_id]. The bubble-scoped outbound record (#487) and slack resource grant are never written: the agent believes Slack is registered, self-reply loop prevention/outbound sends are silently broken, and the subagent fallback path (_register_channel_credentials retries unsigned on exception) never triggers because no exception is raised.
- Evidence: server.py:705-714 calls signed_request(...) and unconditionally logs success + returns [team_id]; bobi/events/signing.py:112 returns pooled.request(...) with no raise_for_status (and bobi/http.py helpers never raise on status). Contrast register_whatsapp_numbers (server.py:747 'if resp.status_code != 200: ... return []') and register_discord_apps (server.py:787), which check the status.

## D069 — `bobi/history.py:188` (bug, low, confirmed, phase 4)

**_project_from_path replaces every '-' with '/', mangling project names for any repo with a hyphen in its name (including bobi-agent itself), which breaks the --project filter in search.**

- Detail: Claude encodes cwd '/Users/z/dev/bobi-agent' as directory '-Users-z-dev-bobi-agent'. `.replace('-', '/', 1).replace('-', '/')` (the count=1 call is redundant — the second replaces all) yields '/Users/z/dev/bobi/agent'. conversations.project stores the mangled value, so `search(query, project='bobi-agent')` (LIKE '%bobi-agent%') matches nothing for the very sessions it targets, and the displayed project path is wrong.
- Evidence: history.py:187-188: `return file_path.parent.name.replace("-", "/", 1).replace("-", "/")`; search() filters with `c.project LIKE ?` on '%<project>%' (history.py:346-348).

## D022 — `bobi/history.py:262` (bug, medium, confirmed, phase 4)

**_index_file counts a trailing partially-written JSONL line as read, so once the writer completes that line it is never indexed — the message is permanently missing from the index and from the sleep-cycle delta.**

- Detail: The background indexer (start_background_indexer, 120s cadence; also scheduler's history.index() before each sleep cycle) reads a transcript while Claude Code is mid-append. read_text().splitlines() (line 199) includes the incomplete final line; json.loads fails and the line is skipped (lines 220-222), but `lines_read = len(lines)` (lines 262-265) records it as consumed. On the next pass the now-complete line satisfies `len(lines) <= skip` for its index and is never re-read. That message never reaches the messages table, so messages_since() (the sleep-cycle transcript delta, #456) silently drops it forever.
- Evidence: history.py:199 reads all lines; 218-222 skip unparseable lines with no adjustment; 262-265 write index_state with lines_read=len(lines) unconditionally, and 207-208 (`if len(lines) <= skip: return 0`) prevents any later re-read of the completed line.

## D068 — `bobi/history.py:316` (bug, low, confirmed, phase 4)

**_fts_query breaks on queries containing a double quote (or an all-whitespace query), producing invalid FTS5 syntax that raises sqlite3.OperationalError to the caller.**

- Detail: `bobi agent eng transcript search 'fix "auth" bug'` (cli.py:2103-2105, no guard): the token `"auth"` is wrapped to `""auth""`, which FTS5 rejects ('fts5: syntax error'), and an all-whitespace query yields an empty MATCH expression — either way search() raises sqlite3.OperationalError and the CLI shows a traceback instead of results. FTS5 requires embedded quotes to be doubled inside the phrase, not naively wrapped.
- Evidence: history.py:315-319: `quoted = [f'"{t}"' for t in tokens if t]` performs no escaping of `"` within tokens and returns '' for empty input; search() at 352 executes the MATCH unguarded.

## D010 — `bobi/kb/embedder.py:127` (bug, high, confirmed, phase 4)

**embed()'s dead-sidecar recovery catches OSError, but the pooled httpx client raises httpx.ConnectError (not an OSError subclass), so the restart-and-retry path is dead code and _verified_port stays pinned to the dead port forever.**

- Detail: Sidecar answers one embed (caching _verified_port), then dies (OOM/manual kill). Every later embed() call posts to the dead port, httpx.ConnectError propagates uncaught, and because _verified_port is never invalidated the process never calls ensure_running() again — cold-memory sync and KB indexing fail on every retry until the whole manager process restarts.
- Evidence: embed() does `except OSError:` around _post_embed; _post_embed uses bobi.http (httpx). Verified in the repo venv: `issubclass(httpx.ConnectError, OSError)` is False and a connection-refused GET through bobi.http raises httpx.ConnectError. On failure `_verified_port = None` is never executed and `_verified_port = port` at the end is skipped, so the stale port is reused on every subsequent call (ensure_running() is only reached when _verified_port is None).

## D043 — `bobi/kb/store.py:127` (bug, medium, confirmed, phase 4)

**_fts_query wraps each whitespace token in double quotes without escaping embedded double quotes, so any query token containing an odd number of '"' characters produces malformed FTS5 syntax and search() raises.**

- Detail: Agent or user runs `bobi recall-memory 'the 5" display bug'` (or kb search with any quote-bearing text); the FTS5 MATCH raises 'unterminated string', crashing the search/recall instead of returning results. FTS5 requires embedded quotes to be doubled.
- Evidence: Reproduced with apsw against a real fts5 table: _fts_query('lone" quote') -> '"lone"" OR "quote"' -> apsw error 'unterminated string'. _fts_search does not catch it, and callers (cli.py kb_search line 3209, recall-memory line 3312) have no handler.

## D045 — `bobi/manager_health.py:30` (bug, medium, confirmed, phase 4)

**The health endpoint uses a single-threaded HTTPServer with no handler timeout, so one half-open or stalled client connection blocks /health and /ready for all subsequent probes indefinitely.**

- Detail: Manager runs in a container with BOBI_HEALTH_BIND=0.0.0.0 for orchestrator probes. Any client that connects and holds the socket without completing an HTTP request (port scanner, stalled proxy, misbehaving checker) parks the handler in rfile.readline() forever — BaseHTTPRequestHandler.timeout is None and _HealthServer is plain HTTPServer (serial serve_forever). The supervisor's next liveness probes queue behind it and time out, so a healthy manager is diagnosed unhealthy and restarted.
- Evidence: manager_health.py:30-31 `class _HealthServer(HTTPServer): allow_reuse_address = True` — no ThreadingMixIn and no `timeout` set on the handler class built in _make_handler (lines 34-86); _thread runs `_server.serve_forever` (line 188), which handles one connection at a time.

## D004 — `bobi/monitors/script_cache_checks.py:978` (bug, high, confirmed, phase 4)

**script_cache self-heal invokes the blocking agent runtime synchronously on the single scheduler thread, stalling every other monitor for minutes.**

- Detail: The scheduler runs on one thread: `_loop` calls `tick()`, which iterates monitors and, for a `check:` monitor, calls `self._check_conditions(monitor, registry)` -> `check(monitor, projects)` inline (scheduler.py:637-643, 753, 965). For a `script_cache` monitor, the first tick it is due (no active script) — and every tick a cached script fails or the fingerprint changes — `_run_active` returns False and `script_cache` calls `_self_heal` -> `generate_candidate` -> `bobi.subagent.run_check_blocking` (script_cache_checks.py:978, 738). `run_check_blocking` blocks until the agent finishes or times out, retrying up to attempts=2 each bounded by CHECK_TIMEOUT=600s (subagent.py:1710-1717), i.e. up to ~20 minutes. While it blocks, `tick()` cannot return, so no other monitor is evaluated: interval monitors drift and a weekday-gated `at:` slot whose scheduled instant passes during the block gets treated as a missed-while-down catch-up and is skipped (D8, scheduler.py:688-692, grace only 60s). This contradicts the framework's own design — the description-only check flavor deliberately spawns out-of-band via `_spawn_check` precisely so the scheduler thread is 'never blocked' (scheduler.py:1014-1031) — while the native `script_cache` check does the same expensive agent call inline.
- Evidence: bobi/monitors/scheduler.py:753 (`conditions = self._check_conditions(monitor, registry)` runs synchronously in `run_monitor`, called from the single `monitor-scheduler` thread's `tick`); bobi/subagent.py:1710-1717 (`run_check_blocking` blocks, attempts=2 x timeout=600)

## D023 — `bobi/monitors/tool_checks.py:151` (bug, medium, confirmed, phase 4)

**tool_poll/venn_poll cache the resolved command keyed only on monitor name with no config fingerprint, so editing a monitor's command/query keeps silently running the stale cached script.**

- Detail: `_run_command` tries `_run_cached_script(monitor_name, ...)` first (tool_checks.py:151); the cached `.sh` is keyed only by monitor name (`_script_path`, line 67-69) and is only ever invalidated when it exits non-zero (line 176-180). It is never checked against the current command/query. Scenario: a user edits a tool_poll monitor's `command:` (or a venn_poll monitor's `service`/`tool`/`query`) in monitors.yaml. On the next tick the OLD cached script still exits 0 with parseable JSON, so `_run_command` returns those conditions and never runs the new `cmd` nor re-caches — the monitor polls the old target indefinitely. Unlike script_cache_checks.py, which invalidates via `_fingerprint` over prompt/id_field/extra (script_cache_checks.py:618-624, 937), tool_checks has no equivalent, so a config change to a working tool_poll/venn_poll monitor is silently ignored until the stale script happens to fail.
- Evidence: bobi/monitors/tool_checks.py:67-69 (script path keyed on name only), 176-180 (cache invalidated only on non-zero exit); contrast bobi/monitors/script_cache_checks.py:937 (`if state.get('fingerprint') != fp: ... return False` regenerates on config change)

## D032 — `bobi/registry.py:221` (bug, medium, confirmed, phase 4)

**fetch() for an unpinned team silently downgrades 'latest published version' to the rolling main-push tarball when the remote version read transiently fails.**

- Detail: `bobi install eng-team` (no @version) while the raw.githubusercontent.com agent.yaml fetch times out (timeout=5) or is rate-limited: _read_remote_version catches ALL exceptions and returns None (lines 147-151), fetch treats None as 'version-less team' (line 221) and _asset_url(repo, name, None) resolves the rolling <name>.tar.gz (line 48), which is clobbered on every main push. The user silently installs unreleased main content instead of the latest published immutable asset, with no warning distinguishing 'version-less team' from 'version read failed'.
- Evidence: registry.py:143-151 `_read_remote_version` -> `except Exception: return None`; registry.py:221-222 `target = version if pinned else _read_remote_version(name, repo); asset_url = _asset_url(repo, name, target)`; registry.py:48 `fname = f"{name}-{version}.tar.gz" if version else f"{name}.tar.gz"` where the docstring (lines 200-204) says rolling is only for version-less teams (D-5).

## D044 — `bobi/runtime_guard.py:242` (bug, medium, confirmed, phase 4)

**with_mutable_runtime_package runs the strict +w sweep before entering its try/finally, so an EPERM partway through the unlock leaves every already-chmodded file writable with no readonly rollback.**

- Detail: Container/team package image contains one file owned by another uid (the exact class #774 handled for the readonly direction). `bobi update`/install enters with_mutable_runtime_package; _chmod_tree(_mutable_mode, strict=True) chmods N files +w then raises OSError on the unowned file. The exception propagates from __enter__ before the try/finally exists, so the finally readonly re-lock never runs: the protected team-package tree stays partially writable (doctor's check_runtime_write_policy fails) until the next subagent spawn re-runs prepare_brain_runtime.
- Evidence: runtime_guard.py:239-247 — `_chmod_tree(package, _mutable_mode, strict=True)` executes before `try: yield / finally: _chmod_tree(package, _readonly_mode)`; _chmod_tree with strict=True re-raises OSError mid-iteration (line 85-86) after earlier entries were already chmodded. Callers: install.py:42,182 and cli.py:2456,2484,2511.

## D035 — `bobi/setup/actions.py:361` (bug, medium, confirmed, phase 4)

**install_team only enforces the validated/hash-freshness gate when state.mode == 'create', so open/modify-mode teams can be installed from unvalidated or since-edited source, contradicting the function's own docstring and the INSTALL hard floor.**

- Detail: Open mode: user validates (validated=True), then edits agent.yaml through the review editor introducing a YAML parse error — POST /api/file sets validated=False and clears validated_hash. The user (or the still-rendered install button) POSTs /api/install: the endpoint has no gate of its own, install_team's staleness check is inside `if state.mode == "create":`, so the broken pack is copied into run/package/ and marked installed. In create mode the identical sequence raises ActionError ('run validate_team again'). The docstring promises 'Raises ActionError if the source is missing or its validation is stale' with no mode caveat, and state._hard_floor requires validated for INSTALL in both modes.
- Evidence: actions.py:361-368 — the `current != state.validated_hash` check is guarded by `if state.mode == "create":`; webui/server.py:1397-1406 /api/install calls install_team with no stage or validated check; state.py:193-194 `_hard_floor` requires `self.validated` for Stage.INSTALL regardless of mode; docstring at actions.py:350-351.

## D036 — `bobi/setup/authoring.py:296` (bug, medium, confirmed, phase 4)

**merge_agent_yaml claims chat is a setup-managed overlay key but never removes or overwrites an existing `chat:` when the user switches the team to CLI, so a slack->cli change silently does not take effect in the generated agent.yaml.**

- Detail: Open an existing team whose agent.yaml has `chat: slack` (reverse_fill sets state.chat='slack'); in the conversation the user says they'll talk to the team from the command line, digestion sets state.chat='cli'; at Build, merge_agent_yaml runs `if state.chat and state.chat != "cli": cfg["chat"] = state.chat` — the condition is false, the existing `chat: slack` key is left in cfg, and the merged agent.yaml still declares slack chat (the slack service entry is likewise kept by the services union). The team continues to run the Slack chat adapter despite the user's explicit switch; only slack->telegram style switches take effect.
- Evidence: authoring.py:296-297 (`if state.chat and state.chat != "cli": cfg["chat"] = state.chat` with no else-pop), contradicting the docstring at authoring.py:245-246: 'overlay the keys setup manages (entry_point, chat) onto the existing config'. reverse_fill (open_mode.py:250) seeds state.chat from the pack, apply_deltas (digestion.py:308-309) sets chat='cli' from the conversation.

## D033 — `bobi/setup/webui/server.py:637` (bug, medium, confirmed, phase 4)

**_resolve_pending writes a pre-probe snapshot of the MCP entry back into state.spec.mcp_servers after an up-to-60s await, silently reverting any edit the user saved via /api/mcp/add while the test was running.**

- Detail: User asks to test a stdio connection and confirms; _resolve_pending captures `entry = state.spec.mcp_servers.get(key)` then awaits mcp_probe.probe (timeout 60s, first run resolves deps). Meanwhile the user edits the connection (fixes command/args/env_vars) and saves — /api/mcp/add replaces state.spec.mcp_servers with the corrected entry. When the probe finishes, `state.spec.mcp_servers[key] = entry` writes the stale pre-edit entry (plus last_test) back, and _record() persists it — the user's correction is silently lost. The code handles removal-during-test (entry missing) but not edit-during-probe.
- Evidence: server.py:619 `entry = (state.spec.mcp_servers or {}).get(key)`; server.py:631 `result = await mcp_probe.probe(entry, project, call_name=tool)`; server.py:634-637 `entry["last_test"] = {...}; state.spec.mcp_servers[key] = entry`; /api/mcp/add (server.py:1060-1064) rebuilds and reassigns `state.spec.mcp_servers` concurrently in the threadpool (sync route). The comment at server.py:621-622 acknowledges the edit/remove window but only guards the missing-entry case.

## D007 — `bobi/setup/webui/server.py:953` (bug, high, confirmed, phase 4)

**/api/mcp/detect (and the /api/browse folder picker) confine paths to BOBI_HOME (~/.bobi by default), not the user's home directory the comments and error message claim, so pointing detect at any real MCP project folder is rejected.**

- Detail: Default install (no BOBI_HOME override): user opens the add-connection form and pastes '~/dev/substack-mcp' (a folder genuinely inside their home directory) into the detect field. _within_home resolves it and checks containment against home = paths.home_dir() = ~/.bobi; the path is outside ~/.bobi, so the endpoint returns 400 'pick a folder inside your home directory' — a message the path already satisfies. Since virtually no MCP server project lives under ~/.bobi, the detect feature always fails; same boundary makes the /api/browse picker unable to leave ~/.bobi.
- Evidence: server.py:167 `home = (home_root or paths.home_dir()).resolve()` with paths.py:66-68 `home_dir()` returning `Path.home() / ".bobi"` when BOBI_HOME is unset; server.py:170-182 `_within_home` confines to that tree; server.py:953 uses it for detect. Contradicting intent: server.py:336-338 comment 'Rooted at the user's home (the library and most dev repos live there); confined to it so the localhost page can't list the whole filesystem', and the 400 message 'pick a folder inside your home directory' (server.py:956), while mcp_detect.py's docstring says 'Point this at a folder on disk'.

## D081 — `bobi/setup/webui/server.py:1284` (bug, low, confirmed, phase 4)

**GET /api/credential/value falls back to os.environ for any requested var name, so the endpoint serves arbitrary process environment variables (AWS keys, ANTHROPIC_API_KEY, etc.) to the page, beyond the 'saved credential' purpose its comment states.**

- Detail: Any code running in the setup page's origin (or anything that obtains the per-launch nonce embedded in the served HTML) can GET /api/credential/value?var=AWS_SECRET_ACCESS_KEY and receive the plaintext value from the setup process's inherited environment — a secret that was never saved through setup and does not live in run/.env. The justifying comment ('the value already lives in plaintext in .env on this machine') only covers the read_env path, not the os.environ fallback, which widens exposure to every secret exported in the shell that launched `bobi setup`.
- Evidence: server.py:1276-1287 — `val = actions.read_env(project).get(var) or os.environ.get(var, "")` with no restriction to state.credentials_saved or to vars setup manages; contrast mcp_probe.py:30-35, which deliberately withholds ambient os.environ secrets from child processes for exactly this reason.

## D080 — `bobi/setup/webui/server.py:1373` (bug, low, confirmed, phase 4)

**GET /api/file calls target.read_text() with no decode-error handling, so any non-UTF-8 file in the pack (which /api/files happily lists) makes the review file viewer 500.**

- Detail: Open mode on a pack that contains a binary or non-UTF-8 file (e.g. a logo.png or latin-1 doc copied into the source tree): /api/files lists every file under the pack with no suffix filter (server.py:1342-1344), the review UI requests /api/file?path=logo.png, target.read_text() raises UnicodeDecodeError, and the request fails with an unhandled 500 instead of a clean error.
- Evidence: server.py:1368-1373 — `read_file` returns `target.read_text()` inside no try/except; server.py:1337-1345 `files()` lists all `p.is_file()` entries regardless of type, so binary files are directly reachable from the UI.

## D076 — `bobi/slack.py:170` (bug, low, confirmed, phase 4)

**format_slack_message blanket-replaces literal \n/\t escape sequences across the whole message, including inside code fences, corrupting quoted code/JSON/log content.**

- Detail: A monitor or supervisor notification includes a fenced code block quoting JSON or source, e.g. ```{"msg": "a\nb"}``` or `printf("\n")`: the replace at line 170 runs BEFORE the code-block-aware conversion passes, so the literal backslash-n inside the fence becomes a real newline, silently altering the quoted content the human is meant to read verbatim. The rest of the pipeline (_wrap_markdown_tables, _convert_markdown_outside_code_blocks) is careful to skip code blocks, but this replacement is not.
- Evidence: slack.py:167-174: `text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")` executes unconditionally on the full text before the code-block-aware _convert_markdown_outside_code_blocks (lines 90-102) runs.

## D074 — `bobi/slack_manifest.py:39` (bug, low, confirmed, phase 4)

**render_manifest substitutes the user-supplied app name unescaped into unquoted YAML scalar positions, so names containing YAML-special characters break or silently truncate the manifest.**

- Detail: App name 'Bobi: Staging' -> template renders `name: Bobi: Staging`, which yaml.safe_load rejects (mapping values not allowed here), crashing manifest_to_dict/manifest_to_json/create_app_url. App name 'Bobi #1' -> everything after '#' is parsed as a YAML comment, silently creating an app named 'Bobi'. A name like 'yes' or 'null' parses as a boolean/None instead of a string.
- Evidence: slack_manifest.py:38-42 uses string.Template(...).safe_substitute(APP_NAME=app_name) with no quoting/escaping; bobi/templates/slack-app.manifest.yaml lines 9 and 14 place ${APP_NAME} as bare unquoted scalars (`name: ${APP_NAME}`, `display_name: ${APP_NAME}`).

## D042 — `bobi/tool_library.py:168` (bug, medium, confirmed, phase 4)

**resolve_dependencies de-dupes by name with FIRST occurrence winning while the tool_library union appends leaf entries after base entries, so a leaf overlay can never override a base layer's dependency pin — inverting compose's leaf-wins rule.**

- Detail: Base team declares `tool_library: [codex]` (catalog pin @openai/codex@0.144.4). A leaf overlay needing a newer CLI declares inline `tool_library: [{name: codex, success: ..., install: {npm: ['@openai/codex@0.150.0']}}]`. compose.py:635 unions base-first (`out.get(key) + val`), _dedupe keeps both (string vs dict markers differ), then tool_library.py:168-170 skips the leaf's entry because 'codex' is already seen — the overlay's pin is silently dropped and the image bakes the base's version with no warning.
- Evidence: compose.py:10 ('The leaf always wins.') and compose.py:630-635 (base entries accumulate before the overlaying layer's); tool_library.py:168-171 `if dep.name in seen: continue` keeps the first (base) occurrence.

## D084 — `bobi/tool_library.py:225` (bug, low, confirmed, phase 4)

**The dependency-guide leaf-wins check (`if not guide_path.exists()`) mistakes a stale guide from a previous install for a team-shipped file, so reinstalling a team whose layers ship no tools/ dir never refreshes tools/<name>.md after a catalog guide update.**

- Detail: A team declares `tool_library: [venn]` and no layer ships a tools/ directory. install.py:52-58 clears only surfaces some layer contributes, so dest/tools/ survives reinstall; a framework upgrade that rewrites bobi/tool_library/venn/guide.md then reinstalls the team, but tool_library.py:225 sees the prior install's tools/venn.md and skips the write — the runtime agent keeps reading the outdated usage guide indefinitely. Removing the dep from the team similarly leaves the orphaned guide behind forever.
- Evidence: install.py:52-54 builds `contributed` only from `(layer.dir / sub).is_dir()` and rmtrees only those; compose() writes into the same reused dest (paths.package_dir), and tool_library.py:224-227 writes the guide only when the path does not already exist.

## D037 — `bobi/webapp/daemon.py:191` (bug, medium, confirmed, phase 4)

**daemon stop() sends SIGTERM/SIGKILL to the pid from a possibly-stale pidfile without confirming it is the bobi app, so pid reuse causes it to kill an unrelated process.**

- Detail: The app crashes without cleanup (run_foreground's finally block only runs on clean shutdown), leaving app.pid pointing at a now-dead pid. The OS later reuses that pid for an unrelated process. `bobi app stop` reads the stale pid, _pid_alive(pid) returns True (the reused process is alive), and stop() immediately os.kill(pid, SIGTERM) then SIGKILL (daemon.py:191,197) — killing the innocent process. status() (daemon.py:126-136) guards against exactly this by pinging /api/ping with the token before declaring the app running, but stop() performs no such liveness/identity check before signalling.
- Evidence: daemon.py:186-197 — pid=_read_int(_pid_path()); if not _pid_alive(pid): cleanup+return; os.kill(pid, SIGTERM); ... os.kill(pid, SIGKILL). Contrast daemon.py:130-134 where status() additionally requires _ping(port, token) to confirm identity; run_foreground's pidfile cleanup (daemon.py:234-237) only executes on graceful exit, so a crash leaves a stale pidfile.

## Q080 — `bobi/brain/__init__.py:105` (dead-code, low, confirmed, phase 5)

**The public BRAIN_MODEL_ENV compatibility alias has zero importers in the repo, tests, and the private deploy repo.**

- Detail: It exists only 'for older external code that imported the constant directly', but no such code exists in any repo of the family (bobi-agent, tests, bobi-deploy). It also isn't in __all__. If the concern is unknown PyPI consumers, note that in the removal PR; otherwise delete the alias and keep only the private _BRAIN_MODEL_ENV.
- Evidence: grep -rn 'BRAIN_MODEL_ENV' across bobi/, tests/, ~/dev/bobi-deploy: only the private _BRAIN_MODEL_ENV uses plus the alias definition at __init__.py:103-105.

## Q078 — `bobi/brain/claude.py:27` (dead-code, low, confirmed, phase 5)

**BrainCapabilities is imported but unused in both brain/claude.py and brain/codex.py — a #789 refactor leftover.**

- Detail: Before #789 the Gateway subclasses/factories declared `capabilities = BrainCapabilities(...)` in these files; that moved to the GatewayAwareEngine mixin's property in gateway.py, leaving the imports (claude.py:27, codex.py:36) dead. Drop both names from the import blocks.
- Evidence: grep -n BrainCapabilities bobi/brain/claude.py bobi/brain/codex.py hits only the import lines; the only constructor call in the package is gateway.py:101 (mixin property) and stub.py:158.

## Q077 — `bobi/brain/claude.py:125` (dead-code, low, confirmed, phase 5)

**The trailing `if last_error is not None: raise last_error` after the connect retry loop is unreachable.**

- Detail: _env_int clamps attempts to >= 1, so range(1, attempts+1) always runs; on the final attempt should_retry is False (attempt < attempts fails) and the except block re-raises, and success returns — the loop can never fall through. Delete the last_error variable and the trailing block; behavior identical.
- Evidence: claude.py:411-420 _env_int returns max(value, 1); connect() loop at 101-123: every iteration either returns, or raises when should_retry is False, and should_retry is always False on the last iteration.

## D061 — `bobi/cli.py:348` (duplication, low, confirmed, phase 5)

**_list_agent_packs in cli.py is a byte-for-byte duplicate of service.py's _list_agent_packs, and the cli copy is dead code.**

- Detail: Two identical implementations of the pack-discovery scan (cache/installed/local tiers) must be kept in sync; the cli copy has zero call sites (the start command consumes exc.available from service.NoAgentInstalled instead), so any future fix to the discovery rules lands in one copy and silently not the other. Consolidatable by deleting cli.py:348-360.
- Evidence: cli.py:348-360 vs bobi/service.py:256-268 — identical bodies. Repo-wide grep shows no caller of the cli copy; service.py:253 uses its own local copy.

## D062 — `bobi/cli.py:958` (duplication, low, confirmed, phase 5)

**Dead helpers _find_pid_path (line 948), _stop_manager_pid (line 958) and _run_from_config (line 364) duplicate the live stop/start paths in service.py and are never called.**

- Detail: _stop_manager_pid re-implements the exact SIGTERM/SIGKILL/30x0.2s-poll loop and the user-facing strings ('Invalid PID file — cleaning up.', "Process didn't exit — try: bobi agent <name> stop --force") that service.stop_team + the stop command (cli.py:1017-1034) own; a change to stop semantics/messaging must be made twice or drifts. All three helpers have zero call sites (verified by repo-wide grep), so they can be deleted outright.
- Evidence: cli.py:948-997 duplicates service.py:655-695 (stop_team) and the stop command's echo strings at cli.py:1019/1032; grep across bobi/, tests/ finds no reference to _find_pid_path, _stop_manager_pid, or _run_from_config.

## Q008 — `bobi/cli.py:1594` (dead-code, medium, confirmed, phase 5)

**Six unreachable `if not project_path:` guards / else-branches follow `_detect_project_root()` (or `paths.home_dir()`), which can never return a falsy value — it returns a Path or raises click.UsageError.**

- Detail: paths.resolve_root/bound_root guarantee a Path or a raise, so these branches can never execute: status (1594-1596, the 'No Bobi Agent runtime selected' echo+exit), monitor_add (2418-2419), monitor_pause (2483-2487 else-branch and the `if project_path else "package/monitors.yaml"` at 2489), monitor_remove (2510-2514 else-branch), event_server_stop (2701-2703), _find_transcript (1483, the `else "bobi-manager"` arm), and agents_browse (3093, `if project_path else []` after `paths.home_dir()`). Removing them flattens monitor_pause/monitor_remove to a single `with with_mutable_runtime_package(...)` call and deletes misleading error paths that suggest a None contract that doesn't exist. Behavior is preserved because the conditions are provably always true/false.
- Evidence: Read paths.resolve_root (bobi/paths.py:122-145): every path returns a valid Path or raises RuntimeError, which _detect_project_root converts to click.UsageError; paths.home_dir always returns a Path. The distinct `workflow_list` case correctly uses `_try_detect_project_root()` which does return None — showing the codebase already has a separate helper for the nullable case.

## Q032 — `bobi/config.py:317` (dead-code, medium, confirmed, phase 5)

**Config fields `registries`, `default_role`, and `chat` are parsed from agent.yaml but never read by any production code.**

- Detail: `cfg.registries` is shadowed by bobi/registry.py:_all_registries, which reads registries straight from the global config.yaml itself; `cfg.default_role` (including the `defaults:` block special-casing at _parse line 510) has zero readers anywhere including tests; `cfg.chat` is read only by parse-assertion tests, while the actual consumer of the yaml `chat:` key (setup/open_mode.py:250) reads the raw yaml dict, not Config. Better shape: drop the three fields and their _parse lines (and the two chat parse asserts in tests); dataclass consumers are unaffected because nothing reads them.
- Evidence: grep -rn '\.registries' and '\.default_role' across bobi/ and tests/ return zero readers; registry.py:71-84 re-reads registries from yaml directly; '.chat' readers are only tests/test_config.py:120 and tests/integration/test_agent_yaml_config.py:43 asserting the parse, plus setup code operating on SetupState.chat / raw dicts.

## Q125 — `bobi/config.py:409` (dead-code, low, unverified, phase 5)

**Config.brain_small_model property has zero callers — the one real consumer bypasses it and reads the raw brain dict.**

- Detail: Either delete the property or point env.py:65 at it (one caller, either direction works). As-is it is an accessor added for symmetry with brain_kind/brain_model/brain_effort (47 external uses between them) that never got a consumer, so it silently drifts from the raw-dict read that actually feeds gateway small-model selection.
- Evidence: grep for `brain_small_model` across bobi/, tests/, and bobi-deploy hits only the definition (config.py:408-411); bobi/env.py:65 reads `brain.get("small_model", "")` directly from the dict instead.

## Q038 — `bobi/config.py:510` (dead-code, medium, confirmed, phase 5)

**Config.default_role is parsed from agent.yaml's `defaults.role` but never read by anything, so the key is silently ignored.**

- Detail: Role defaulting actually flows through the Config.entry_role property (entry_point or 'manager', config.py:361-368), which the docstring calls 'the one place the default lives'. The default_role field (line 321) and its parse expression (line 510) are vestigial: a pack author setting `defaults: {role: x}` gets no effect and no warning. Delete the field and the parse line; no behavior changes because no reader exists.
- Evidence: grep -rn default_role across bobi/, tests/, agents/, scripts/, docs/, skills/ and ~/dev/bobi-deploy: only the dataclass field (config.py:321) and the constructor arg (config.py:510). entry_role property read at config.py:361-368 confirms the real default mechanism.

## Q045 — `bobi/doctor.py:517` (dead-code, low, confirmed, phase 5)

**`_check_policy` is a self-described 'deprecated compatibility wrapper for one release' with zero production callers, several releases after the sleep-cycle rename shipped.**

- Detail: The policy→long_term_memory rename (#698) shipped around 0.42 and the repo is now on 0.46; run_doctor calls `_check_long_term_memory` directly and nothing in bobi/ references `_check_policy`. Only tests/test_long_term_memory.py and tests/test_doctor.py import it — and they are testing `_check_long_term_memory` behavior through the stale alias. Delete the wrapper and update the two test files to import `_check_long_term_memory`; identical behavior since the wrapper is a bare delegation.
- Evidence: grep across the whole repo including tests: bobi/doctor.py:517 is the definition, and the only importers are tests/test_long_term_memory.py (5 sites) and tests/test_doctor.py (2 sites); no production code path reaches it.

## Q053 — `bobi/history.py:15` (dead-code, low, confirmed, phase 5)

**SESSIONS_DIR module constant is never referenced.**

- Detail: Defined next to PROJECTS_DIR but no code in the module or repo uses it; delete the line.
- Evidence: grep -rn SESSIONS_DIR across repo: only its definition at history.py:15.

## Q013 — `bobi/history.py:447` (dead-code, medium, confirmed, phase 5)

**context_for_events (49 lines) has zero production callers — only tests exercise it.**

- Detail: Repo-wide grep (all file types, including cli.py, scheduler, doctor, webapp) finds no caller outside tests/test_history.py. It looks like a leftover from an older manager context-injection design; the live history consumers are index()/search()/messages_since() (scheduler.py:1131,1135; doctor.py:589; cli.py transcript commands). Deleting it (and its test class) removes a whole query-fanout/formatting feature nobody ships.
- Evidence: grep -rn context_for_events across repo excluding .venv/worktrees: matches only bobi/history.py:447 (def) and tests/test_history.py.

## Q048 — `bobi/history.py:498` (dead-code, low, confirmed, phase 5)

**start_background_indexer has zero callers anywhere in the repo, including tests.**

- Detail: The background re-index loop is never started by any code path; production indexing happens via the monitor scheduler calling history.index() directly (scheduler.py:1131). 20 lines of thread machinery can be deleted outright with no behavior change.
- Evidence: grep -rn start_background_indexer across the whole repo (all extensions, excluding .venv/worktrees): the only match is its own definition.

## Q052 — `bobi/inbox.py:102` (dead-code, low, confirmed, phase 5)

**Inbox._closed is written in __init__ and close() but never read anywhere.**

- Detail: Redundant state: nothing checks _closed (push/recv happily operate after close; the real close effect is unregister_local_inbox). Deleting both assignments removes a flag that implies a guard that does not exist, which is actively misleading when reasoning about the post-close drain race the Session.stop() comments discuss.
- Evidence: grep -rn '_closed' across bobi/ and tests/: only inbox.py:102 and inbox.py:150 (both writes); no reads.

## Q094 — `bobi/kb/store.py:174` (dead-code, low, confirmed, phase 5)

**KBStore.kb_dir() and KBStore.db_path_for() static methods have zero callers in the repo, including tests.**

- Detail: Every consumer either passes db_path explicitly (memory.py cold-memory path) or relies on the module-private _kb_dir default inside __init__/create/remove/list_kbs; the two static wrappers are unused public surface. Delete them - the internal _kb_dir remains for the default path logic.
- Evidence: grep -rn 'kb_dir|db_path_for' across bobi/ and tests/: only the defs at store.py:175/179, the module-level _kb_dir uses, and tests monkeypatching bobi.kb.store._kb_dir directly (tests/test_kb_store.py:42).

## D083 — `bobi/mcp/__init__.py:1` (doc-drift, low, confirmed, phase 5)

**bobi/mcp is an empty leftover package whose docstring claims 'Built-in MCP servers shipped with bobi' while it contains no servers and nothing imports it.**

- Detail: Doc claims built-in MCP servers live here; reality: the package holds only the one-line __init__.py — the former servers (codex_server, image_server, inject) survive only as stale entries in bobi/mcp/__pycache__ — and no module in bobi/ or tests/ imports bobi.mcp. A developer looking for the runtime MCP surface is pointed at a dead package instead of bobi/mcp_handshake.py and the per-brain rendering in bobi/brain/codex_config.py.
- Evidence: bobi/mcp/ contains only __init__.py (46 bytes, docstring 'Built-in MCP servers shipped with bobi.'); grep across bobi/ and tests/ finds no `from bobi.mcp` / `import bobi.mcp` consumers; __pycache__ lists codex_server/image_server/inject .pyc for source files that no longer exist.

## Q121 — `bobi/memory.py:169` (dead-code, low, unverified, phase 5)

**reference_memory_path() has zero callers; the one place that needs the reference.md path (monitors/scheduler.py:1126) hand-builds it inline instead.**

- Detail: Either delete the helper, or (better) have scheduler.py:1126 call it so the path has one owner — the current state is the worst of both: a dead public helper plus an inline duplicate of the same expression `paths.workspace_dir(root) / "memory" / "reference.md"`. Behavior identical either way.
- Evidence: grep -rw reference_memory_path across bobi/, tests/, agents/, scripts/ and ~/dev/bobi-deploy: only the definition at bobi/memory.py:169. grep -rn 'reference.md' shows scheduler.py:1126 constructing the same path inline.

## Q030 — `bobi/memory.py:381` (dead-code, medium, confirmed, phase 5)

**The policy->long_term_memory deprecation aliases (memory.MAX_POLICY_CHARS/load_policy/format_policy_prompt, paths.policy_path/policy_cursor_path, subagent._load_policy_prompt, prompts/resolver._load_policy_section) are past their stated one-release window and have zero production callers.**

- Detail: All are documented 'Deprecated alias(es) kept for one release'. The rename shipped in commit 6fea983 on 2026-07-08 and releases 0.43-0.46 have shipped since, so the window has elapsed several times over. The private aliases subagent._load_policy_prompt and resolver._load_policy_section are unreachable even in principle (private, zero callers). Better shape: delete all seven symbols and switch tests/test_sleep_cycle.py and tests/test_long_term_memory.py from paths.policy_path/policy_cursor_path to the long_term_memory_* names - behavior is identical since every alias is a pure passthrough.
- Evidence: grep -rn across repo incl. tests: memory.load_policy/format_policy_prompt/MAX_POLICY_CHARS have zero callers; subagent.py:658 _load_policy_prompt and resolver.py:194 _load_policy_section appear only at their def sites; paths.policy_path/policy_cursor_path are called only from tests (test_sleep_cycle.py, test_long_term_memory.py). git log shows the rename landed 2026-07-08 (6fea983); repo is now at 0.46.0.

## Q014 — `bobi/monitors/scheduler.py:567` (dead-code, medium, confirmed, phase 5)

**The rest of the curator→sleep-cycle compat layer is five releases past its stated one-release window: _default_spawn_curator (567), the spawn_curator ctor param + self.spawn_curator alias (577/584/588), _spawn_curator (1211), the curator.md prompt fallback (1084), the system/policy.updated legacy publish leg (1439), and the bobi/monitors/curator.py shim module.**

- Detail: Each wrapper is documented 'Deprecated compatibility wrapper for one release'; the rename shipped in commit 6fea983, first released in v0.41.0 (2026-07-08), and the repo is now on 0.46.0 — five releases later. The only live references are in-repo: tests/test_sleep_cycle.py calls the old names (_spawn_curator, spawn_curator=, _default_spawn_curator) and bobi/subagent.py:2021 imports the curator.py shim for parse_result — both are one-line renames to the sleep_cycle names. Removing the policy.updated leg also collapses _publish_memory_updated's convoluted 3-candidate loop with its published_events dedup set and 'published = ok only when candidate == event' bookkeeping (1437-1450) into a direct publish (plus the system/memory.updated alias when a custom monitor.event is set), and lets drain.py's _is_policy_update drop its policy.updated matching. Behavior is preserved for everything current: nothing in the runtime publishes or renders curator names anymore.
- Evidence: git tag --contains 6fea983 → v0.41.0+; VERSION is 0.46.x. grep shows old-name callers only in tests/test_sleep_cycle.py and subagent.py:2021 (curator_mod.parse_result, re-exported unchanged from sleep_cycle). drain.py:57-58 matches policy.updated only because scheduler.py:1439 still emits it.

## Q055 — `bobi/monitors/scheduler.py:1097` (dead-code, low, confirmed, phase 5)

**Three deprecated curator wrappers — _load_curator_prompt (1097), _on_curator_result (1414), _publish_policy_updated (1452) — have zero callers anywhere in the repo, including tests.**

- Detail: These 'deprecated compatibility wrapper for one release' methods are pure indirection that nothing exercises: no production code, no test, no CLI path references them (only their definitions appear in grep). They can be deleted outright with zero behavior change. Note _on_curator_result would even behave differently from the real path if someone did call it — it forwards without memory_path/compaction_required/reference args — so it is not merely dead but a latent trap.
- Evidence: grep -rn '_on_curator_result\|_load_curator_prompt\|_publish_policy_updated' --include='*.py' across the whole repo returns only the three definition sites in scheduler.py; test_sleep_cycle.py exercises other curator aliases but never these three.

## Q057 — `bobi/monitors/script_cache_checks.py:1046` (dead-code, low, confirmed, phase 5)

**approve_pending's scripts_dir parameter is never read — the body resolves all paths via _pending_path/_pin/_scripts_dir — yet tests pass a directory to it, which is silently ignored.**

- Detail: The parameter is pure vestigial surface: the function ignores it and derives every path from the module-level helpers, so dropping it changes nothing at runtime. Worse than harmless, it misleads: tests/test_script_cache_runner.py:312 and :331 call approve_pending(m, scripts_dir) apparently expecting to point the function at a directory, but the arg does nothing (the tests only work because paths are redirected via the environment). Remove the parameter and fix the two test call sites.
- Evidence: Read of approve_pending body (script_cache_checks.py:1046-1076): scripts_dir never referenced; grep shows the only callers are cli.py:2607 (one arg) and the two test sites passing an ignored second arg.

## Q095 — `bobi/paths.py:91` (dead-code, low, confirmed, phase 5)

**paths.agent_runtime_root is a pure alias of agent_run_root with zero callers.**

- Detail: It returns agent_run_root(name) verbatim and nothing in bobi/ or tests/ references it; every caller uses agent_run_root or resolve_root_for_agent. Delete the alias.
- Evidence: grep -rn agent_runtime_root across the repo (incl. tests): only the definition at paths.py:91.

## Q118 — `bobi/paths.py:183` (dead-code, low, unverified, phase 5)

**compose_lock_path() has zero callers; every compose-lock consumer builds `<dest>/compose-lock.json` inline against its own base dir.**

- Detail: install.read_compose_lock (install.py:92) and write_compose_lock (install.py:111) and the manifest entry list (install.py:176) all use the literal filename against a caller-supplied dest, which the paths helper (anchored to package_dir) cannot express. The helper is unreachable; delete it, or if a canonical constant is wanted, keep only a COMPOSE_LOCK_NAME string.
- Evidence: grep -rw compose_lock_path across bobi/, tests/, agents/, scripts/ and ~/dev/bobi-deploy: only the definition. grep -rn 'compose-lock' shows all real consumers in bobi/install.py:92,111,176 and tests/test_compose.py building the path inline.

## Q119 — `bobi/paths.py:282` (dead-code, low, unverified, phase 5)

**worktrees_dir() points at state_dir()/worktrees, a location nothing in the codebase uses — workflow worktrees actually live under the repo's .claude/worktrees.**

- Detail: The only worktree-path construction in the runtime is bobi/workflow/orchestrator.py:131 (`repo_root / ".claude" / "worktrees" / session_name`). The paths helper describes a directory that is never created or read, and keeping it invites someone to 'fix' code to use the wrong location. Delete it.
- Evidence: grep -rw worktrees_dir across bobi/, tests/, agents/, scripts/ and ~/dev/bobi-deploy: only the definition at bobi/paths.py:282. grep -rn '"worktrees"' in bobi/: only orchestrator.py:131 and the dead helper.

## Q102 — `bobi/prompts/resolver.py:194` (dead-code, low, confirmed, phase 5)

**_load_policy_section() is a private 'deprecated alias for one release' with zero callers — a private name cannot serve external compat, so it was dead on arrival.**

- Detail: It only forwards to _load_long_term_memory_section. Being underscore-private, no installed package could legitimately import it, and nothing in-repo does. Delete the three lines; behavior unchanged.
- Evidence: grep -rw _load_policy_section across bobi/, tests/, agents/, scripts/ and ~/dev/bobi-deploy: only the definition at bobi/prompts/resolver.py:194.

## Q081 — `bobi/sdk.py:42` (dead-code, low, confirmed, phase 5)

**TERMINAL_STATUSES tuple is defined but never read anywhere.**

- Detail: ACTIVE_STATUSES, FAILED_STATUSES, and DEAD_STATUSES all have consumers (sdk.py itself, reconcile, inbox.py:322), but TERMINAL_STATUSES has none — code that needs terminal-ness checks against ACTIVE_STATUSES or DEAD_STATUSES instead. Delete it (or start using it where DEAD_STATUSES is abused for terminal checks — but as-is it is unread vocabulary).
- Evidence: grep -rn TERMINAL_STATUSES across bobi/, tests/, ~/dev/bobi-deploy: only the definition at sdk.py:42.

## Q079 — `bobi/sdk.py:71` (dead-code, low, confirmed, phase 5)

**Module constant CLAUDE_CLI has zero readers anywhere and forces a shutil.which() at import time.**

- Detail: Comment says 'Resolved once at import for back-compat', but no code in bobi/, tests/, or the private bobi-deploy checkout reads it — everything uses get_cli_path(). Deleting the constant removes a filesystem probe from sdk.py's import path with no behavior change.
- Evidence: grep -rn 'CLAUDE_CLI' across bobi/, tests/, and ~/dev/bobi-deploy: only the definition at sdk.py:71; no sdk.CLAUDE_CLI attribute access either.

## Q113 — `bobi/sdk.py:198` (dead-code, low, plausible, phase 5)

**sdk.state_dir() wrapper (delegating to paths.state_dir) has zero callers.**

- Detail: Every consumer of the state dir calls bobi.paths.state_dir directly or passes a state_dir Path around (memory.py, service.py). The sdk-level delegate is never imported or attribute-accessed, including in tests and bobi-deploy. Delete it; behavior identical.
- Evidence: grep for 'from bobi.sdk import ... state_dir', 'sdk.state_dir', and bare 'state_dir(' callers across bobi/, tests/, ~/dev/bobi-deploy: only paths.state_dir uses and local state_dir parameters; the sdk wrapper at 198-200 is unreferenced.

## Q082 — `bobi/sdk.py:456` (dead-code, low, confirmed, phase 5)

**SessionRegistry.log_path's only caller is its own unit test.**

- Detail: Production code always calls the module-level session_log_path() (subagent.py:1067, cli.py:1486, sdk.py:586); the registry method is exercised solely by tests/test_registry.py:108, which tests the method itself. Delete the method (the sibling handoff_path has real integration-test consumers and can stay).
- Evidence: grep -rn '\.log_path(' across bobi/, tests/: only tests/test_registry.py:108. All runtime callers use session_log_path directly.

## Q031 — `bobi/service.py:709` (dead-code, medium, confirmed, phase 5)

**service.restart_team has zero callers anywhere in the repo, including tests.**

- Detail: Both real restart paths bypass it: the CLI restart command (cli.py:1045) uses ctx.invoke(stop) + ctx.invoke(start), and the webapp LocalRuntime.restart_team (webapp/runtime.py:412) deliberately implements stop_team + its own start_team so preflight failures carry the structured report. Deleting the function removes an untested third restart path that can silently drift from the two live ones.
- Evidence: grep -rn restart_team across bobi/ and tests/: only the def at service.py:709, its internal start_team call at :716, and the unrelated webapp TeamRuntime.restart_team methods (webapp/runtime.py:105,412) called from webapp/server.py:189. No test imports service.restart_team.

## Q054 — `bobi/session.py:17` (dead-code, low, confirmed, phase 5)

**session.py imports hashlib (line 17) and Path (line 22) but uses neither.**

- Detail: Both imports are relics (session-id hashing and path handling moved elsewhere); grep of session.py shows no other occurrence of 'hashlib' or 'Path'. Delete the two import lines.
- Evidence: grep -n 'Path\|hashlib' bobi/session.py returns only the two import lines.

## Q037 — `bobi/setup/__init__.py:19` (dead-code, medium, confirmed, phase 5)

**run_setup() has zero callers; the `bobi setup` CLI command now opens the webapp daemon URL instead, leaving the setup package's only public entry point dead.**

- Detail: cli.py's setup command (bobi/cli.py:791) starts bobi.webapp.daemon and opens #/setup in a browser; the setup web UI is mounted inside the webapp via build_setup_app (bobi/webapp/server.py:206). run_setup's serve() path (bobi.setup.webui.server.serve) is a parallel launch path nobody invokes. Delete run_setup (and serve() if it also has no callers) so the one real launch path is unambiguous.
- Evidence: grep -rw run_setup across bobi/, tests/, agents/, scripts/, docs/, skills/, event-server/ and the private ~/dev/bobi-deploy checkout: only the definition at bobi/setup/__init__.py:19. Read bobi/cli.py:786-814 confirming `bobi setup` routes through webapp daemon.

## Q120 — `bobi/setup/actions.py:108` (dead-code, low, unverified, phase 5)

**default_secret_prompt() — a click-based masked terminal prompt — has zero callers since setup became web-UI-driven.**

- Detail: Setup runs through the local web UI (setup/__init__.py docstring; webui/server.py); secrets are collected in the browser, not via click.prompt. No code references this terminal prompt helper. Delete it; behavior unchanged.
- Evidence: grep -rn 'default_secret_prompt\|secret_prompt' across bobi/, tests/, agents/: only the definition at bobi/setup/actions.py:108.

## Q065 — `bobi/setup/actions.py:161` (dead-code, low, confirmed, phase 5)

**installed_team_name has no production callers — only its own unit tests reference it.**

- Detail: Leftover from the removed tools.py wrapper layer the module docstring still references. Delete the function and its dedicated tests; nothing else reads run/package's team name through this path, so behavior is unaffected.
- Evidence: grep across bobi/ and tests/ (including cli.py, webui, webapp): definition at actions.py:161 plus tests/test_setup_actions.py:138-149 only. No runtime caller.

## Q066 — `bobi/setup/actions.py:173` (dead-code, low, confirmed, phase 5)

**resolve_or_fetch has no production callers — only its own unit tests reference it.**

- Detail: Same removed-caller-layer leftover as installed_team_name; the live template-fetch path is open_mode.fetch_into. Delete the function and its tests — no behavior change.
- Evidence: grep across bobi/ and tests/: definition at actions.py:173 plus tests/test_setup_actions.py:152-165 only. The setup web flow fetches templates via open_mode.fetch_into / registry.fetch instead (server.py:503, open_mode.py:163).

## Q067 — `bobi/setup/authoring.py:614` (dead-code, low, confirmed, phase 5)

**_env_var_fallback is an unreachable fallback: the `conn.credential_var or _env_var_fallback(...)` branch in tools_prompt can never fire.**

- Detail: The function is a dead safety net that also happens to duplicate services._env_var_for line-for-line. Drop `or _env_var_fallback(conn.name)` from tools_prompt and delete the function (plus its inner `import re`); behavior is provably identical because the fallback branch is unreachable.
- Evidence: tools_prompt is only called from compute_manifest (authoring.py:735) with connectors from custom_services(), which returns only kind=='custom' connectors; those are built exclusively by services._custom_connector (services.py:347), which always attaches one required Secret whose var comes from services._env_var_for — never empty (falls back to 'SERVICE_API_KEY'). So credential_var is always truthy. No other caller of tools_prompt or _env_var_fallback exists (grep over bobi/ and tests/).

## Q068 — `bobi/setup/llm.py:23` (dead-code, low, confirmed, phase 5)

**The _delta_text re-export from bobi.brain.claude exists only so a test can import it from the old location; no production code uses llm._delta_text.**

- Detail: A compat shim whose sole consumer is the test that pins it is indirection earning nothing — those assertions are really pinning brain-adapter behavior. Point the test at bobi.brain.claude._delta_text (or move the assertions into the brain tests) and delete the re-export; behavior is unchanged since nothing at runtime imports it from llm.
- Evidence: grep for _delta_text: the real definition and its one production caller are both in bobi/brain/claude.py (lines 59, 544); the only consumer of the llm.py re-export is tests/test_setup_llm.py:102-108. The comment itself says it is 'kept for back-compat (tested as llm._delta_text)'.

## Q049 — `bobi/subagent.py:658` (dead-code, low, confirmed, phase 5)

**_load_policy_prompt, the 'deprecated alias for one release' from the policy→long_term_memory rename, has zero callers and has outlived several releases.**

- Detail: The rename shipped around 0.42 (#698) and the repo is now on 0.46; grep finds no caller in bobi/, tests/, or any other tree. The alias can be deleted.
- Evidence: grep -rn _load_policy_prompt across repo: only bobi/subagent.py:658 (its own def).

## Q050 — `bobi/subagent.py:1687` (dead-code, low, confirmed, phase 5)

**_parse_check_output is a back-compat shim with zero production callers, while run_check_blocking re-inlines its exact verdict→(finding, summary, details) logic.**

- Detail: Only tests/test_subagent_blocking.py calls the shim; the one production path that needs the conversion (run_check_blocking:1759-1763) duplicates the body instead of calling it. Either delete the shim and its dedicated tests (the logic is covered via run_check_blocking) or keep it and have run_check_blocking build its CheckResult through it — both remove one of the two copies of the same 8-line extraction.
- Evidence: grep: _parse_check_output referenced only in tests/test_subagent_blocking.py; subagent.py:1759-1763 repeats bool(verdict.get('finding')) / summary-if-finding / details-isinstance-dict guard verbatim in spirit.

## Q086 — `bobi/validate.py:77` (dead-code, low, confirmed, phase 5)

**ValidationResult.errors property has zero callers anywhere.**

- Detail: The property (lines 77-80) filters failed checks but nothing reads it: every consumer of validate_config (service.py, doctor.py, setup/actions.py, cli.py, all tests, and the private bobi-deploy repo) uses only .ok, .checks, and .format(). Delete the property; behavior is trivially preserved.
- Evidence: grep for `\.errors\b` across /Users/zkozick/dev/bobi-agent (bobi/, tests/, webapp/) and /Users/zkozick/dev/bobi-deploy returns no matches; the only other ValidationResult class (bobi/monitors/script_cache_checks.py) is unrelated and has no errors property.

## Q059 — `bobi/workflow/orchestrator.py:592` (dead-code, low, confirmed, phase 5)

**failed_step is assigned at 7 sites in _run_workflow_async and never read; the Any and AgentResult imports are also unused.**

- Detail: failed_step = step.name is dutifully set on every failure path (lines 592, 628, 650, 692, 816, 841, 866) but nothing ever reads it — the finally block and callers use only run_failed/failure_error. It is pure ceremonial state that makes each failure epilogue look load-bearing. Deleting all 7 assignments changes nothing. Same for `from typing import Any` (line 22; the only other 'Any' in the file is inside a comment at line 554) and `AgentResult` in the bobi.subagent import block (line 32; appears nowhere else in the file). Behavior trivially preserved: none are referenced.
- Evidence: grep -n 'failed_step\|AgentResult\|Any' bobi/workflow/orchestrator.py shows only assignments/imports, zero reads; repo-wide grep shows no external reference to these names in this module.

## Q012 — `bobi/auth_bootstrap.py:192` (missed-reuse, medium, confirmed, phase 6)

**auth_bootstrap._parse_conversation hand-rolls the conversation-ref grammar that bobi.conversation.parse_conversation already implements.**

- Detail: The private parser duplicates every rule of the shared grammar (len(parts) in (4,6), no empty segments, parts[4]=='thread', chat_type in {dm,group,channel}) and returns the first four fields as a tuple. Replacing the body with `conv = parse_conversation(ref); return None if conv is None else (conv.source, conv.scope, conv.chat_type, conv.chat_id)` — or using the Conversation object directly at the one call site (line 220) — keeps behavior identical while leaving exactly one Python implementation of the grammar that conversation.py documents as mirroring event-server/core/src/conversation.ts. Today a grammar change must be made in three places (TS, conversation.py, auth_bootstrap.py) instead of two.
- Evidence: Read of auth_bootstrap.py 192-205 vs conversation.py 43-59: identical validation logic, CHAT_TYPES set literal re-inlined. grep shows auth_bootstrap does not import bobi.conversation.

## Q083 — `bobi/brain/__init__.py:231` (structure, low, confirmed, phase 6)

**GATEWAY_UNRESOLVED_BASE_URL lives in brain/__init__.py, forcing gateway.py's require_gateway_base_url into a function-level circular-import workaround.**

- Detail: gateway.py owns the base-url pin (GATEWAY_BASE_URL_ENV, gateway_base_url()) but must lazily 'from bobi.brain import GATEWAY_UNRESOLVED_BASE_URL' inside require_gateway_base_url (gateway.py:69) because __init__.py imports gateway at module load. Moving the sentinel constant into gateway.py (and re-exporting it from __init__ for the existing __all__ entry) keeps all base-url semantics in one module and deletes the lazy import; pure code motion, behavior preserved.
- Evidence: brain/__init__.py:34 imports gateway at top; gateway.py:69 does the deferred import with the comment-implied reason; the constant is consumed by __init__.py set_process_brain and gateway.py only.

## Q026 — `bobi/brain/base.py:148` (structure, medium, confirmed, phase 6)

**stream_once is part of the de facto BrainFactory contract but is missing from the Protocol and unimplemented by CodexBrain, so the brain interface is dishonest.**

- Detail: bobi/setup/llm.py:70 calls get_brain().stream_once(...) unconditionally (the setup/digestion pour path, used by setup/automate.py, digestion.py, authoring.py). ClaudeBrain and StubBrain implement it (StubBrain implements it precisely so tests can drive this path), but BrainFactory in base.py declares only make_session, and CodexBrain has no stream_once at all — a process with BOBI_BRAIN=codex hitting this path dies with AttributeError laundered into LLMError. The honest shape: declare stream_once on the BrainFactory protocol and give CodexBrain a real implementation (a one-shot codex exec turn, which _CodexSession already knows how to run) or an explicit NotImplementedError naming the gap. Declaring it on the protocol is behavior-preserving for the working brains and turns a silent contract hole into a checked one.
- Evidence: grep 'stream_once' across repo: implementations only in brain/claude.py:487 and brain/stub.py:178; sole production caller bobi/setup/llm.py:70 via get_brain(); no stream_once anywhere in brain/codex.py; BrainFactory protocol (base.py:148-170) declares only make_session.

## Q027 — `bobi/brain/claude.py:328` (missed-reuse, medium, confirmed, phase 6)

**_claude_transcript_path re-implements the Claude transcript locator that already exists in bobi/chat_history.py (_claude_projects_dirs + _transcript_path).**

- Detail: Both walk CLAUDE_CONFIG_DIR/projects then ~/.claude/projects (deduped), iterate project dirs, and return the first <session_id>.jsonl hit. claude.py's copy (added later, for the #770/#772 max-turns fallback) folds the dedupe into the loop and adds OSError guards, but the semantics are identical. A change to Claude's transcript layout or CLAUDE_CONFIG_DIR handling now needs two coordinated edits. Better shape: promote one shared locator (e.g. a public find_claude_transcript(session_id) in chat_history.py, or a small shared module) and have _max_turns_from_transcript call it; behavior is preserved because the search order and match rule are the same.
- Evidence: bobi/chat_history.py:83-112 (_claude_projects_dirs/_transcript_path) vs bobi/brain/claude.py:328-353 (_claude_transcript_path): same dirs list, same dedupe-by-str, same project_dir iteration, same f"{session_id}.jsonl" candidate check. Not verbatim text (restructured + try/except OSError), so it evaded the copy-paste pass.

## Q087 — `bobi/build.py:70` (simplification, low, confirmed, phase 6)

**_flatten_if_chained's re-read/setdefault/rewrite of the composed agent.yaml (lines 70-74) is redundant — compose() already sets the agent name to the leaf directory's name.**

- Detail: compose._compose_agent_yaml does merged.setdefault("agent", chain[-1].dir.name) where chain[-1] is the leaf layer built from the same team_dir passed here, and compose() writes that agent.yaml to `staged`. So build.py's cfg.setdefault("agent", team_dir.name) can never insert anything and the write_text is a byte-identical yaml round-trip. Delete lines 70-74 (keeping the rename-to-leaf-name below); the 'preserve the leaf's directory name' comment is misleading since the naming is already guaranteed by compose.
- Evidence: bobi/compose.py:611 `merged.setdefault("agent", chain[-1].dir.name)` inside _compose_agent_yaml, called from compose() which writes dest/agent.yaml (line 343-344); chain[-1] is the leaf = the same directory build.py names team_dir, so team_dir.name == chain[-1].dir.name unconditionally.

## D050/Q007 — `bobi/cli.py:95` (duplication, medium, confirmed, phase 6)

**The entire local event-server port-resolution trio (_parse_local_event_server_port, _event_server_port_file, _selected_local_event_server_port, ~50 lines) is copy-pasted between cli.py and service.py, with a third verbatim copy of the URL-port parser in subagent.py.**

- Detail: Any change to the port-resolution chain (pid/port file precedence, default 8080, https default 443) made in one copy but not the others makes `bobi` CLI commands compute a different event-server port than the manager (service.py) or the child registration path (subagent.py) actually uses — e.g. CLI reports/stops a server on one port while the manager launched it on another. Consolidatable: all three bodies are line-for-line identical today, so a single shared helper (e.g. in bobi/events or bobi/sdk) can replace them.
- Evidence: bobi/service.py:813-861 duplicates bobi/cli.py:95-148 verbatim (same pid_file/port_file precedence, same Config.load fallback, same `return 8080`); bobi/subagent.py:1284-1291 `_local_port` duplicates the parser body (urlparse, scheme in http/https, hostname in ('localhost','127.0.0.1','::1'), `parsed.port or (443 if scheme=='https' else 80)`). Both copies are live: cli.py:139/2672/2745 and service.py:703/852.
- Cross-pass (Q007): cli.py re-solves the local event-server port resolution (`_parse_local_event_server_port`, `_event_server_port_file`, `_selected_local_event_server_port`, ~54 lines) that already exists in bobi/service.py:813-861 with identical logic.

## Q009 — `bobi/cli.py:151` (simplification, medium, confirmed, phase 6)

**`_ensure_root_bound()` is behaviorally identical to `_detect_project_root()` — its body re-implements the first two lines of the latter and then delegates to it — yet 14 call sites split between the two names.**

- Detail: `_detect_project_root()` (line 72) already starts with `bound = paths.bound_root(); if bound is not None: return bound`, so `_ensure_root_bound()`'s `root = paths.bound_root(); return root if root is not None else _detect_project_root()` adds nothing: same return value, same UsageError, same bind side effect in every case. Two names for one behavior makes readers hunt for a nonexistent difference (e.g. `stop` uses `_ensure_root_bound` while `start` uses `_detect_project_root`). Delete `_ensure_root_bound` and point its 14 callers (transcript group, subagents_*, kb_*, recall_memory, stop) at `_detect_project_root`. Behavior preserved by construction.
- Evidence: Read both function bodies (cli.py:72-87 and 151-155); grep shows _ensure_root_bound has 14 call sites, all inside cli.py, and no external importers.

## Q046 — `bobi/cli.py:925` (over-abstraction, low, confirmed, phase 6)

**Three single-caller pass-through wrappers around bobi.service/bobi.events functions add indirection the file's own house pattern avoids: `_manager_session_name` (925), `_clear_manager_session` (935), and `_post_event` (2960).**

- Detail: The house pattern in cli.py is a lazy `from bobi.service import X` inside the command body (start, stop, status, message all do this), and `events_publish` (1985) already calls `bobi.events.publish.post_event` directly. `_manager_session_name` has one caller (_find_transcript:1483) that never passes its `role` parameter, and its docstring claim that 'start, --fresh, and transcript lookup all resolve the same name through here' is stale — start and --fresh now go through bobi.service directly. `_post_event` has one caller (_run_check:2953). Inlining the imports at the call sites deletes ~25 lines and a stale docstring while preserving behavior exactly (each wrapper is a bare delegation; `_clear_manager_session`'s extra click.echo moves to its single production call site in `restart`).
- Evidence: grep shows _manager_session_name called once (1483, role never passed), _clear_manager_session called once in production (1057; tests/test_bubble.py:118 imports it), _post_event called once (2953) while events_publish bypasses it and calls events.publish.post_event directly.

## Q126 — `bobi/cli.py:1155` (over-abstraction, low, unverified, phase 6)

**The hidden `ask` command's `--source` option is exercised by nothing in any repo; every documented invocation relies on the 'engineer' default.**

- Detail: Drop the option and pass sender="engineer" (or "cli", matching the sibling `message` command) directly to send_message — the flag is speculative sender-attribution surface on a command that is itself documented as a plain alias for `message --wait`. Behavior identical for every caller that exists.
- Evidence: grep for `--source` across docs/, skills/, agents/, tests/, bobi/prompts/, bobi/templates/, ~/dev/bobi-deploy and ~/dev/moda-agents returns zero hits, while `bobi agent <name> ask "question"` (no flags) appears in docs/OVERVIEW.md, docs/QUICKSTART.md, skills/bobi.md, and bobi/prompts/base.md.

## Q047 — `bobi/cli.py:1661` (simplification, low, confirmed, phase 6)

**The doctor command's result loop uses `getattr(r, "required", True)` and `hasattr(r, "sandbox_error")` defensiveness against attributes that every result is guaranteed to have.**

- Detail: Both bobi.doctor.run_doctor and bobi.browser.run_doctor return the same `CheckResult` dataclass (bobi/browser.py:26 imports it from bobi.doctor), which declares `required: bool = True` and `sandbox_error: bool = False` as fields with defaults. Plain attribute access (`r.required`, `r.sandbox_error`) is equivalent and stops implying that heterogeneous result types can flow through here. Behavior preserved: no producer of these results lacks the fields.
- Evidence: bobi/doctor.py:12-27 defines CheckResult with both fields defaulted; grep of bobi/browser.py shows it imports and constructs the same CheckResult; no other result type reaches the loop.

## Q127 — `bobi/cli.py:2427` (over-abstraction, low, unverified, phase 6)

**`monitors add --url` is exercised by nothing in any repo, and Monitor.extra already accepts arbitrary keys (including url) straight from monitors.yaml.**

- Detail: Remove the option and its `extra["url"] = url` plumbing (cli.py:2471-2473); anyone needing a url on a monitor already gets it via the yaml path the schema was explicitly designed for. Flagging as unused surface rather than confident removal since it is documented in the command's own --help example.
- Evidence: grep for `--url` across tests/, docs/, skills/, agents/, prompts/, templates/, bobi-deploy and moda-agents finds only cloudflared examples and the option's own docstring example. bobi/monitors/schema.py:152 folds every non-reserved yaml key into `extra`, so `url:` in monitors.yaml reaches the check prompt (bobi/subagent.py:1597-1598) without any CLI support.

## D063 — `bobi/cli.py:2897` (duplication, low, confirmed, phase 6)

**The --requested-by JSON parse/validate block is copy-pasted identically in _dispatch_agent and _run_agent_wait.**

- Detail: cli.py:2856-2867 and cli.py:2897-2908 are the same 12 lines (json.loads, isinstance dict check, two error messages, SystemExit(1)). _dispatch_agent already parses requested_by before delegating in the non-wait path but passes the RAW string to _run_agent_wait which re-parses it — trivially consolidatable into one `_parse_requested_by` helper (or by parsing once in _dispatch_agent and passing the dict).
- Evidence: cli.py:2856-2867 (in _dispatch_agent) vs cli.py:2897-2908 (in _run_agent_wait): identical error strings '--requested-by must be a JSON object' / '--requested-by must be valid JSON' and identical control flow.

## Q090 — `bobi/compose.py:359` (simplification, low, confirmed, phase 6)

**merge_workspace hand-rolls a recursive copy that shutil.copytree(src, dest/'workspace', dirs_exist_ok=True) already provides.**

- Detail: The manual sorted(rglob)+mkdir+copy2 loop reproduces copytree's default behavior (copy2 per file, directories created, later layers overwrite earlier ones with dirs_exist_ok=True). One call per layer replaces the 9-line loop; leaf-wins semantics are preserved because layers are still iterated base→leaf and copytree overwrites existing files.
- Evidence: compose.py:359-369 — the loop's only work is dir creation plus shutil.copy2 keyed by relative path, exactly copytree(dirs_exist_ok=True)'s contract (available since Python 3.8; the codebase uses 3.10+ syntax like `str | None`).

## Q089 — `bobi/compose.py:507` (simplification, low, confirmed, phase 6)

**The monitor merge threads a redundant monitor_order list through three functions when the insertion-ordered monitor_records dict already carries the same order.**

- Detail: _accumulate_monitors appends to `order` only when the name is new to `records`, so order is exactly list(records.keys()) at all times. Dropping the order parameter from _accumulate_monitors and _seed_framework_monitors (return bool(records) instead of bool(order)) and iterating records directly in _compose_structured_dir removes one of three parallel data structures with byte-identical output — Python dicts preserve insertion order.
- Evidence: compose.py:549-567 — the only order.append is in the else-branch guarded by `name in records`; the consumers (lines 541, 545) iterate order solely to index records/src.

## Q088 — `bobi/compose.py:723` (simplification, low, confirmed, phase 6)

**_PRUNE_DIR_SURFACES is an identity-mapping dict whose values are never read and whose 'roles' entry is unreachable.**

- Detail: _prune_one only tests membership (`if surface in _PRUNE_DIR_SURFACES`) and then uses `surface` directly (`base = dest / surface`), never the mapped value; and `surface == "roles"` is handled and returned at line 766 before the dict check is reached, so the "roles" key is dead. Replace the dict with a tuple ("tools", "workflows", "context") — behavior identical, and the false suggestion that surfaces can be remapped disappears.
- Evidence: Read _prune_one (compose.py:751-789): line 766 returns for roles before line 772's membership test; grep shows _PRUNE_DIR_SURFACES referenced only at lines 723 and 772, both as a membership set.

## D088/Q124 — `bobi/config.py:336` (duplication, low, confirmed, phase 6)

**The launch-admission default values are hand-maintained in three places — the Config.launch_admission field default_factory, Config._parse_launch_admission's defaults dict, and the LaunchAdmissionPolicy dataclass defaults — which can silently drift.**

- Detail: A future tuning change (e.g. raising load_per_cpu_hard_limit to 2.5) edited in launch_admission.LaunchAdmissionPolicy would not take effect for any team, because policy_from_config reads every value from the config dict, whose independent copies of the defaults in config.py:336-344 and config.py:527-535 still say 2.0 — three copies, one authority unclear.
- Evidence: Identical literal sets {enabled: False, max_starting_agents: 1, load_per_cpu_soft_limit: 1.5, load_per_cpu_hard_limit: 2.0, min_memory_available_mb: 512, init_failure_window_seconds: 600, init_failure_backoff_threshold: 2} at bobi/config.py:336-344 (field default_factory), bobi/config.py:527-535 (_parse_launch_admission defaults), and mirrored as dataclass defaults at bobi/launch_admission.py:41-49.
- Cross-pass (Q124): The 7-key launch_admission defaults literal is written twice (field default_factory at 336-344 and inside _parse_launch_admission at 527-535), and the per-key `.get(key, default)` fallbacks at 539-548 are unreachable.

## Q109 — `bobi/config.py:496` (simplification, medium, plausible, phase 6)

**Three accepted spellings reach the one event-server-URL setting — `event_server: <str>`, `event_server: {url: ...}`, and `event_server_url:` — and the dict form has zero users anywhere.**

- Detail: One setting should have one spelling. Concrete shape: delete the dict branch (config.py:496-500 collapses to `event_server_url=raw.get("event_server_url", str(raw.get("event_server", "") or ""))` or, better, migrate the test fixtures and accept only `event_server`), and fix the ingress.py hint to name the house spelling. Behavior is preserved for every config that exists in any repo; only the never-used dict form stops parsing.
- Evidence: All 3 in-repo packs, all 5 moda-agents packs, setup authoring (bobi/setup/authoring.py:216 writes the string form), docs, and skills use `event_server: <string>`. `event_server_url:` as a raw yaml key appears only in bobi-agent unit tests (test_doctor_bubble.py, test_auth_bootstrap.py). A grep for the dict form (`event_server:` followed by `url:`) across bobi-agent, bobi-deploy, moda-agents, tests and templates returns nothing. Meanwhile ingress.py:143's error hint tells users to 'Set event_server_url' — the spelling no real pack uses.

## Q093 — `bobi/config.py:615` (structure, low, confirmed, phase 6)

**The event-server deployment/cursor/bubble state persistence (config.py:601-701) is transport session state, not package configuration, and sits in a module whose docstring scopes it to agent.yaml parsing.**

- Detail: deployment_state_path, session_cursor_path, load/save_deployment_state, and the bubble_state trio are JSON state-file IO for the event-server trust domain (their own comments point at bobi/events/server.py:ensure_bubble as the consumer). Housing them in config.py makes service.py and the events code import 'config' for things that are not config, and grows a 700-line module along an unrelated axis. Better shape: move the ~100 lines to bobi/events (e.g. events/state.py beside server.py) and update the handful of imports - pure relocation, no behavior change.
- Evidence: config.py module docstring: 'Runtime configuration from a Bobi Agent package... Machine-wide config.yaml is deliberately limited...'; lines 601-701 contain only state-file IO whose comments reference bobi/events/server.py:ensure_bubble; consumers are service.py (_wait_for_manager_transport, clear_manager_session) and the events layer, not config parsing.

## Q092 — `bobi/costs.py:156` (over-abstraction, low, confirmed, phase 6)

**rollup_costs takes a group_by parameter that its body never references.**

- Detail: The function always computes all four by_* maps; only format_costs actually switches on group_by. cli.py:3383 passes group_by=group_by into rollup_costs, which misleads readers into thinking the rollup is shaped by it. Better shape: drop the parameter from rollup_costs and the argument at the cli call site - output is unchanged because the parameter is dead.
- Evidence: Read of costs.py:156-257: no occurrence of group_by inside the function body. Callers: cli.py:3383 (passes it), webapp/runtime.py:494/507 and all of tests/test_costs.py call it without group_by.

## Q114 — `bobi/dep_bootstrap.py:88` (over-abstraction, low, plausible, phase 6)

**ResolvedRecipe.from_install and ResolvedRecipe.from_agent are the identical one-line function under two names.**

- Detail: Both classmethods are exactly `return cls._coerce(arg or {})`; the split implies divergent handling that does not exist and adds a third private layer (_coerce) under them. Collapse to a single public classmethod (e.g. `coerce`), updating the two call sites (materialize lines 261 and 294) and two tests — behavior identical. If the docstring distinction (verbatim install vs agent-reported) is worth keeping, keep it as a comment at the call sites where it actually applies.
- Evidence: dep_bootstrap.py:88-98 — both bodies call cls._coerce identically; callers are only lines 261, 294 and tests/test_dep_bootstrap.py:60-80 (verified by grep across both repos).

## Q091 — `bobi/dep_bootstrap.py:419` (simplification, low, confirmed, phase 6)

**pathlib.Path is imported inside four separate functions (and string-quoted in signatures) although a top-level stdlib import has no cycle risk.**

- Detail: team_has_bake (line 419), render_team_deps (line 458), _ensure_bootstrap_runtime (line 531), and _main (line 658) each do `from pathlib import Path`, forcing the quoted "Path" annotations at lines 410 and 435. The module's local-import pattern exists to break bobi-internal cycles (compose/tool_library/build_render), but pathlib is stdlib — hoist it to the top-level import block, unquote the annotations, and delete the four local imports. Behavior identical.
- Evidence: Top of dep_bootstrap.py imports only hashlib/json/logging/os/subprocess + bobi.brain/bobi.tool_library; grep shows four function-local `from pathlib import Path` at lines 419, 458, 531(via tempfile block), 658, while sibling modules (build_render.py, compose.py, local_deps.py imports) import Path at module level without issue.

## Q034 — `bobi/env.py:18` (missed-reuse, medium, confirmed, phase 6)

**env._configured_brain/pin_brain_from_root re-implement the brain-mapping extraction and defaults that Config._parse and the Config.brain_* properties already define, duplicating drift-prone constants.**

- Detail: Config._parse(path, env) already accepts an explicit env for interpolation - the very reason env.py gives for re-parsing agent.yaml by hand. pin_brain_from_root then re-encodes the wire_api default ('responses' appears both at config.py:416 and env.py:66) and the presence-based gateway declaration ('base_url' in brain at env.py:70 mirroring Config.brain_is_gateway at config.py:391-406). Better shape: have _configured_brain load via Config._parse(paths.agent_yaml_path(root), env) and read cfg.brain / the brain_* properties (keeping the existing try/except {} fallback), or at minimum hoist the shared 'responses' default and the declaration predicate into config.py so there is one definition. Behavior is preserved: _interpolate_env is the same resolver Config._parse applies, as env.py's own docstring notes.
- Evidence: env.py:29-45 hand-parses agent.yaml with yaml.safe_load + config._interpolate_env; config.py:448 Config._parse(cls, path, env=...) takes the same env parameter; the 'responses' fallback exists at both config.py:416 (brain_wire_api) and env.py:66; presence-based gateway logic exists at both config.py:404-406 and env.py:70.

## D095 — `bobi/events/adapters.py:81` (duplication, low, confirmed, phase 6)

**Running `git remote get-url origin` and normalizing the remote URL to a GitHub owner/repo slug is implemented twice: adapters._github_remote_key/_parse_github_url and orchestrator._remote_matches_slug plus its inline subprocess call.**

- Detail: The two normalizers already differ subtly: adapters._parse_github_url requires 'github.com' in the URL and splits on it; orchestrator._remote_matches_slug takes the last two path components of any remote. A remote URL shape handled by one (e.g. a GitHub Enterprise host, or ssh://git@github.com/org/repo) can be detected for event subscription by adapters yet fail _resolve_repo_root's slug match in the orchestrator (or vice versa), so the agent subscribes to a repo's events but the cleanup_worktree native action reports 'could not resolve target repo'. One shared remote-slug helper removes the drift.
- Evidence: bobi/workflow/orchestrator.py:992-1010 (_remote_matches_slug: strip .git, split ':'/'/' to owner/repo) plus 1046-1051 (subprocess git remote get-url origin) duplicating bobi/events/adapters.py:81-110 (subprocess git remote get-url origin + _parse_github_url strip .git, extract owner/repo).

## Q019 — `bobi/events/adapters.py:118` (missed-reuse, medium, confirmed, phase 6)

**adapters.py hand-rolls Slack channel name→ID resolution (_resolve_channel_names + _is_channel_id) that bobi/slack.py's resolve_channel_id already owns, and does it worse.**

- Detail: bobi/slack.py resolve_channel_id is the declared single code path for the ID-vs-name decision (setup/webui/server.py:1210 comment: 'One code path: resolve_channel_id owns the ID-vs-name decision') and is strictly more capable: it degrades to public-only listing on missing_scope instead of failing all names, excludes archived channels, supports @handle DMs, and uses the correct ID shape ([CGD][A-Z0-9]{6,}). The adapters copy fails every name when the token lacks groups:read, and its _is_channel_id accepts lowercase tails ('Cabc' passes isalnum()) and misses D-prefixed IDs while slack.py rejects/accepts them correctly. Better shape: resolve each configured channel via `try: resolve_channel_id(token, ch) except RuntimeError: log.warning(...)` — this preserves the existing drop-unresolvable semantics while deleting ~60 lines (lines 113-174). Only delta is one conversations.list pagination pass per name instead of one shared pass, which for the typical 1-3 configured channels is immaterial.
- Evidence: bobi/slack.py:311-362 (resolve_channel_id with missing_scope fallback, exclude_archived, D-prefix, @handle); bobi/setup/webui/server.py:1210-1214 names it the house path; adapters.py:113-174 re-paginates conversations.list without those behaviors; only caller is _detect_slack (adapters.py:217).

## D096 — `bobi/events/adapters.py:209` (duplication, low, confirmed, phase 6)

**_detect_slack inlines a Slack auth.test call (GET + bearer header + team_id/bot_id extraction) that events/server._slack_auth_info already provides, and adapters already imports the sibling helper _slack_app_id from that module.**

- Detail: adapters.py:209-220 needs exactly (team_id, bot_id) — the tuple _slack_auth_info returns — yet re-implements the request and parsing inline. A fix to the shared helper (e.g. retry on Slack 429, or handling the enterprise_id field for Enterprise Grid workspaces) won't reach the auto-detection path, so `bobi doctor`/registration (server path) and subscription auto-detection (adapters path) can disagree about the same token. The import boundary already exists in the same function (adapters.py:219 imports _slack_app_id from bobi.events.server).
- Evidence: bobi/events/server.py:604-623 (_slack_auth_info: pooled.get https://slack.com/api/auth.test, Bearer header, timeout 5.0, extracts team_id/bot_id/user_id) duplicated inline at bobi/events/adapters.py:209-220 (same GET, same header, same timeout, extracts team_id/bot_id).

## Q108 — `bobi/events/client.py:49` (consistency, medium, plausible, phase 6)

**Wall-clock timestamps are written in two conflicting conventions — local-naive time.strftime ISO strings in six modules vs timezone-aware UTC isoformat in four — while both in-repo parsers canonically interpret naive timestamps as UTC.**

- Detail: Nine sites write local-time, offset-less strings via time.strftime("%Y-%m-%dT%H:%M:%S"): events/client.py:49 (event log entries), memory.py:258, history.py:204/265, kb/store.py:34-35, workflow/orchestrator.py:292/326, workflow/state.py:87/156. Five sites write aware UTC via datetime.now(timezone.utc).isoformat(): registry.py:138, monitors/scheduler.py:594/699/775, monitors/script_cache_checks.py:627-628, setup/webui/server.py:788/807. The parse-side house rule is explicit: both _parse_iso helpers (scheduler.py:149-159, script_cache_checks.py:631-637) do dt.replace(tzinfo=utc) for naive values, i.e. naive == UTC — so every local-naive timestamp is misread by the host's UTC offset the moment any consumer applies the house parsing rule, and files under the same BOBI_HOME mix the two semantics (registry fetched_at aware-UTC next to events log local-naive). The aware-UTC style should win because it matches the parser convention and the fleet runs boxes/containers whose local TZ is not guaranteed; converge via one shared now_iso() helper. Behavior is preserved for every consumer that treats these as opaque strings, and becomes correct for consumers that parse them.
- Evidence: grep for time.strftime("%Y-%m-%dT%H:%M:%S") found 9 write sites in 6 modules; grep for datetime.now(timezone.utc) found 5 sites in 4 modules; read both _parse_iso implementations (scheduler.py:149, script_cache_checks.py:631) which assume naive-means-UTC; grep found zero datetime.utcnow or naive datetime.now() uses, confirming these are the only two conventions.

## D077/Q105 — `bobi/events/drain.py:76` (duplication, low, confirmed, phase 6)

**_without_placeholder_fields is duplicated verbatim in drain.py and channels.py.**

- Detail: Identical 5-line helper (copy event, pop placeholder_ts from fields) maintained in two modules that are already coupled (drain calls channels handlers); a future change to placeholder handling in one site (e.g. popping an additional field) silently diverges from the other, since drain.py applies its copy to slack.thread_reply events (line 98) while channels.py applies its copy inside SlackInputChannel.prepare (lines 111, 117). Trivially consolidatable: drain.py already imports from bobi.events.channels in _prepare_chat_events.
- Evidence: drain.py:76-80 and channels.py:92-96 contain byte-identical function bodies: `fields = dict(event.get("fields", {})); fields.pop("placeholder_ts", None); return dict(event, fields=fields)`.
- Cross-pass (Q105): _prepare_chat_events special-cases slack.thread_reply events, redundantly re-implementing the first branch of SlackInputChannel.prepare.

## Q064 — `bobi/events/server.py:236` (simplification, low, confirmed, phase 6)

**ensure_running remaps five unprefixed env vars to BOBI_ES_* with five identical two-line if-blocks.**

- Detail: WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN, DISCORD_BOT_TOKEN, DISCORD_APPLICATION_ID, and DISCORD_MESSAGE_CONTENT each get `if env.get(X): env['BOBI_ES_' + X] = env[X]`. A single `for var in ('WHATSAPP_APP_SECRET', ..., 'DISCORD_MESSAGE_CONTENT'): if env.get(var): env[f'BOBI_ES_{var}'] = env[var]` produces the identical env dict, keeps the two explanatory comments, and makes adding the next channel's vars a one-line change instead of another copied block.
- Evidence: server.py lines 236-248: five structurally identical conditionals differing only in the variable name; the pattern grew by accretion (#656 WhatsApp comment, then #2 Discord comment).

## Q112 — `bobi/events/server.py:413` (simplification, low, plausible, phase 6)

**authorize_resources repeats the same 6-line 'log warning / append unbacked / keep-if-not-filtering / continue' tail four times.**

- Detail: The unauthorized-topic outcome (credential missing, registration didn't back it, transport error, server denied) is handled by four near-identical blocks each recomputing `action = ... if filter_unauthorized else ...`, appending to unbacked, and conditionally appending to kept. A loop-local closure `def _unbacked(msg, *args): log.warning(msg + (' — dropping' if filter_unauthorized else ' — keeping'), *args); unbacked.append(sub); (not filter_unauthorized) and kept.append(sub)` collapses the four copies to one, shrinking the 90-line function by ~25 lines with identical log content and list contents. The function's control flow (which topics end up in kept/unbacked) is untouched.
- Evidence: server.py lines 391-399, 413-422, 428-436, 440-448: four occurrences of the action-string + log.warning + unbacked.append + conditional kept.append pattern inside one loop body.

## Q020 — `bobi/events/server.py:604` (structure, medium, confirmed, phase 6)

**_slack_auth_info and _slack_app_id are general Slack Web API helpers living in the event-server launcher module, privately imported across module boundaries by auth_bootstrap.py and adapters.py.**

- Detail: bobi/slack.py is the house home for direct-token Slack Web API calls (its docstring: 'What remains is the direct-token path used by system notifications... and channel-reference resolution for setup', with the _slack_api helper these two functions bypass by hand-rolling pooled.get + Bearer headers). Both helpers are pure Slack Web API lookups (auth.test, bots.info) with no event-server dependency, yet two other modules import them with underscore-private names from events/server.py (auth_bootstrap.py:208, adapters.py:219 — the latter wrapped in try/except purely to tolerate the awkward cross-module reach). Moving them into bobi/slack.py as public helpers removes the cross-module private imports and puts each Slack API call in the one module that owns that concern; register_slack_workspaces keeps calling them via a normal import. Pure move, behavior unchanged.
- Evidence: grep: bobi/auth_bootstrap.py:208 `from bobi.events.server import _slack_app_id, _slack_auth_info`; bobi/events/adapters.py:219 `from bobi.events.server import _slack_app_id` inside a try/except; bobi/slack.py docstring and _slack_api (slack.py:181) show the established direct-token Slack helper home.

## Q104 — `bobi/history.py:14` (consistency, medium, plausible, phase 6)

**history.py hardcodes Path.home()/'.claude'/'projects' for locating Claude transcripts while chat_history.py's _claude_projects_dirs() is the house pattern that also honors CLAUDE_CONFIG_DIR.**

- Detail: Two modules in the same package resolve 'where do Claude Code transcripts live' in conflicting ways. chat_history._claude_projects_dirs (chat_history.py:83) checks CLAUDE_CONFIG_DIR first then falls back to ~/.claude — necessary because bobi runs agent brains with CLAUDE_CONFIG_DIR set (#779 renders per-team CLAUDE.md there). history.index() scanning only ~/.claude/projects means the FTS index and the sleep-cycle delta can silently miss transcripts written under a configured CLAUDE_CONFIG_DIR. Sharing one helper (move _claude_projects_dirs to a common module, iterate its dirs in index()) makes the indexer consistent with the replay path; for deployments without CLAUDE_CONFIG_DIR behavior is byte-identical.
- Evidence: history.py:13-14 `CLAUDE_DIR = Path.home()/'.claude'; PROJECTS_DIR = CLAUDE_DIR/'projects'` used in index() at 283; chat_history.py:83-97 implements the env-aware variant; no shared helper exists (grep for _claude_projects_dirs shows only chat_history.py).

## Q035 — `bobi/http.py:57` (simplification, medium, confirmed, phase 6)

**post/get/put/delete each hand-build the same optional-kwargs dict instead of being one-line delegates to the module's own request() helper.**

- Detail: Five functions repeat the identical 'add json/content/headers/timeout if set' filtering. httpx.Client.post(url, ...) is exactly client.request('POST', url, ...), so post/get/put/delete can each become `return request('POST', url, json=json, content=content, headers=headers, timeout=timeout)`, keeping the None-filtering in one place (request). Cuts ~40 lines to ~8 with byte-identical wire behavior, and a future kwarg (e.g. params) gets added once instead of five times.
- Evidence: http.py:57-122: the same 4-branch kwargs construction appears in post, put, request and the 2-branch version in get, delete; all end in client().<verb>(url, **kwargs), which httpx documents as equivalent to client().request(verb, ...).

## D078 — `bobi/ingress.py:55` (duplication, low, confirmed, phase 6)

**The agent.yaml explicit `subscribe:` parsing (yaml load + env interpolation + str-or-list normalization + strip/filter) is implemented twice: ingress.explicit_subscriptions and subscriptions.discover_subscriptions/_normalize_explicit_subscriptions.**

- Detail: Both sites read paths.agent_yaml_path, yaml.safe_load, run _interpolate_env(raw.get("subscribe", []), project_env(project_path)), and normalize a str-or-list into stripped non-empty strings. If the subscribe schema changes (e.g. dict entries with per-topic options), one parser gets updated and the other drifts, making the ingress reachability warning disagree with what the session actually subscribes to. Consolidatable: ingress could call subscriptions' normalizer (or a shared helper) instead of re-implementing it.
- Evidence: ingress.py:55-79 (explicit_subscriptions) vs bobi/events/subscriptions.py:13-22 (_normalize_explicit_subscriptions) plus subscriptions.py:36-43 (same yaml.safe_load + _interpolate_env(raw.get("subscribe", []), project_env(...)) sequence).

## Q097 — `bobi/kb/embedder.py:146` (missed-reuse, low, confirmed, phase 6)

**embedder.stop() hand-parses the pid file (int(read_text().strip()) plus ValueError handling) while is_running() three functions above already uses sdk.read_pid for the same file.**

- Detail: sdk.read_pid is the repo's canonical tolerant pid-file reader (used in service.py and embedder.is_running). stop() should call `pid = read_pid(pid_p)` and kill only when truthy, dropping the manual int/strip/ValueError handling - same observable behavior (bad or missing pid means no kill, files still unlinked).
- Evidence: embedder.py:61 uses `pid_alive(read_pid(_pid_path()))` from bobi.sdk; embedder.py:147 re-implements the parse as `int(pid_p.read_text().strip())` with (ValueError, ProcessLookupError, OSError) handling.

## Q096 — `bobi/memory.py:181` (over-abstraction, low, confirmed, phase 6)

**cold_memory_kb_name() is a function wrapping the module constant COLD_MEMORY_KB_NAME, with exactly one caller.**

- Detail: The single caller (cli.py:3333-3336) imports the function just to get the constant. Importing COLD_MEMORY_KB_NAME directly removes an indirection that can never vary; the constant is already public in the same module.
- Evidence: grep -rn cold_memory_kb_name: definition at memory.py:181 and the sole use at cli.py:3333/3336; COLD_MEMORY_KB_NAME is defined two screens above at memory.py:41.

## Q033 — `bobi/memory.py:215` (structure, medium, confirmed, phase 6)

**The cold-memory sync code in memory.py reaches into KBStore internals (store._connect(), _fetchone, _chunk_text) in two places to answer a question that should be a KBStore method.**

- Detail: Both cold_memory_kb_needs_sync (lines 215-232) and sync_reference_to_cold_memory_kb (lines 262-279) open the raw connection themselves, import the private _fetchone/_chunk_text helpers, and run the same entry_count/vector_count SQL to decide 'is this source+hash fully indexed?'. The store's encapsulation is defeated and the staleness rule lives outside the store that owns the schema. Better shape: add one KBStore method, e.g. source_index_complete(source: str, source_hash: str, expected_chunks: int) -> bool (or one returning the two counts), and have both memory.py sites call it. Behavior is preserved - same SQL, same comparison - and the private imports disappear.
- Evidence: memory.py:215-232 and 263-279 each call store._connect() and import _fetchone/_chunk_text from bobi.kb.store; the SELECT COUNT ... LEFT JOIN entries_vec query and the expected==len(_chunk_text(content)) comparison appear at both sites; kb/store.py exposes no public API for this question.

## Q056 — `bobi/monitors/scheduler.py:84` (simplification, low, confirmed, phase 6)

**_load_framework_checks hand-rolls importlib (sys.modules check + spec_from_file_location + module_from_spec + exec_module) to load modules that are ordinary importable submodules of bobi.monitors.**

- Detail: The files it globs (bobi/monitors/*_checks.py) live inside the bobi.monitors package and are named with their canonical module names (module_name = f'bobi.monitors.{py_file.stem}'); tool_checks and script_cache_checks are already imported normally elsewhere (script_cache_checks itself does 'from bobi.monitors.tool_checks import ...'). importlib.import_module(module_name) performs exactly the sys.modules-cache-then-load dance this function replicates, so the body reduces to a 4-line loop: glob, import_module, collect CHECKS. The spec/loader machinery is only needed for _load_checks' pack-level files (bobi_checks.* under the installed monitors dir), which are genuinely outside any package — keep it there only.
- Evidence: scheduler.py:88-99 rebuilds import_module semantics for in-package files; the same file's _load_checks (138-146) is the case that actually needs spec_from_file_location because bobi_checks.* has no importable package parent.

## D093/Q058 — `bobi/monitors/scheduler.py:149` (duplication, low, confirmed, phase 6)

**_parse_iso (ISO-8601 parse with 'Z'->'+00:00' replacement and naive-to-UTC defaulting) is duplicated verbatim in two modules of the same package.**

- Detail: monitors/scheduler.py:149-159 and monitors/script_cache_checks.py:631-638 implement the identical function; a timezone/format fix applied to one (e.g. handling fractional-second or offset quirks from a new event source) silently misses the other, making scheduler last_run parsing and script_cache backoff_until parsing disagree on the same timestamp string. script_cache_checks already imports _parse_items/_items_to_conditions from tool_checks, so a shared home exists.
- Evidence: bobi/monitors/script_cache_checks.py:631-638 — same body: fromisoformat(value.replace('Z', '+00:00')), None on error, replace(tzinfo=timezone.utc) when naive.
- Cross-pass (Q058): Helper defs are interleaved into the module's import block, and several functions locally re-import stdlib modules that are already imported at module top.

## Q015 — `bobi/monitors/scheduler.py:1215` (simplification, medium, confirmed, phase 6)

**_on_sleep_cycle_result repeats the same ~10-line failure block seven times: build a detail string, log.warning('… - cursor NOT advanced, retrying next interval'), _publish_monitor_error(name, 'sleep-cycle', reason, detail, publish=self.publish), return.**

- Detail: The blocks at 1253-1264 (artifact unreadable), 1265-1277 (cap exceeded), 1278-1293 (working budget), 1302-1311 (reference unreadable), 1312-1322 (reference invalid), 1324-1335 (reference unchanged), and 1337-1349 (pointer missing) differ only in the reason slug and detail text. A local closure — def fail(reason: str, detail: str): log.warning(...); _publish_monitor_error(...) — turns each block into 'fail(reason, detail); return', collapsing roughly 70 lines to ~25 in the gnarliest function of the scheduler and making the next validation gate a two-line addition instead of another copy-paste. Log lines and published payloads stay byte-identical, so behavior is preserved. (Not verbatim inter-module duplication — it's one function's internal control-flow shape.)
- Evidence: Read of scheduler.py lines 1231-1349: seven early-return blocks with identical log format string '%s - cursor NOT advanced, retrying next interval' and identical _publish_monitor_error call shape, varying only in reason slug and detail interpolation.

## D094 — `bobi/monitors/script_cache_checks.py:564` (duplication, low, confirmed, phase 6)

**_scripts_dir() and the monitor-name sanitizer (replace('/', '_').replace('..', '_')) are duplicated between script_cache_checks.py and tool_checks.py even though the modules already share code.**

- Detail: Both helpers must resolve to the same directory (paths.state_dir()/'scripts') because tool_poll caches (<name>.sh) and script_cache artifacts (<name>.sc.sh, <name>.state.json) live side by side; if one copy's location or sanitization changes (e.g. hashing names to avoid collisions), the two subsystems split into different directories/name schemes and cached-script invalidation in one no longer sees the other's files. script_cache_checks.py:68-71 already imports helpers from tool_checks, so the import path exists.
- Evidence: bobi/monitors/tool_checks.py:60-69 (_scripts_dir: paths.state_dir()/'scripts' + mkdir; inline safe_name = monitor_name.replace('/', '_').replace('..', '_')) duplicated by bobi/monitors/script_cache_checks.py:564-572 (_scripts_dir + _safe_name, identical bodies).

## Q016 — `bobi/monitors/script_cache_checks.py:794` (consistency, medium, confirmed, phase 6)

**_slack_notify bypasses the module's own policy-resolution chokepoint: it re-reads and re-parses agent.yaml via _install_policy() and consults monitor.extra directly on every notification, instead of notify_channel being resolved once in _policy().**

- Detail: The house pattern in this module is explicit: _policy(monitor) overlays install-level script_cache config with per-monitor extra, is resolved once at the top of script_cache(), and is threaded into every consumer (_run_active, _self_heal, approve_pending). _slack_notify is the lone deviant — it duplicates the 'extra overrides install' resolution inline (monitor.extra.get(...) or _install_policy().get(...)) and pays a yaml.safe_load of agent.yaml per notification. notify_channel is also absent from _policy's base dict and from the module docstring's config list, so half the config surface is invisible at the documented resolution point. Better shape: add 'notify_channel': None to _policy's base, thread the already-resolved policy dict through _notify/_bump_failure/_queue_review (policy is in scope at every _notify call chain), and have _slack_notify read policy['notify_channel']. Same resolved value, one resolution path, no repeated yaml parsing.
- Evidence: script_cache_checks.py:661-675 (_policy base lacks notify_channel), :794 (inline two-source resolution + _install_policy() call which re-parses agent.yaml at :645-658), module docstring config list :38-46 omits notify_channel; every other config key flows through the policy dict passed down from script_cache() at :1104.

## Q018 — `bobi/registry.py:52` (simplification, medium, confirmed, phase 6)

**The project_path parameter is threaded through ~15 registry functions but never actually used — the cache and registry list are global.**

- Detail: _cache_dir(project_path) ignores its argument and returns paths.agent_cache_dir() (a no-arg global under home_dir()/cache/agents, per the 'shared cache, D-3' comment), and _all_registries(project_path) likewise reads only paths.ensure_global_config(). Every public function (cache_path, cached_version, is_cached, check_update, fetch, fetch_from_url, fetch_from_archive, list_cached, list_remote) demands a project_path that has zero effect, forcing callers in build.py, compose.py, cli.py, and setup/open_mode.py to fabricate one. Worse, list_remote branches on `_all_registries(project_path) if project_path else [DEFAULT_REPO]` — the branch semantics ('with a project you get user registries') are accidental, since _all_registries never looks at the project. Dropping the parameter is a mechanical shrink of every signature and call site with identical behavior.
- Evidence: grep -n project_path bobi/registry.py shows it only ever forwarded, never dereferenced; bobi/paths.py:286 agent_cache_dir() takes no args; _all_registries reads paths.ensure_global_config() only. Callers: build.py:106-108, compose.py:162-186, cli.py:640/2976/3085.

## Q028 — `bobi/sdk.py:51` (structure, medium, confirmed, phase 6)

**Claude-CLI path resolution (_resolve_cli_path/get_cli_path/CLAUDE_CLI) lives in the generic session-registry module instead of the claude brain adapter.**

- Detail: sdk.py is the brain-agnostic session registry; locating the `claude` binary is vendor knowledge that the #485 brain seam was built to contain. brain/claude.py already has to lazily `from bobi.sdk import get_cli_path` inside both make_session and stream_once (one leg of the sdk<->brain lazy-import tangle: sdk.py in turn lazily imports bobi.brain in save_session_id/load_resumable_session_id). Moving _resolve_cli_path/get_cli_path into bobi/brain/claude.py (webapp/server.py:99 updates its one import) removes vendor logic from sdk.py and deletes the cross-module lazy imports; behavior is unchanged since get_cli_path re-resolves on every call regardless of where it lives. Test patches of 'bobi.sdk.get_cli_path' would move mechanically.
- Evidence: Production consumers of get_cli_path: only bobi/brain/claude.py (2 call sites) and bobi/webapp/server.py:99-101. sdk.py module docstring itself claims scope 'Session registry'; the CLI resolver's only tie to the rest of the module is co-location.

## Q085 — `bobi/sdk.py:121` (consistency, low, confirmed, phase 6)

**Bound-root access is split between two routes: monitors import paths.bound_root directly (aliased as get_project_root) while kb/embedder.py and events/drain.py go through the sdk.get_project_root delegate.**

- Detail: sdk.get_project_root/set_project_root are documented pure delegates to bobi.paths ('delegates to bobi.paths'), and the monitors package already bypasses them with 'from bobi.paths import bound_root as get_project_root' (registry.py:31, scheduler.py:130) — that aliasing is itself evidence of a half-finished migration. House pattern should be bobi.paths.bind_root/bound_root everywhere; converting kb/embedder.py:78 and events/drain.py:29 (and eventually service.py's set_project_root calls) lets the sdk wrappers retire and stops two names meaning one thing. Behavior preserved: the delegates add nothing.
- Evidence: bobi/monitors/registry.py:31, scheduler.py:130 use paths.bound_root aliased AS get_project_root; bobi/kb/embedder.py:78 and bobi/events/drain.py:29 import bobi.sdk.get_project_root; sdk.py:116-123 shows the wrappers are one-line delegates.

## Q084 — `bobi/sdk.py:538` (missed-reuse, low, confirmed, phase 6)

**load_resumable_session_id re-reads '<name>.brain' inline instead of calling load_session_brain defined 30 lines above.**

- Detail: Lines 538-541 (`brain_path = _sessions_dir() / f"{name}.brain"; recorded_brain = brain_path.read_text().strip() if brain_path.exists() else ""`) are exactly load_session_brain(name)'s body (sdk.py:508-517). Calling the helper keeps the '.brain' file format/location in one place; behavior identical.
- Evidence: sdk.py:508-517 vs sdk.py:538-541 — same path construction, same exists/read/strip/'' fallback.

## Q021 — `bobi/setup/actions.py:185` (over-abstraction, medium, confirmed, phase 6)

**save_credential's prompt_fn injection is vestigial: every live caller passes a constant-returning lambda, and the only real prompt implementation (default_secret_prompt) has zero callers repo-wide.**

- Detail: The callback exists to support a masked-terminal-prompt path that was removed with tools.py; today the value always arrives in the POST payload before save_credential is called, so the indirection is pure ceremony (and the docstring claim that the function 'prompts the user directly' is false). Simpler shape: `save_credential(state, project, var_name, service, value)` taking the value directly; delete default_secret_prompt (which also drops actions.py's only use of click) and fix the stale docstrings. Behavior is preserved because every caller already computes the value up front.
- Evidence: grep for save_credential/prompt_fn: all 5 production callers are in webui/server.py and every one passes `prompt_fn=lambda *_: value` (lines 1028, 1052, 1129, 1234, 1297). grep for default_secret_prompt across bobi/ and tests/: only the definition at actions.py:108 — no callers, not even tests. The module docstring still describes 'the @tool wrappers in tools.py today' as the caller layer, but bobi/setup/tools.py does not exist (ls of bobi/setup/).

## Q006 — `bobi/setup/actions.py:353` (structure, medium, confirmed, phase 6)

**The setup web UI's library module imports `_install_pack`, `_write_install_gitignore`, and `_resolve_agent_pack` from `bobi.cli` instead of from `bobi.install`, inverting the dependency direction the #525 extraction created.**

- Detail: bobi/install.py's own docstring says it was extracted from the CLI so every install caller (CLI, setup UI, webapp) shares one path, with the CLI re-exports kept only 'for back-compat'. Yet actions.py:175 and :353 still import through `bobi.cli`, which drags in click and executes `truststore.inject_into_ssl()` at import time just to reach two functions that live in bobi.install. The better shape: actions.py imports `install_pack`/`write_install_gitignore` from bobi.install, and `_resolve_agent_pack` (a pure path lookup with no click dependency, cli.py:333) moves into bobi/install.py or bobi/registry.py so both cli.py and actions.py call it there. Behavior is identical — the aliases are pure re-exports.
- Evidence: bobi/cli.py:16-19 shows `_install_pack`/`_write_install_gitignore` are bare aliases of bobi.install functions; grep shows bobi/setup/actions.py:175 and :353 are the only non-test library importers of these names from bobi.cli; bobi/install.py docstring states the CLI names exist only for back-compat.

## Q070 — `bobi/setup/mcp_registry.py:45` (over-abstraction, low, confirmed, phase 6)

**MCPServerSpec carries a configurable auth-header micro-DSL (auth_header + auth_value with {ref} formatting) and a transport field that no spec in the registry ever sets to anything but the default.**

- Detail: Config surface nobody sets: headers() can simply return `{"Authorization": f"Bearer ${{{self.secret_var}}}"}` and server_config() can hardcode type='http', deleting three fields and the .format(ref=...) indirection. Behavior is byte-identical for every registry entry; if a future hosted MCP genuinely needs a custom header, adding the field back then is a two-line change.
- Evidence: All five entries in _SPECS (stripe, huggingface, sentry, context7, deepwiki) leave auth_header='Authorization', auth_value='Bearer {ref}', and transport='http' untouched; headers() is the only consumer of the format string. grep shows no other construction site for MCPServerSpec in bobi/ or tests/.

## Q071/D123 — `bobi/setup/webui/server.py:85` (missed-reuse, low, confirmed, phase 6)

**serialize_state hardcodes the four spec-slot names as a literal tuple instead of using the canonical SPEC_SLOTS constant from bobi.setup.state.**

- Detail: Redundant literal that can silently drift if a slot is ever added (readiness_for raises on unknown slots, so drift would surface as a 500 in one place and not the other). Import SPEC_SLOTS alongside the existing state imports on line 40 and iterate it — identical output today, one source of truth going forward.
- Evidence: server.py:85 iterates `("goal", "roles", "autonomous", "services")` while state.py:59 defines SPEC_SLOTS with exactly that value and digestion.py:197 already builds the identical readiness dict via `{s: spec.readiness_for(s).value for s in SPEC_SLOTS}`.
- Cross-pass (D123): _probe_event_server reimplements the event-server /health probe with raw urllib, despite events/server.health() declaring itself the single definition of healthy and bobi/http.py being the mandated outbound HTTP client.

## Q069 — `bobi/setup/webui/server.py:108` (consistency, low, confirmed, phase 6)

**_probe_event_server hand-rolls an outbound HTTP request with raw urllib.request, against the repo's explicit house rule that framework code uses the pooled bobi.http client.**

- Detail: The setup server is the odd one out for no benefit: rewrite _probe_event_server on `bobi.http.get` (same /health GET, same status/'auth'=='hmac' payload checks, same error strings mapped from httpx exceptions), dropping the four urllib imports. The extra hmac check and per-failure error detail justify not reusing events.server.health() directly, but not the raw-urllib transport.
- Evidence: bobi/http.py module docstring: 'All framework code that makes outbound HTTP requests should use this module instead of raw urllib.request' — 22 modules follow it (grep 'from bobi import http'), including the canonical event-server health probe bobi/events/server.py:112. server.py imports HTTPError/URLError/UrlRequest/urlopen (lines 26-29) solely for this one function.

## Q022 — `bobi/setup/webui/server.py:543` (structure, medium, confirmed, phase 6)

**The entire MCP connection-test conversational flow (~130 lines: _propose_test, _resolve_pending, _record) lives as nested async generators inside the /api/message route closure, while its other half (the intent/confirmation matchers) lives in mcp_probe.**

- Detail: This is the largest handler in the 1.5k-line route file, and the test-a-connection dialogue is split across two modules: propose/resolve wording and state mutation in a route closure, intent parsing in mcp_probe. Better shape: move _propose_test/_resolve_pending (and the _record helper) to mcp_probe (or a small mcp_test_chat module) as module-level async generators taking (state, project, text, hit/decision), keeping only dispatch + _sse wrapping in the route. Pure code motion — the generators already receive everything they need as closure variables that become parameters — so behavior is identical, /api/message shrinks to ~60 lines, and the dialogue becomes unit-testable without a TestClient.
- Evidence: server.py lines 533-727: the /api/message handler is ~195 lines, of which _propose_test (543-589) and _resolve_pending (591-653) are pure conversation logic that only touches state, project, and mcp_probe.probe(); the matching functions they pair with (match_connection_test, match_test_confirmation) already live in bobi/setup/mcp_probe.py (lines 241, 284). The generators yield plain text chunks — the SSE wrapping happens outside them in gen().

## Q023 — `bobi/setup/webui/server.py:1284` (consistency, medium, confirmed, phase 6)

**Credential resolution precedence is handled in two conflicting styles: the documented house pattern is process-env-first (exported var wins over .env), but /api/credential/value and mcp_probe read .env first.**

- Detail: When a var is set in both places with different values, the copy-to-clipboard endpoint and the MCP probe use a different credential than `bobi agent start` will actually run with — exactly the drift the venn_key comment exists to prevent. Better shape: one `actions.env_value(project, var)` helper implementing the documented process-env-first order (venn_key becomes `env_value(project, "VENN_API_KEY")`), used by _env_value, credential_value, and mcp_probe. Behavior is unchanged in the normal single-source case and converges on the documented runtime precedence otherwise.
- Evidence: House pattern, stated twice with rationale: actions.venn_key (actions.py:138 — 'Same precedence as runtime resolution (config.load_dotenv): an exported environment variable wins over .env') and server.py _env_value (line 1190 — same comment). Deviants: server.py:1284 `actions.read_env(project).get(var) or os.environ.get(var, "")` (reversed), mcp_probe.py:48 `saved.get(var) or os.environ.get(var)` and mcp_probe.py:215-216 (both .env-first).

## D052 — `bobi/slack.py:311` (duplication, medium, confirmed, phase 6)

**Slack channel-name-to-ID resolution via paginated conversations.list is implemented twice with already-divergent behavior: slack.resolve_channel_id and events/adapters._resolve_channel_names.**

- Detail: The two copies have drifted: slack.py degrades to public-only channels when the token lacks groups:read (missing_scope retry, slack.py:349-351), uses limit=1000 and exclude_archived; adapters.py uses limit=200, no missing_scope fallback, and on the first non-ok response breaks and silently drops ALL unresolved names (adapters.py:148-150,167-172). Concretely: a bot token without groups:read plus channels configured by name — the slack.py path still resolves public channels, but the adapters path drops every name, and _slack_keys(team_id, []) then subscribes the agent to the entire workspace instead of the configured channels (adapters.py:185-190). One shared resolver would make both paths behave identically.
- Evidence: bobi/events/adapters.py:115-174 (_resolve_channel_names: same cursor-paginated GET https://slack.com/api/conversations.list, lowercase name match, next_cursor loop) duplicating bobi/slack.py:311-362 (resolve_channel_id).

## Q010 — `bobi/subagent.py:1302` (simplification, medium, confirmed, phase 6)

**_start_event_subscription's 5-branch registration decision tree re-implements the authorize→PUT-subscriptions→on-failure-re-register sync sequence twice with only cosmetic differences.**

- Detail: The local-server branch (1314-1343) and the final else branch (1356-1393) both do: ensure_bubble → optional _register_channel_credentials → authorize_resources → pooled.put(.../subscriptions {replace}) → raise_for_status → set active_subscriptions, falling back to cursor-unlink + _register_with_retry on failure. Differences are log wording and whether authorize failure falls back to the raw subscribe list — reconcilable into one `_sync_or_reregister()` local helper. With that helper the branch tree collapses to: (1) resolve/start server if local, (2) if no saved deployment or no bubble.json → register fresh, (3) else sync-or-reregister — roughly 60 fewer lines in a 310-line function that is already the hardest part of the file to follow. Behavior preserved because both existing blocks converge on the same two outcomes (synced subscriptions or a fresh registration).
- Evidence: Read of subagent.py 1293-1393: the two blocks share the ensure_bubble/_register_channel_credentials/authorize_resources/PUT/raise_for_status/active_subscriptions sequence; on-exception both end in cursor_path.unlink + _register_with_retry.

## Q051/D070 — `bobi/subagent.py:1738` (simplification, low, confirmed, phase 6)

**run_check_blocking, run_gate_blocking, and run_curator_blocking each repeat the same ~12-line preamble: local `import hashlib`, sha256 slug, _session_name(role, phase), and registry.register(SessionEntry(role=..., phase=..., status='starting')).**

- Detail: A `_register_verdict_session(seed, name, role, phase, title, cwd) -> tuple[slug, session]` helper collapses three copies into one, and lets `hashlib` move to the module imports (it is also locally imported a fourth time in spawn_adhoc). Behavior preserved: same slug derivation and registry entry per caller.
- Evidence: subagent.py 1729-1743, 1945-1961, 2019-2033: identical structure differing only in role/phase/title and the hash seed string.
- Cross-pass (D070): run_check_blocking re-implements the verdict-normalization (finding/summary/details coercion) that _parse_check_output already provides, leaving _parse_check_output with no production callers (tests only).

## Q029 — `bobi/tool_library.py:44` (structure, medium, confirmed, phase 6)

**The tool_library.py module deliberately shadows the sibling bobi/tool_library/ data directory, a hazard the module spends a docstring paragraph explaining instead of eliminating.**

- Detail: A regular module shadowing a same-named namespace-package directory means the directory can never hold Python code, confuses IDEs/collection tools, and requires the CATALOG_DIR = Path(__file__).parent / "tool_library" indirection plus an explanatory docstring (lines 43-45). The cleaner shape preserving all import paths AND the user-facing docs path (docs/TOOL_LIBRARY.md documents bobi/tool_library/<name>/): move the module's code into bobi/tool_library/__init__.py so the catalog entry dirs (codex/, gstack/, venn/) become plain package data inside a real package, and CATALOG_DIR becomes Path(__file__).parent. Every `from bobi.tool_library import X` site (dep_bootstrap, local_deps, cli, host_caps, compose, bobi-deploy) is unchanged; hatch packages the `bobi` tree wholesale so wheel contents are unchanged.
- Evidence: bobi/tool_library.py lines 43-45 explicitly document the shadowing; ls shows bobi/tool_library/{codex,gstack,venn} contain only tool.yaml+guide.md (no .py); pyproject.toml packages `bobi` wholesale (packages = ["bobi"]); all importers use `from bobi.tool_library import ...` which resolves identically from an __init__.py.

## D051/Q123 — `bobi/webapp/daemon.py:92` (duplication, medium, confirmed, phase 6)

**webapp/daemon.py reimplements pid-file helpers (_pid_alive, _read_int) that already exist as bobi.sdk.pid_alive / bobi.sdk.read_pid, and the copies have already diverged on PermissionError semantics.**

- Detail: sdk.pid_alive (sdk.py:126-136) treats PermissionError from os.kill(pid, 0) as ALIVE (returns True); daemon._pid_alive (daemon.py:92-99) treats it as DEAD (returns False). If the webapp daemon pid belongs to another uid (e.g. it was once started via sudo or from a container-shared BOBI_HOME), daemon.status() declares it not running and `bobi app start` spawns a second daemon that fails to bind the port and overwrites app.pid, orphaning the live process. service.py demonstrates the correct pattern by delegating (service.py:279-288). Consolidatable: import read_pid/pid_alive from bobi.sdk.
- Evidence: bobi/sdk.py:126-144 (pid_alive returns True on PermissionError; read_pid identical to _read_int) vs bobi/webapp/daemon.py:85-99 (except (ProcessLookupError, PermissionError): return False).
- Cross-pass (Q123): _ping uses raw urllib.request.urlopen — called in a 0.2s startup polling loop — instead of the pooled bobi.http client mandated by that module's docstring.

## D097/Q024 — `bobi/webapp/daemon.py:203` (duplication, low, confirmed, phase 6)

**daemon.py re-implements three launch fragments that webui_common/launcher.py already owns: the socket-bind + uvicorn serve pattern, the persisted-token mint, and the delayed browser open.**

- Detail: run_foreground() (lines 216-235) hand-rolls AF_INET socket + SO_REUSEADDR + bind + uvicorn.Server(uvicorn.Config(app, log_level="warning")) + server.run(sockets=[sock]) + KeyboardInterrupt/finally sock.close() — the exact body of launcher._serve_socket (lines 24-29) and serve_local's serve loop (lines 46-63). ensure_token() (lines 69-82) repeats serve_container's token contract (launcher.py lines 78-86: secrets.token_urlsafe(24), write to a state file, chmod 0o600). _open_browser() (lines 241-245) repeats serve_local line 52 (threading.Timer + webbrowser.open). Any future change to the shared launch contract (e.g. IPv6 bind handling in _serve_socket, uvicorn config, token length/permissions) must now be made in two places; launcher.py is the module explicitly built to hold these patterns and daemon.py bypasses it.
- Evidence: bobi/webui_common/launcher.py:24-29 (_serve_socket: same socket/SO_REUSEADDR/bind), :46-63 (same uvicorn.Server(Config(app, log_level="warning")) + run(sockets=[sock]) + finally sock.close()), :78-86 (same token_urlsafe(24) + write_text + chmod 0o600), :52 (same threading.Timer(...webbrowser.open) as daemon.py:241-245)
- Cross-pass (Q024): run_foreground/_open_browser/ensure_token re-solve the serve primitives that webui_common/launcher.py already owns; the right shape is shared primitives in launcher.py, not a third hand-rolled serve path (and not one god-launcher API either).

## Q075 — `bobi/webapp/server.py:158` (consistency, low, confirmed, phase 6)

**Success-only GET handlers inconsistently wrap runtime dicts in JSONResponse (agent_spend, agent_health, agent_sessions, agent_status, subagents, start/stop/restart) while sibling handlers return plain dicts (ping, dashboard, fleet_spend, setup_current).**

- Detail: With no response_model declared, FastAPI serializes a returned dict to the same JSON body and 200 status as JSONResponse(dict) — the wrapper buys nothing on these routes (unlike setup_open/subagent_messages/chat, which need it for non-200 statuses). The house pattern for a plain success payload is the bare dict return, as ping/dashboard/fleet_spend/setup_current already do; making the eight wrapper-only handlers return `rt.<method>(name)` directly drops the JSONResponse import pressure and one layer of noise per route with byte-identical responses.
- Evidence: Read server.py in full: lines 143-155 and 196-200 return dicts; lines 157-189 and 297-299 wrap identical always-200 payloads in JSONResponse; none of these routes declares a response_model, so serialization behavior is the same either way.

## Q107 — `bobi/webapp/server.py:246` (structure, medium, plausible, phase 6)

**The setup_open handler's on_finish closure embeds ~30 lines of slot-rename filesystem business logic (shutil.move of agent dirs, nested run/ salvage, rmdir) inside an HTTP route in a module whose own charter is HTTP mapping.**

- Detail: server.py's module docstring says handlers own 'HTTP mapping (routes, status codes, input validation)', yet on_finish (lines 246-276) implements slot-identity management: comparing state.team_name to the placeholder name, moving agents/<old>/ to agents/<final>/, salvaging run/ into an existing target, and best-effort rmdir. This is setup-domain logic that is untestable except through the full HTTP + SetupState flow. Better shape: a plain function in bobi/setup (e.g. open_mode.finalize_slot(placeholder, final, source_dir) or a small slots helper) that on_finish calls with the two names — same operations in the same order, so behavior is preserved, but the rename semantics become a unit-testable seam and the route shrinks to its HTTP job. The adjacent nested-source tolerance block (lines 224-232) already delegates to open_mode.list_teams_in — that is the house pattern this closure violates.
- Evidence: Read server.py 191-293; the module docstring (lines 13-15) claims HTTP-mapping-only scope; open_mode already hosts the sibling source-shape logic (open_mode.is_team/list_teams_in called at lines 223-227), showing where this logic's home is.

## Q072 — `bobi/webui_common/launcher.py:37` (over-abstraction, low, confirmed, phase 6)

**serve_local's `announce` callback parameter (and the Announcer type alias) has exactly one caller, whose lambda reproduces the default label-based message character-for-character.**

- Detail: The only announce user is setup/webui/server.py:1468, passing `lambda url: f"\n  bobi setup is running at {url}\n  (Ctrl-C to stop)\n"` — identical output to the default branch `f"\n  {label} is running at {url}\n  (Ctrl-C to stop)\n"` given the label="bobi setup" that the same call already passes. Delete the announce parameter, the Announcer alias, and the caller's lambda; keep label. Output is byte-identical, so behavior is preserved, and the launcher loses a config surface nobody meaningfully sets.
- Evidence: grep -rn announce across bobi/ and tests/: only launcher.py's definition and setup/webui/server.py:1468 (the tests/test_setup_server.py hit is an unrelated test name). Compared the two f-strings with label="bobi setup": identical.

## Q060 — `bobi/workflow/orchestrator.py:108` (over-abstraction, low, confirmed, phase 6)

**_find_project_root(cwd) takes and ignores a cwd parameter, and both call sites pass a meaningful-looking cwd that has no effect.**

- Detail: The body is a one-line return of bobi_root(); the docstring itself says 'cwd plays no part'. Yet both callers (line 377 passing the possibly-worktree cwd, line 1126 in _execute_notify_step) pass cwd as if it influenced resolution, and _execute_notify_step threads a cwd parameter through its own signature partly to feed it. Better shape: drop the parameter (keep the module-level function — tests/test_notify_step.py:465 patches it as a seam, so it should stay a named function) so the signature stops implying cwd-relative resolution. Behavior preserved: the argument is already dead.
- Evidence: orchestrator.py:108-113 (parameter unused, docstring says so); callers at 377 and 1126 pass work_cwd/cwd; tests patch bobi.workflow.orchestrator._find_project_root by name, so the function itself is a live test seam.

## Q063 — `bobi/workflow/orchestrator.py:128` (consistency, low, confirmed, phase 6)

**_setup_worktree re-imports subprocess locally as sp even though the module already imports subprocess at top level and uses it directly elsewhere in the same file.**

- Detail: The house pattern in this module is: stdlib imports at module top (subprocess is imported at line 18 and used plainly in _resolve_repo_root at line 1046), with deferred imports reserved for bobi-internal modules (bobi.config, bobi.brain, etc., presumably for import-cycle/startup reasons). _setup_worktree's `import subprocess as sp` is the lone deviant — it shadows an already-loaded stdlib module under an alias for no benefit. Deleting the local import and using subprocess.run directly is behavior-identical.
- Evidence: orchestrator.py line 18 (top-level `import subprocess`), line 128 (`import subprocess as sp` inside _setup_worktree), line 1046 (plain subprocess.run in _resolve_repo_root).

## Q061 — `bobi/workflow/orchestrator.py:435` (simplification, low, confirmed, phase 6)

**_make_session's if/else on agent_name executes the identical call in both branches.**

- Detail: Both branches call resolve_agent_prompt(<agent_name or "">, project_root, interactive=interactive); the else branch just passes the same empty string agent_name already holds. The 6 lines collapse to `agent_prompt = resolve_agent_prompt(agent_name, project_root, interactive=interactive)`. resolve_agent_prompt's first parameter is `role: str` and it receives "" either way, so behavior is byte-identical.
- Evidence: orchestrator.py:435-439; resolve_agent_prompt signature at bobi/prompts/resolver.py:100-105 confirms the first positional is a plain str with no branching on identity of caller.

## D098 — `bobi/setup/webui/static/app.css:8` (duplication, low, confirmed, phase 7)

**The framed app-window chrome (.app rule, body radial-gradient background, and the data-retro grid overlay) is copied verbatim between the setup UI and webapp stylesheets instead of living in webui_common/static like tokens.css.**

- Detail: The .app rule (width:min(1240px,96vw); height:min(800px,94vh); margin:2.6vh auto; identical border/box-shadow/radius) and the html[data-retro="on"] body overlay are declaration-for-declaration identical in both files (verified by normalizing and diffing the rule bodies); the webapp copy's own comment says 'same treatment as the setup flow, so moving between the shell and the hosted editor reads as one app'. That cross-UI visual identity is maintained only by manual sync — a DESIGN.md-driven tweak to the frame in one file silently diverges the other. webui_common already has the delivery mechanism (SHARED_ASSET_NAMES routes tokens.css from webui_common/static through both apps' /static/ mounts), so a shared chrome.css would eliminate the copy.
- Evidence: bobi/webapp/static/app.css:28-40 (.app — byte-identical declarations to setup app.css:8), :22-27 (html[data-retro="on"] body — identical repeating-linear-gradient overlay to setup app.css:6), :18 (identical body radial-gradient to setup app.css:5); sharing mechanism at bobi/webui_common/__init__.py:6 (SHARED_ASSET_NAMES = {"tokens.css"})

## D099 — `bobi/setup/webui/static/app.js:20` (duplication, low, confirmed, phase 7)

**HTML-escaping is hand-rolled in three variants across the two UIs: setup app.js esc (5 chars incl. quotes), agent.js esc (3 chars, pre-markdown), and an inline strip in agent.js's header template.**

- Detail: setup app.js:20-21 escapes &<>"' for both text and attribute sinks; bobi/webapp/static/views/agent.js:177-178 defines a second esc escaping only &<>; agent.js:17 additionally inlines name.replace(/[&<>]/g, "") because the file's own esc is not yet initialized at that point. Three sites now own the XSS-relevant escaping decision; a fix or hardening applied to one (e.g. adding quote escaping for attribute contexts) does not propagate, and the 3-char variants are silently unsafe if ever reused in an attribute sink. Consolidatable into one shared escape helper in a webui_common-served module alongside the fetch wrapper.
- Evidence: bobi/webapp/static/views/agent.js:177-178 (const esc = (s) => s.replace(/&/g,...).replace(/</g,...).replace(/>/g,...)) and agent.js:17 (name.replace(/[&<>]/g, "")) — second and third escaping implementations beside setup app.js:20-21

## D124 — `bobi/setup/webui/static/app.js:90` (duplication, low, plausible, phase 7)

**The token-header JSON fetch wrapper with server-gone health tracking is implemented independently in both SPAs: setup app.js getJSON/postJSON + markDisconnected/heartbeat vs shell.js api() + noteFailure/missedPings.**

- Detail: Both wrappers implement the same client contract against install_security: read the token from a meta tag, attach the hard-coded "x-bobi-webui-token" header (setup app.js:7,15; shell.js:9-11,22), JSON-encode POST bodies, parse JSON responses, and flip a 'server stopped' UI on fetch failure (setup: markDisconnected overlay lines 67-88 plus a 4s /api/ping heartbeat at lines 2376-2383; shell: noteFailure/noteSuccess lines 37-51 driving the #gone overlay). A change to the auth contract (header rename, moving the token out of the page) or the disconnect UX must be re-implemented twice and can drift. Both apps already serve shared static assets through webui_common's resolve_static_asset, so a shared client module (like tokens.css) is the natural consolidation point.
- Evidence: bobi/webapp/static/shell.js:16-51 (api() sets "x-bobi-webui-token" from meta, JSON body/response, noteSuccess/noteFailure health tracking) — second implementation of setup app.js:90-107 (getJSON/postJSON with header H from meta[name=bobi-nonce], markConnected/markDisconnected) plus app.js:2376-2383 (heartbeat poll)

## Q073 — `bobi/webapp/static/shell.js:166` (dead-code, low, confirmed, phase 7)

**The dynamic `import("./views/agent.js").catch(() => null)` with the stub() fallback ('The agent view is coming in this build.') is leftover scaffolding from before agent.js existed.**

- Detail: agent.js ships in the same static dir as dashboard.js, which shell.js imports statically at the top; if agent.js failed to load, the static dashboard import would be equally broken, so the fallback protects nothing real. Simplify: `import { mountAgent } from "./views/agent.js"` alongside the dashboard import, drop the await/catch, drop the now-caller-less stub() helper (mountSetupEntry builds its own DOM and never calls stub). Behavior is preserved in every reachable state — the stub only renders when the server fails to serve its own bundled asset. The stub's own copy ('coming in this build') confirms it was a pre-agent-view placeholder.
- Evidence: views/agent.js exists (937 lines, read in full); stub() at shell.js:148 has exactly one caller — the import-failure branch at line 172; dashboard.js is imported statically at shell.js:7, establishing the pattern.

## Q076 — `bobi/webapp/static/views/agent.js:17` (consistency, low, confirmed, phase 7)

**The header interpolates the agent name into an innerHTML template with a hand-rolled `name.replace(/[&<>]/g, "")` strip — the single deviation from both views' otherwise-strict createElement/textContent pattern for dynamic data.**

- Detail: Everywhere else dynamic strings (session names, statuses, errors, descriptions) go through textContent (dashboard.js card(), agent.js renderCards/renderSessionRows/renderAttention), and the file even comments that fmtSpend interpolation at line 609 is allowed only because it is 'our own formatted string, never agent/user data'. The name strip also silently alters display for any name containing &, <, or >. Better shape: put `<span class="agent-ctl-name" data-el="ctlName"></span>` in the template and set `els.ctlName.textContent = name` after the els harvest — same rendering for every valid name, no bespoke sanitizer to reason about.
- Evidence: grep for innerHTML with interpolation across static/: agent.js line 17 is the only site interpolating request-derived data; all other innerHTML uses are constant templates or self-formatted strings (line 609's comment makes the house rule explicit).

## D008/Q025 — `bobi/webapp/static/views/agent.js:189` (bug, high, confirmed, phase 7)

**Markdown link renderer interpolates the URL into a double-quoted href attribute without escaping quotes, allowing agent output to inject event-handler attributes (XSS).**

- Detail: esc() (agent.js:177) escapes only & < >, leaving " intact. The link regex captures the URL as ([^)\s]+) (agent.js:187), which permits " (only ) and whitespace are excluded). safe==url is then dropped into `<a href="${safe}" ...>` (agent.js:189). An agent reply (drivable via prompt injection from any web page / email / tool output the agent reads) containing `[x](https://a"onmouseover="location=document.cookie//)` renders as `<a href="https://a" onmouseover="location=document.cookie//" ...>`. The payload needs no spaces, no parens, and no ) — all of which the URL regex forbids — so it survives, and the page has no CSP (static/index.html sets none), so the handler executes JS in the operator's loopback-dashboard origin on hover. The inline comment at agent.js:174-176 claims 'agent output can never inject markup', which this contradicts.
- Evidence: agent.js:177 esc = s.replace(/&/,&amp;).replace(/</,&lt;).replace(/>/,&gt;) — no quote escaping; agent.js:187 URL group /\[([^\]]+)\]\(([^)\s]+)\)/ allows "; agent.js:189 return `<a href="${safe}" ...`; static/index.html has no Content-Security-Policy; renderMarkdown is applied to agent replies at agent.js:376-377 (body.innerHTML = renderMarkdown(m.text)).
- Cross-pass (Q025): The zero-dependency markdown renderer (esc/mdInline/renderMarkdown, ~60 lines) is defined inside the mountAgent closure of a 937-line view file; it is a clean extraction seam that would meaningfully shrink the god-closure.

## Q074 — `bobi/webapp/static/views/agent.js:322` (dead-code, low, confirmed, phase 7)

**loadMessages wraps `await api(...)` in try/catch, but api() is designed never to reject — its own catch returns {ok:false,status:0,data:null} — so the catch branch is unreachable and no other api() call site in either view wraps it.**

- Detail: shell.js's api() catches fetch failures internally (lines 27-30) and only then touches res.json() inside its own try. Every other call site in agent.js and dashboard.js uses the bare `const { ok, data } = await api(...)` house pattern. The try/catch/finally in loadMessages (319-326) can be flattened to `const result = await api(...); messagesLoading = false;` — behavior identical, one less nesting level, and the misleading suggestion that api() can throw goes away.
- Evidence: Read shell.js api() (lines 16-35): all await points are inside its own try blocks; grep of both view files shows loadMessages is the only api() caller using try/catch.

## D011 — `event-server/core/src/adapters/chat-sdk-slack.ts:82` (bug, high, confirmed, phase 7)

**The blanket `if (innerEvent.subtype) skip` drops every Slack message carrying subtype 'file_share', so file uploads in DMs and thread replies are silently discarded despite the file-attachment extraction contract (#628) below it.**

- Detail: A user DMs the bot a screenshot with a caption. Slack delivers an event_callback whose inner message event has subtype 'file_share' (Slack sets this subtype on every message that shares a file). bridgeSlackWebhook returns {event: null, skip: true} at line 82-84, before the files extraction at lines 152-167 or the slack.dm classification can run — the message and its attachment never become an event and the bot never responds. Channel @mentions with files still work only because app_mention events carry no subtype; the DM and thread-reply file paths are dead.
- Evidence: chat-sdk-slack.ts:82-84 skips on any subtype; lines 152-178 extract innerEvent.files into fields.files/payload.files, which for message-type events can only be reached when subtype is absent. The only test exercising files (event-server/test/chat-sdk-bridge.spec.ts:313-336) builds a synthetic dmPayload with files but no subtype, which does not match real Slack file-share traffic; the only subtype test covers 'message_changed' (spec line 198-204).

## D091 — `event-server/core/src/adapters/discord.ts:91` (duplication, low, confirmed, phase 7)

**The attachment-to-files normalization loop (build Array<Record<string,string>> with per-key presence checks and String() coercion, then mirror into fields.files JSON and payload.files) is duplicated between the Discord and Slack adapters and should share a keyed-extraction helper.**

- Detail: Both adapters implement the same 'files' fields contract that agents consume (fields.files = JSON.stringify(files), payload.files = parsed array, size stringified): discord.ts iterates rawAttachments copying id/filename→name/content_type→mimetype/url/size with `if (a.x) entry.y = String(a.x)`, and chat-sdk-slack.ts iterates rawFiles copying id/name/mimetype/filetype/url_private/url_private_download/size with the identical pattern. A change to the shared contract (e.g. adding a max-files cap or a new common key) must be re-implemented per adapter and can drift; a helper taking a source→dest key map would consolidate both, and the upcoming WhatsApp media support would be a third site.
- Evidence: event-server/core/src/adapters/discord.ts:91-100 and event-server/core/src/adapters/chat-sdk-slack.ts:153-167 — structurally identical per-entry loops feeding the identical `if (files.length > 0) fields.files = JSON.stringify(files)` + payload.files mirroring (discord.ts:127,151; chat-sdk-slack.ts:178,204).

## Q117 — `event-server/core/src/adapters/github.ts:42` (consistency, low, plausible, phase 7)

**Webhook-payload field extraction is handled in two conflicting styles: linear/whatsapp/discord narrow at runtime (asRecord/stringField helpers, explicit typeof checks), while github.ts and chat-sdk-slack.ts use bare `as` casts on untrusted payload fields.**

- Detail: The house pattern for untrusted inbound payloads is explicit runtime narrowing: linear.ts defines asRecord/stringField (lines 3-10) and uses them throughout; whatsapp.ts comments "Runtime type check, not a cast: a numeric `from` would throw…" (line 60) and discord.ts checks `typeof author?.id === "string"` for every id. github.ts instead casts blind (`sender.login as string`, `assignees.map((a) => a.login as string).join(", ")` at line 42, `review.body as string).slice(0, 500)`), and chat-sdk-slack.ts does the same (`(innerEvent.text as string) || ""`). The concrete better shape: move linear.ts's asRecord/stringField into a shared adapters util and use them in github.ts/chat-sdk-slack.ts. For well-formed provider payloads behavior is identical; for malformed ones it converges the adapters on the drop-don't-propagate policy the other three already document.
- Evidence: linear.ts:3-10 (module-local helpers, not exported), whatsapp.ts:60-62 and discord.ts:39-41 (explicit typeof narrowing with comments stating the policy), vs github.ts:24-97 and chat-sdk-slack.ts:99-125 (bare `as` casts on the same class of untrusted fields).

## D047 — `event-server/core/src/circuit-breaker.ts:237` (bug, medium, confirmed, phase 7)

**The tripped-breaker pause buffer is unbounded and only flushed lazily on the next event in the same conversation key, so a hot external loop grows state.paused without limit during cooldown and then floods the entire backlog at once (uncounted toward re-trip); if the key goes quiet after tripping, the buffered events are retained forever and never delivered, contradicting the header's 'buffered, not dropped' / 'Auto-resume after COOLDOWN_MS' contract.**

- Detail: A third-party CI bot posts a comment every 2s on one GitHub issue: breaker trips at 5 events, then buffers ~150 NormalizedEvents (full webhook payloads) per 5-min cooldown, repeatedly — unbounded memory growth in the long-lived local server. When a human finally comments, all buffered loop events are delivered to the agent at once with a fresh window (timestamps reset to []), so the flood is uncounted. Conversely, if the trip itself starved the loop (agent-to-agent case) and no further event ever arrives on that key, the paused events sit in memory forever and are never delivered despite the 'buffered, not dropped' contract.
- Evidence: circuit-breaker.ts:236-239 and 249 push into `state.paused` with no size cap; `resumeState` (260-265) explicitly defers draining to the caller, and the only resume triggers are inside `recordDelivery`/`isBreakerTripped`, both invoked only when a NEW event arrives on the same key. There is no timer and no eviction of the module-level `states` Map (line 180) outside test-only `resetAllBreakers`. Consumer local.ts:309 drains the whole backlog in one shot into the deployment buffer, and drained events bypass `recordDelivery` so none count toward the window.

## Q098 — `event-server/core/src/circuit-breaker.ts:286` (dead-code, low, confirmed, phase 7)

**isBreakerTripped has zero callers anywhere in the repo, including tests.**

- Detail: Its docstring says it is "used after cooldown to decide whether to flush paused events", but no caller exists: local.ts drives the breaker exclusively through recordDelivery/drainPaused/buildLoopDetectedEvent/isExemptFromBreaker, and circuit-breaker.spec.ts never imports it. It also carries its own copy of the cooldown-resume logic (checkCooldown + resumeState), so deleting it removes a second place the resume semantics must be kept in sync. Removal cannot change behavior since nothing invokes it.
- Evidence: grep -rn isBreakerTripped across event-server (src, core/src, test) matches only the definition at circuit-breaker.ts:286; the import lists in local.ts:48-53 and test/circuit-breaker.spec.ts:12-13 do not include it.

## Q101 — `event-server/core/src/core.ts:27` (simplification, low, confirmed, phase 7)

**SlackNormalizationResult.skip is redundant state — it is always exactly `event === null`, and consumers already double-check both.**

- Detail: bridgeSlackWebhook never produces event !== null with skip true, nor event === null with skip false (every return site pairs {event: null, skip: true} or {event, skip: false}). handleSlackWebhook consequently hedges with `if (result.skip || !result.event)`, and slack-socket consumers must reason about an impossible fourth state. Dropping `skip` and narrowing the type to `{ event: NormalizedEvent | null; challenge?: string }` removes the redundant field, turns the hedge into `if (!result.event)`, and cannot change behavior because the two fields are perfectly correlated today. Test assertions on .skip translate mechanically to event-null checks.
- Evidence: All return sites in adapters/chat-sdk-slack.ts (lines 59, 64-69, 74-76, 79, 82-84, 93-95, 112, 118, 184-208) pair skip:true with event:null and skip:false with a non-null event; core.ts:801 checks both (`result.skip || !result.event`); grep shows .skip is otherwise only asserted in tests.

## Q100 — `event-server/core/src/core.ts:980` (missed-reuse, low, confirmed, phase 7)

**createIngestEvent hardcodes its topic spelling as [topic, `ingest/${topic}`] instead of calling sourceQualifiedTopics, even though that helper's own comment claims both paths route through it so the spellings "can never drift".**

- Detail: The comment at core.ts:265-267 states sourceQualifiedTopics is "the single helper both createTopicEvent and createIngestEvent route through, so the two spellings can never drift between the signed-publish and token-ingest paths" — but only createTopicEvent calls it; createIngestEvent (line 980) re-spells the pair inline. They have in fact already drifted in one corner: for a mintable topic beginning "ingest/" (validateIngestTopic reserves only github/linear/slack), the inline version emits a nonsense double-prefixed "ingest/ingest/x" topic that the helper would suppress. Replacing line 980 with `topics: sourceQualifiedTopics(topic, "ingest")` restores the documented invariant and is behavior-identical for every topic not starting with "ingest/".
- Evidence: core.ts:264-274 (helper + the "single helper both … route through" comment), core.ts:978-981 (hardcoded pair with a comment that only says it "mirrors" the fallback), core.ts:1521-1523 (INGEST_RESERVED_SOURCES lacks "ingest", so the drift corner is reachable).

## D089 — `event-server/core/src/core.ts:1314` (bug, low, confirmed, phase 7)

**handleRegisterDeployment trusts `body.subscriptions as string[]` with no shape validation on the unauthenticated MINT path: a string value passes the `!subscriptions?.length` guard and is iterated character-by-character into the subscription index, and an array containing a non-string crashes with an unhandled TypeError (500) inside unauthorizedGlobalTopics.**

- Detail: POST /register with {"name":"x","subscriptions":"github:foo"} mints a bubble and registers ten one-character subscriptions ("g","i","t",...) instead of rejecting with 400; {"name":"x","subscriptions":[42]} throws `key.startsWith is not a function` and surfaces as a transport 500 (after persisting an orphaned bubble record) instead of a 400.
- Evidence: core.ts:1314 `const subscriptions = body.subscriptions as string[];`, guard at 1316 only checks truthiness and `.length` (both satisfied by a non-empty string); 1379-1381 `for (const sub of subscriptions) await storage.addSubscription(namespaceSubKey(bubble.id, sub), ...)` iterates a string per character; for `subscriptions: [42]`, unauthorizedGlobalTopics → isGlobalTopic (core.ts:344-346) calls `key.startsWith` on a number and throws, escaping the handler. MINT requires no signing headers (core.ts:1328-1335), so the surface is unauthenticated.

## D046 — `event-server/core/src/core.ts:2211` (bug, medium, confirmed, phase 7)

**In handleChannelsSend, a file reply resolving a placeholder (edit_ref + mode update/final) on a channel without edit support silently discards the caller's text: the handler 400s if text is missing ('text required when edit_ref is combined with files'), but then only performs the placeholder edit when `adapter.update && caps.edit`, and calls `adapter.uploadFiles(botToken, conv, files)` with no comment either way.**

- Detail: Agent posts a WhatsApp placeholder message, then finalizes with {conversation: whatsapp:..., mode: "final", edit_ref: "wamid...", text: "here's the report", files: [pdf]}. The handler validates text as required, skips the edit (WhatsApp caps.edit=false), uploads the file with no caption/comment, and returns 200 ok — the user receives the file but the required explanatory text is never delivered anywhere.
- Evidence: core.ts:2201-2212: `if (editRef && (mode === "update" || mode === "final")) { if (!outText) return {status:400, ..."text required when edit_ref is combined with files"}; if (adapter.update && caps.edit) { ...edit... } result = await adapter.uploadFiles(botToken, conv, files); }`. WhatsApp declares `edit: false, files: true` (channels.ts:480-484), and channels.ts:7-9 documents the degradation contract as 'an `update` on a channel without edit support becomes a follow-up post' — which the text-only path honors (core.ts:2220-2223) but this path does not.

## Q099 — `event-server/core/src/core.ts:2381` (missed-reuse, low, confirmed, phase 7)

**handleSlackWorkspaceRegister hand-rolls three raw fetch() calls to the Slack Web API (auth.test twice, bots.info once) that the already-imported Chat SDK helper callSlackApi covers.**

- Detail: channels.ts already imports and uses callSlackApi(method, body, {apiUrl, token}) from @chat-adapter/slack/api for exactly this kind of call (slackAdapter.typing, fetchConversation), and core.ts already imports slackApiUrl from channels. The three inline `fetch(`${slackApiUrl()}auth.test`...)` / `bots.info?...` blocks at 2381-2417 re-solve URL joining, the Bearer header, and JSON parsing with `as` casts, in near-triplicate. Replacing each with callSlackApi keeps behavior: the signed branch's failure handling (non-ok → 403) maps to the existing try/catch since the SDK signals failure via SlackApiError, and the best-effort branches already swallow errors. This deletes ~25 lines and removes a hand-rolled Slack client from a module that has a real one in scope.
- Evidence: core.ts:2381-2417 (three fetch blocks); channels.ts:17-25 imports callSlackApi and uses it at 369 and 383; node_modules/@chat-adapter/slack/dist/api.d.ts:91 shows callSlackApi(method, body, {apiUrl, token}) with SlackApiError for failures.

## D048 — `event-server/core/src/core.ts:2445` (bug, medium, confirmed, phase 7)

**handleSlackWorkspaceRegister's global workspace record update is a non-atomic read-merge-write (getSlackWorkspace then putSlackWorkspace of mergeBot(existing)), so two bots registering the same workspace concurrently race and the last write silently drops the other app's bots-map entry — including its per-app signing_secret, whose loss 401s that app's inbound events (the exact incident mergeBot was written to prevent).**

- Detail: Two agents with different Slack apps in one workspace restart at the same time (e.g. a fleet roll): both read the workspace record concurrently, each merges only its own bot, and the second put overwrites the first — app A's bots[appId] entry (token + signing_secret) reverts to the pre-registration state. App A's subsequent inbound events verify against the wrong secret and are rejected 401, dropping user mentions until A happens to re-register.
- Evidence: core.ts:2445-2446: `const globalExisting = await storage.getSlackWorkspace(workspaceId); await storage.putSlackWorkspace(workspaceId, mergeBot(globalExisting));` — mergeBot (2411-2441) merges only into the snapshot read before the await; no storage adapter offers compare-and-swap. Inbound verification reads this global record per team_id (slackSigningSecretFor, core.ts:207-215) and falls back to the global secret when the app's entry is missing, which returns INVALID_SIGNATURE for an app signing with its own secret (core.ts:1065-1078).

## Q116 — `event-server/core/src/gateway/discord.ts:203` (over-abstraction, low, plausible, phase 7)

**DiscordGatewaySession.onTimer takes a parameter typed as the literal "heartbeat" and guards against other values — generality for timer kinds that don't exist.**

- Detail: The parameter type is the single literal "heartbeat", so the `if (kind !== "heartbeat") return []` guard is unreachable for any type-checked caller (the only caller, discord-gateway-local.ts:277, passes the literal). The speculative multi-timer surface earns nothing today; `onTimer(): GatewayAction[]` (or onHeartbeatTimer) with the guard deleted is smaller and behavior-identical. If a second timer kind ever appears, widening the signature then is trivial.
- Evidence: gateway/discord.ts:203-211 (literal-typed param + dead guard); grep shows the sole caller at discord-gateway-local.ts:277 passes "heartbeat".

## D090 — `event-server/src/local.ts:293` (duplication, low, confirmed, phase 7)

**storage.deliver() contains three near-identical copies of the seq-assign / eventBuffer-push / trim / JSON-serialize / broadcast-to-websockets block (loop-detected fan-out, drained-paused replay, and main delivery).**

- Detail: Three sites must stay in lockstep: lines 293-302 (loop event: lseq/lseqEvent/trim/lmsg/ws loop), lines 313-320 (drained paused events: pseq/pseqEvent/trim/pmsg/ws loop), and lines 324-339 (main event: seq/seqEvent/trim/msg/ws loop). Each repeats the identical `nextSeq++`, `eventBuffer.push`, `length >= 2 * MAX_BUFFER` trim, `JSON.stringify({type:"event",data})`, and try-send/delete-on-throw logic. A future fix to one copy (e.g. buffer-trim policy or dead-socket cleanup) that misses the other two silently diverges delivery behavior between normal, drained, and loop-event paths. Consolidatable into one `pushAndSend(dep, event)` helper.
- Evidence: event-server/src/local.ts:293-302, 313-320, and 324-339 — the three blocks differ only in variable prefixes (l/p/none) and the target deployment record.

## D049 — `event-server/src/local.ts:402` (bug, medium, confirmed, phase 7)

**readBody buffers the entire request body into memory with no size cap, so the ingest route's 256KB limit (and every other route) is enforced only after an arbitrarily large body has already been fully buffered.**

- Detail: On a self-hosted server bound non-loopback (BOBI_ES_BIND, the documented docs/SELF_HOSTED_EVENT_SERVER.md deployment), an unauthenticated client POSTs a multi-gigabyte body to any matched route (e.g. /webhooks/ingest/x — no valid token needed, the body is read at local.ts:510 before verification). readBody accumulates all chunks in memory; core's maxBodyBytes check (core.ts:1265) runs only after the full read, so the 413 arrives after the memory is already spent. A handful of concurrent large POSTs OOM-kills the event server, dropping all live agent WebSocket deliveries.
- Evidence: local.ts:402-409 readBody concatenates all chunks unconditionally; local.ts:510 `const body = await readBody(req)` runs before handleWebhookRequest; core.ts:1265-1267 shows the 413 size gate only executes against the already-fully-read rawBody. No Content-Length check or stream abort exists anywhere in local.ts.

## Q115 — `event-server/src/local.ts:838` (missed-reuse, low, plausible, phase 7)

**evictStaleDeployments re-implements subscription-index and deployment removal that the same file's storage.removeSubscription and storage.removeDeployment already implement.**

- Detail: The eviction sweep hand-rolls `set.delete(id); if (set.size === 0) subscriptionIndex.delete(nsKey)` and `deployments.delete(id); apiKeyIndex.delete(dep.apiKey)` — byte-for-byte the bodies of storage.removeSubscription (lines 265-271) and storage.removeDeployment (249-258) defined 570 lines above. Calling those two storage methods instead preserves behavior exactly: removeDeployment's extra websocket-close loop is a no-op here because eviction only fires when disconnectedAt is non-null, which by construction means dep.websockets is empty. Reuse keeps the deregister path (handleDeregisterDeployment → same storage methods) and the eviction path guaranteed-identical instead of coincidentally-identical.
- Evidence: local.ts:838-847 duplicates local.ts:265-271 (removeSubscription) and 249-258 (removeDeployment); disconnectedAt is only set when dep.websockets.size === 0 (line 819-821) and reset to null on connect (line 804), so the ws-close difference is unreachable.

## D113 — `CLAUDE.md:70` (doc-drift, low, confirmed, phase 8)

**'(see Bug fixes above)' references a 'Bug fixes' section that does not exist anywhere in the file (nor in the identical root AGENTS.md).**

- Detail: Development Lifecycle step 3 says 'Reproduce with a failing test first (see Bug fixes above)', but the file has no 'Bug fixes' heading or content above — lines 44-45 explicitly say bug-fix standards live in ~/AGENTS.md instead. A reader (or agent) following the pointer finds nothing; the reference survived the move of those standards out of this file. Same dangling reference at AGENTS.md:70 (byte-identical mirror).
- Evidence: CLAUDE.md:44-45 'General coding, bug-fix, testing ... standards live in ~/AGENTS.md'; grep for 'Bug fixes' in CLAUDE.md matches only line 70's parenthetical — no such section exists

## D106 — `README.md:295` (doc-drift, low, confirmed, phase 8)

**The 'Under the hood' command block shows `bobi agent <name> subagents launch --role <role> --task "context"` without the -w/--workflow option, which the parser requires.**

- Detail: A user copy-pastes the snippet (substituting name/role/task); click aborts with "Missing option '--workflow' / '-w'" instead of launching a sub-agent. The Quick Start example on README.md:126 correctly includes `-w adhoc`, so only this snippet is stale.
- Evidence: bobi/cli.py:2758-2759 `@subagents.command("launch")` / `@click.option("--workflow", "-w", required=True, ...)` — the option is required with no default.

## D121 — `agents/eng-team/README.md:20` (doc-drift, low, confirmed, phase 8)

**README's package layout listing omits shipped files: workflows/stall-recovery.yaml and tools/image-gen.md.**

- Detail: The Layout block lists six workflows (issue-lifecycle, pr-feedback, pr-closed, merge-conflict, build-failure, adhoc) and two tool guides (github.md, slack.md), but the package actually ships workflows/stall-recovery.yaml and tools/image-gen.md as well; anyone auditing or overlaying the pack from the README sees an incomplete surface.
- Evidence: agents/eng-team/workflows/stall-recovery.yaml and agents/eng-team/tools/image-gen.md exist on disk; neither appears in the README.md Layout tree.

## D120 — `agents/eng-team/roles/engineer/ROLE.md:148` (doc-drift, low, confirmed, phase 8)

**The reusable base eng-team engineer prompt embeds bobi-agent-specific test-setup instructions ("For this repo, broad non-integration tests use `pip install -e \".[dev,kb]\"`") that are meaningless for any other repo the distributed team works in.**

- Detail: A consumer derives a house team via `from: eng-team` for an unrelated repo -> every engineer worker is instructed that "this repo" uses the `.[dev,kb]` extras, referencing bobi-agent's own pyproject extras; the instruction was written for bobi-agent's house use and leaked into the generic base package (agent.md line 6-8 markets it as the reusable, tool-agnostic base).
- Evidence: agents/eng-team/roles/engineer/ROLE.md:145-152 vs agents/eng-team/agent.md:5-8 ("This is the **reusable base**... Derive a house team with `from: eng-team`"); `[dev,kb]` extras exist only in bobi-agent's own CLAUDE.md/pyproject.

## D122 — `agents/personal-assistant/agent.md:87` (doc-drift, low, confirmed, phase 8)

**Setup docs say install prompts for `SLACK_BOT_TOKEN` and `VENN_API_KEY`, omitting `SLACK_SIGNING_SECRET` which agent.yaml also declares as a required ${VAR} credential.**

- Detail: A user following agent.md prepares only the two listed secrets; install/startup also requires SLACK_SIGNING_SECRET (agent.yaml credentials block, needed for the event server to verify this app's inbound events in shared workspaces), so validate fails or event verification 401s with a secret the doc never mentioned.
- Evidence: agents/personal-assistant/agent.yaml:36-42 declares credentials bot_token: ${SLACK_BOT_TOKEN} AND signing_secret: ${SLACK_SIGNING_SECRET}; agent.md line 86-88 lists only SLACK_BOT_TOKEN and VENN_API_KEY.

## D082 — `bobi/sdk.py:5` (doc-drift, low, confirmed, phase 8)

**sdk.py module docstring claims every session 'wraps a ClaudeSDKClient', contradicting the post-#485 brain-agnostic session model.**

- Detail: Doc claims 'Persistent tracking for all Claude Code sessions ... Each session wraps a ClaudeSDKClient with connect/resume/query/disconnect'; reality: sessions wrap provider-agnostic BrainSession adapters — bobi/session.py:347 builds via self._brain.make_session(), and codex (_CodexSession, bobi/brain/codex.py:180) and stub sessions involve no ClaudeSDKClient at all. sdk.py itself contains no client wrapping (only the string in its own docstring). A reader extending the registry for a non-Claude brain is misdirected.
- Evidence: bobi/sdk.py:1-12 docstring vs bobi/session.py:347 (`self._brain.make_session(...)`) and bobi/brain/codex.py:180-259 (per-turn `codex exec` subprocess session, no ClaudeSDKClient); `grep ClaudeSDKClient bobi/sdk.py` matches only the docstring line.

## D079 — `bobi/slack_manifest.py:23` (doc-drift, low, confirmed, phase 8)

**Module comment points at event-server/src/index.ts for the Slack webhook route, but that file does not exist.**

- Detail: The comment claims 'The single webhook path the event server exposes for Slack (see event-server/src/index.ts)'. event-server/src/ contains only local.ts and discord-gateway-local.ts; no index.ts exists anywhere in event-server/. The /webhooks/<source> route actually lives in event-server/core/src/core.ts (route -> verifier -> normalizer pipeline around line 813). A developer following the reference to verify WEBHOOK_PATH stays in sync lands on a nonexistent file.
- Evidence: slack_manifest.py:22-24 references event-server/src/index.ts; `find event-server -name index.ts` returns nothing, and grep shows the webhook routing documented in event-server/core/src/core.ts:813 ('route (/webhooks/<source>) -> verifier -> normalizer -> deliver()').

## D110 — `docs/BUILDING_AGENT_TEAMS.md:149` (doc-drift, low, confirmed, phase 8)

**The native-service list ('github', 'slack', 'linear') omits whatsapp and discord, which are fully registered native ingestion adapters.**

- Detail: A team author wiring WhatsApp or Discord follows the 'Connecting services' cascade (native -> Venn -> MCP) and, since the doc says native is only github/slack/linear (also in the agent.yaml example comment at line 101), routes them through Venn or a custom guide instead of declaring the supported native service with events: true as skills/whatsapp-setup.md:83-88 documents.
- Evidence: bobi/setup/services.py:119-260 _NATIVE includes key="whatsapp" (line 184) and key="discord" (line 220) with kind="native"; bobi/events/adapters.py:348-349 register("whatsapp", _detect_whatsapp) / register("discord", _detect_discord); skills/whatsapp-setup.md and skills/discord-setup.md exist

## D111 — `docs/BUILDING_AGENT_TEAMS.md:246` (doc-drift, low, confirmed, phase 8)

**The weekly-job worked example claims eng-team ships context/prep-doc.md 'wired from the director role's monitor/prep.weekly_due handler', but no such monitor or handler exists in the pack.**

- Detail: An author told to 'Copy it as a template for your own weekly jobs' opens agents/eng-team looking for the prep.weekly_due monitor and the routing line in the director prompt; neither exists — grep for 'prep.weekly_due' and 'weekly' across agents/eng-team matches nothing, monitors/defaults.yaml has no weekly prep entry, and director ROLE.md only handles monitor/pr.conflict_detected (line 196) and monitor/status.roundup_due (line 223). Only the context file half of the example exists.
- Evidence: agents/eng-team/context/prep-doc.md exists, but grep -rn 'prep.weekly_due' agents/eng-team/ returns nothing; agents/eng-team/roles/director/ROLE.md:196,223 are the only monitor/* handlers

## D112 — `docs/BUILDING_AGENT_TEAMS.md:271` (doc-drift, low, confirmed, phase 8)

**The raw-API tool-guide exception cites 'agents/eng-team/tools/linear.md', which does not exist — the Linear guide moved to context/.**

- Detail: The doc says raw REST/GraphQL mechanics belong in a tool guide and points to agents/eng-team/tools/linear.md as the exemplar; that path is gone (agents/eng-team/tools/ contains only github.md, image-gen.md, slack.md), and the file now lives at agents/eng-team/context/linear.md — i.e. in the layout the doc says NOT to use for this case — so the illustrated pattern can't be found where cited.
- Evidence: ls agents/eng-team/tools/ -> github.md, image-gen.md, slack.md (no linear.md); agents/eng-team/context/linear.md exists ('# Linear Context')

## D057 — `docs/BUILDING_AGENT_TEAMS.md:298` (doc-drift, medium, confirmed, phase 8)

**The entire 'Decision log (memory)' section documents the retired per-session INDEX.md decision log and an agent-curated memory contract that the codebase has replaced with the sleep-cycle-owned long_term_memory.md.**

- Detail: Doc claims each agent has a decision log at run/state/memory/<session-name>/INDEX.md that 'the agent curates', that prompts/base.md instructs every agent to 'write a note', 'keep the YAML current-state block accurate', and 'prune' entries, and that 'bobi agent <name> doctor checks for agents with empty decision logs'. Reality: memory is a single team-scoped run/state/long_term_memory.md whose ONLY writer is the sleep-cycle monitor; base.md explicitly tells agents 'Do not edit <run>/state/long_term_memory.md yourself' and 'There is no per-session journal'; doctor checks long_term_memory.md presence/cap/foreign-writes, not empty decision logs. A team author following this guide designs role prompts around a write-your-own-notes contract that agents are now instructed NOT to follow.
- Evidence: bobi/memory.py:1-13 ('The older per-session decision log (memory/<session>/INDEX.md ...) is being replaced'); bobi/prompts/base.md:140-186 ('single writer' = sleep-cycle monitor, 'Do not edit ... yourself', 'There is no per-session journal'); bobi/doctor.py:432 _check_long_term_memory() — no empty-decision-log check exists

## D108 — `docs/EVENT_SERVER.md:472` (doc-drift, low, confirmed, phase 8)

**Public-server prerequisites say to "set all three provider webhook secrets (WEBHOOK_SECRET, SLACK_SIGNING_SECRET, LINEAR_WEBHOOK_SECRET) so every inbound route verifies", but the code has five provider verification secrets — the WhatsApp app secret and verify token are omitted.**

- Detail: Doc claims three secrets make every inbound route verify. In reality the WebhookSecrets interface (core.ts:838-846) also carries whatsapp + whatsappVerifyToken, and a WhatsApp-using public server configured per this checklist would 401 every inbound WhatsApp event (whatsapp verify slot fails closed on a missing secret, core.ts:1107-1111) and 403 Meta's GET handshake (core.ts:1089-1098) — i.e. WhatsApp inbound is broken, not verified. SECURITY.md:125-127 already gives the corrected full list (WEBHOOK_SECRET, SLACK_SIGNING_SECRET, LINEAR_WEBHOOK_SECRET, WHATSAPP_APP_SECRET plus WHATSAPP_VERIFY_TOKEN), so EVENT_SERVER.md contradicts both the code and its sibling doc.
- Evidence: event-server/core/src/core.ts:838-846 (WebhookSecrets includes whatsapp and whatsappVerifyToken) and core.ts:1100-1121 (whatsapp verify rejects with 401 when secret unset, unlike github/slack/linear unverifiedAdmission); docs/SECURITY.md:125-127 lists the five-secret set

## D054 — `docs/EVENT_SERVER.md:485` (doc-drift, medium, confirmed, phase 8)

**EVENT_SERVER.md documents the Cloudflare Worker runtime files (event-server/src/index.ts, event-server/src/deployment-session.ts, wrangler.jsonc) as living in this repo, but they were moved to the private bobi-deploy repo and no longer exist here.**

- Detail: Doc claims (lines 45-54: "event-server/src/index.ts is the Worker entry ... Deployed with wrangler (wrangler.jsonc)"; line 479: "both runtimes under event-server/src/ consume it by package name"; Key files lines 485+487 list index.ts and deployment-session.ts) vs reality: event-server/src/ contains only local.ts and discord-gateway-local.ts. A contributor or self-hoster following the doc to the Worker entry, the DO implementation, or the wrangler config finds nothing; the doc also contradicts the repo convention that deployment lives in bobi-deploy (which SECURITY.md line 16 already reflects).
- Evidence: ls event-server/src/ -> only discord-gateway-local.ts and local.ts; no wrangler.jsonc or deployment-session.ts anywhere under event-server/ (find returned nothing outside node_modules); git log --diff-filter=D shows commit 9eb9d84 "repo split: move deploy/fleet/worker IP to the private bobi-deploy repo (#713)" deleted event-server/src/index.ts, event-server/src/deployment-session.ts, and event-server/wrangler.jsonc

## D055 — `docs/MONITORS.md:61` (doc-drift, medium, confirmed, phase 8)

**Doc says monitor configuration 'merges in tiers, later wins by name' with agent.yaml's monitors: key (tier 3) overriding run/package/monitors.yaml (tier 2), but the registry appends records from both files without by-name dedup, so an override never replaces the earlier record.**

- Detail: A team ships monitor X in run/package/monitors.yaml; the user overrides its `command:` under the monitors: key in run/package/agent.yaml per the doc. Registry._load appends both as separate project monitors sharing state_key. Each tick the monitors.yaml record is iterated first, runs, and updates the shared last_run, so the agent.yaml override is never due — the old command keeps running and the documented 'later wins' override silently never takes effect.
- Evidence: bobi/monitors/registry.py:78-92 — project_sources = [monitors.yaml, agent.yaml] and every enabled record is appended to self.project_monitors with no by-name replacement between the two files; schema.py state_key is identical for both (name@project), so they share one scheduler state entry.

## D014 — `docs/MONITORS.md:68` (doc-drift, high, confirmed, phase 8)

**Doc says 'Set enabled: false on a name to switch off a default', but a runtime-tier disable (run/package/monitors.yaml or the monitors: key in agent.yaml, including `bobi agent <name> monitors pause`) only empties projects_for(); the default monitor stays in effective_monitors() and every flavor still runs.**

- Detail: User disables the shipped sleep-cycle default by adding `- {name: sleep-cycle, enabled: false}` to run/package/monitors.yaml (or via `monitors pause`, which writes exactly that). Registry records an opt-out but globals keep enabled=True, so effective_monitors() still returns it; run_monitor's sleep_cycle branch calls _spawn_sleep_cycle with projects=[] and _project_root([]) falls back to the scheduler's bound root — the sleep cycle keeps running (and paying for LLM calls) every 6h despite the documented disable. notify/command/check flavors likewise never consult projects_for before running.
- Evidence: bobi/monitors/registry.py:89-91 (enabled:false only feeds opt_outs) + :96-100 (effective_monitors returns globals unfiltered by opt-outs); bobi/monitors/scheduler.py:735-762 (run_monitor runs notify/command/sleep_cycle without checking projects_for) and :1067-1072 (_project_root falls back to self._project_path when projects is empty).

## D013 — `docs/MONITORS.md:297` (doc-drift, high, confirmed, phase 8)

**Doc claims a self-healed script that widens its capability envelope 're-enters review even in auto mode' so self-healing 'cannot silently widen what the cron is allowed to touch', but in the default auto mode the envelope is never consulted and the new-capability script is auto-pinned.**

- Detail: Monitor runs with default approval=auto; the pinned script breaks and the self-heal agent regenerates one that adds a new binary or a new venn service:tool (a capability change). Per the doc it should queue to pending/ for human review; in code _should_pin returns True unconditionally for auto, so the widened script is pinned and runs unattended with the manager's secret env — the documented security guarantee does not exist in auto mode.
- Evidence: bobi/monitors/script_cache_checks.py:1018-1028 — `def _should_pin(...): if approval == "auto": return True` (envelope comparison only happens under review mode); test suite only covers the review-mode envelope path (tests/test_script_cache_runner.py:300). Contradicts docs/MONITORS.md:295-298 and the lifecycle diagram line 236 ('auto mode / inside known envelope → pin it').

## D107 — `docs/OVERVIEW.md:46` (doc-drift, low, confirmed, phase 8)

**OVERVIEW states every tool dependency is "declared in the team's agent.yaml under tool_library:", but the eng-team example it uses declares its only dependency (gh) under `requires:` with a `check:` key and has no tool_library block.**

- Detail: A reader opens agents/eng-team/agent.yaml (the doc's running example, cited again at lines 62-66 for the gh --version dependency check) expecting a `tool_library:` entry with a `success:` condition; instead they find a `requires:` list using `check:`/`fix:` keys, and can't map the doc's described model onto the flagship team's actual config.
- Evidence: agents/eng-team/agent.yaml:37-42 `requires: - name: gh ... check: "command -v gh >/dev/null 2>&1 && gh --version >/dev/null 2>&1"` (no tool_library key in the file); bobi/tool_library.py:211 shows tool_library entries are expanded INTO requires entries (`entry["check"] = dep.success`), i.e. requires: is a separate first-class authoring surface eng-team uses directly.

## D053 — `docs/QUICKSTART.md:123` (doc-drift, medium, confirmed, phase 8)

**Quickstart Step 3a claims the setup client defaults the team library to ~/bobi-agents/, but the actual default is $BOBI_HOME/agents (~/.bobi/agents/<name>/src).**

- Detail: A new user finishes `bobi setup my-agent` and goes looking for the created team files in ~/bobi-agents/ per the doc; the directory does not exist. The files are at ~/.bobi/agents/my-agent/src, which the same doc's Step 2 ("Everything Bobi creates lives under ~/.bobi") and Step 5 (`--team ~/.bobi/agents/my-agent/src`) correctly reference, so the doc contradicts both the code and itself.
- Evidence: bobi/setup/webui/server.py:167-168 `home = (home_root or paths.home_dir()).resolve(); library = home / "agents"` and :277-279 `default_source = paths.agent_source_dir(state.team_name or "new-agent") ... "default_location": str(default_source)`; bobi/paths.py:66-68 home_dir() = $BOBI_HOME else ~/.bobi, :83-84 agent_source_dir = agents/<name>/src. `grep -rn "bobi-agents" bobi/` returns zero hits.

## D109 — `docs/RELEASE_RUNBOOK.md:107` (doc-drift, low, confirmed, phase 8)

**The repo-split edit truncated the GHCR package-visibility instruction, leaving an incoherent orphaned sentence fragment and losing the actual operational step.**

- Detail: Doc reads: 'If PyPI was just published, allow a short propagation delay before installing the new version from another repo. (github.com/orgs/moda-labs/packages) so consumers can pull without a token; visibility persists across releases.' The parenthetical fragment is grammatically orphaned. Commit 9eb9d84 deleted the leading sentences 'One-time setup (first release only): the first push creates the GHCR package as private. Make it public in the package settings' (present in 9eb9d84^:docs/RELEASE_RUNBOOK.md:114-117). A release operator hitting a private GHCR image has no instruction for the make-public step the subsequent anonymous docker-pull spot-check (lines 112-119) depends on.
- Evidence: git show 9eb9d84^:docs/RELEASE_RUNBOOK.md lines 114-117 contain the deleted lead-in; current docs/RELEASE_RUNBOOK.md:107-110 retains only the tail fragment

## D030 — `docs/WORKFLOW_ENGINE.md:97` (doc-drift, medium, confirmed, phase 8)

**Doc claims a step's `timeout` is 'the declared deadline carried into the registry for the reconciler's dead-man check', but StepDef.timeout is parsed and never read anywhere; the registry gets only the run-level timeout.**

- Detail: An author sets timeout: 300 on a setup step (as in the doc's own example at line 67) or timeout: 86400 on an await step (doc line 192) expecting per-step deadlines. In reality the SessionEntry timeout is run_workflow's function parameter (default 3600, orchestrator.py:198) for the whole run; the step values are dead config — a hung 300s step is not flagged until the 3600s run deadline.
- Evidence: schema.py:97 parses timeout into StepDef; repo-wide grep shows no reader of step.timeout — the only .timeout consumers are bobi/reconcile.py:167-173 (entry.timeout, the SessionEntry field) which run_workflow fills from its own timeout param at orchestrator.py:198-199.

## D056 — `docs/WORKFLOW_ENGINE.md:152` (doc-drift, medium, confirmed, phase 8)

**Doc claims that when a step changes agent: the engine falls back to a fresh session because 'a new agent never inherits another agent's transcript', but the isolation only happens when the step ALSO changes model or effort — an agent change at identical dials continues the same session.**

- Detail: Workflow step 1 runs agent: engineer, step 2 runs agent: reviewer with no model/effort override (both resolve to the same model). The switch branch is guarded by `if step_model != current_model or step_effort != current_effort`, so it is never entered; the reviewer step is injected into the engineer's live session and inherits its full transcript — the exact contamination (reviewer biased by builder reasoning) the doc says cannot happen.
- Evidence: bobi/workflow/orchestrator.py:748-764 — the model/effort inequality guard, and the in-code comment: 'an agent change with identical dials never enters the branch - a pre-existing gap in that isolation, not one this condition can close', directly contradicting docs/WORKFLOW_ENGINE.md:150-156's unconditional claim.

## D072 — `docs/WORKFLOW_ENGINE.md:198` (doc-drift, low, confirmed, phase 8)

**Doc says notification failures are always non-fatal and the workflow continues, but the engine deliberately fails the run when an undeliverable notify step immediately precedes an await step.**

- Detail: Doc (lines 198-199): 'Notification failures are non-fatal: they are logged and the workflow continues.' Code: if the notify outcome is undelivered (no token, no channel, or Slack post failure) and the next step has await_event, the run fails with 'workflow.notify_undeliverable: ...; refusing to arm await step ...' (orchestrator.py:686-700). An author relying on the doc would expect an approval-request notify failure to be survivable; instead the whole run fails. The code behavior is the sensible one (never suspend waiting for an approval nobody was asked for), so the doc is stale.
- Evidence: orchestrator.py:679-700: '_execute_notify_step' outcome checked against next_step.await_event; run_failed set and _emit_step_failed called — directly contradicting the doc's blanket non-fatal claim.

## D006 — `docs/WORKFLOW_ENGINE.md:302` (doc-drift, high, confirmed, phase 8)

**Doc claims the manager calls try_resume_for_event when an event arrives, but nothing in the repo calls it — suspended workflows only resume via the manual CLI command.**

- Detail: Doc says: 'On resume, the manager calls try_resume_for_event(event_type, run_key, event, repo) when an event arrives.' In reality a workflow that suspends on await: approval sits in 'waiting' forever when the approval event arrives; only a human running 'bobi agent <name> workflows resume <run_id>' (cli.py:2219) resumes it. The advertised automatic event-driven resume does not exist.
- Evidence: grep across the whole repo: try_resume_for_event appears only at its definition (bobi/workflow/orchestrator.py:66). bobi/session.py, bobi/ingress.py, bobi/inbox.py, bobi/events contain no workflow references; bobi/prompts/base.md never mentions workflow resume. The only resume caller is the CLI command at bobi/cli.py:2233/2254.

## D116 — `docs/specs/747-sleep-cycle-memory-cap.md:5` (doc-drift, low, confirmed, phase 8)

**Completed pre-implementation spec for shipped issue #747 lingers in docs/, with a Problem section describing pre-fix behavior as current reality, contradicting both the code and the repo policy that design docs live in GitHub issues.**

- Detail: Doc claims 'the cap is currently enforced only by prompt instruction' and that truncation is 'lossy and quiet'; in reality the cap is enforced deterministically and truncation logs a warning with section-aware trimming (shipped in PR #748, commit dd3a575). A reader consulting docs/specs/ would believe the memory-cap enforcement is still an unimplemented plan. CLAUDE.md dev-lifecycle step 1 states 'Design docs live in issues, not in docs/'.
- Evidence: bobi/memory.py:142-145 (oversized-content warning log + _section_aware_truncate), bobi/monitors/scheduler.py post-run artifact validation; file added by implementation commit dd3a575 '[#747] fix: enforce sleep-cycle memory cap (#748)' and never updated since

## D117 — `docs/specs/751-install-write-guard.md:21` (doc-drift, low, confirmed, phase 8)

**Completed pre-implementation spec for shipped issue #751 lingers in docs/, describing the runtime write guard as nonexistent when it has been shipped (and subsequently patched), contradicting repo policy that design docs live in GitHub issues.**

- Detail: Doc claims Bobi 'does not make those images non-writable and does not cover the Bobi framework wheel, its .dist-info metadata' — but bobi/runtime_guard.py implements exactly the proposed API and is wired into brain launch paths (PR #752, commit f63314c; later EPERM fix in PR #774). A reader would conclude the write guard is an unbuilt proposal. CLAUDE.md states design docs belong in issues, not docs/.
- Evidence: bobi/runtime_guard.py:217 check_runtime_write_policy, :239 with_mutable_runtime_package, :250 prepare_brain_runtime — the exact function names the spec proposes as future work; file added by implementation commit f63314c and never updated

## D118 — `docs/specs/issue-753-subagents-wait-max-turns.md:5` (doc-drift, low, confirmed, phase 8)

**Completed pre-implementation spec for shipped issue #753 lingers in docs/, describing the old broken --wait/check-harness behavior in the present tense, contradicting current CLI code and the repo policy that design docs live in GitHub issues.**

- Detail: Doc claims 'the current implementation routes --wait through the monitoring-check harness' and that '--agent-wait ... is hidden from help output'; in reality --wait calls _run_agent_wait(), --as-check exists as the explicit check-mode flag with usage-error guards, and --agent-wait was removed (PR #755, commit 3290abc). The spec also ends with 'Open the implementation PR only after this spec is approved' — a stale instruction for work merged in July. A reader would misdiagnose current CLI behavior from this doc.
- Evidence: bobi/cli.py:2765 --as-check option, :2823-2836 as_check/wait/post-event usage-error guards, :2849 _run_agent_wait dispatch; no 'agent-wait' string remains in bobi/cli.py; file added by implementation commit 3290abc and never updated

## D114 — `skills/discord-setup.md:100` (doc-drift, low, confirmed, phase 8)

**Doc claims `bobi agent <name> event-server status` shows a `discord_gateway` block with connection state, but the CLI command prints only Mode and Deployments.**

- Detail: A user testing their Discord bot runs `bobi agent eng event-server status` to check the Gateway connection state as instructed (also referenced at line 111 'Bobi checks /health for a connected discord_gateway entry'); the command outputs only 'Mode:' and 'Deployments:' (bobi/cli.py:2746-2752), so they can't see the discord_gateway block and may conclude the Gateway isn't running. Only a direct GET /health returns it.
- Evidence: bobi/cli.py:2748-2750 prints only mode and deployments from the health payload; the discord_gateway field exists solely in the raw /health response (event-server/src/local.ts:483 `...(discordHealth.length > 0 ? { discord_gateway: discordHealth } : {})`).

## D059 — `skills/linear-setup.md:76` (doc-drift, medium, confirmed, phase 8)

**Section 5 ('Label issues for automation') and two troubleshooting rows describe a built-in Linear dispatcher (trigger_labels, 'Dispatch only picks up issues in Triage or Unstarted states', 'moves the issue to In Progress') that no code in this repo implements.**

- Detail: A user creates an 'agent' label and sets issues to Triage/Unstarted expecting bobi to pick them up and move them to In Progress; the framework has no label- or state-based Linear dispatch, so nothing happens and the troubleshooting rows (lines 96, 100) send them chasing a nonexistent mechanism. Whether/how issues are picked up is entirely team-pack prompt behavior, and the in-repo eng-team pack uses different states (Todo/In Progress/Blocked/In Review/Done, agents/eng-team/context/linear.md:7) with the manager moving tickets, not a dispatcher gated on Triage/Unstarted.
- Evidence: grep -rn 'trigger_labels' over bobi/ and event-server/ returns nothing (only agents/dogfood-content-review/agent.yaml:39, a github-issues task_tracking block read by prompts, not framework code); grep -rn 'Triage|Unstarted' over bobi/ finds no dispatch logic; bobi/events/ and bobi/workflow/ contain no label filtering.

## D115 — `skills/slack-setup.md:56` (doc-drift, low, confirmed, phase 8)

**The scope list presented as the complete set the manifest pins ('The manifest pins exactly the scopes...' + table + 'Plus chat:write, files:read/files:write, and users:read') omits three scopes the manifest actually requests: channels:read, groups:read, and im:write.**

- Detail: A user auditing the app's permissions against this reference page (or hand-maintaining an app to match) concludes the bot needs 10 scopes; the generated manifest requests 13, including im:write (proactive DMs for briefings/subscription login) and channels:read/groups:read (channel listing / #name resolution). A security review flags the mismatch, or a hand-built app missing im:write breaks proactive DM features like the subscription login link.
- Evidence: bobi/templates/slack-app.manifest.yaml:25-38 lists 13 bot scopes including channels:read (line 27), groups:read (line 32), and im:write (line 35), none of which appear in the doc's table (lines 49-54) or the 'Plus' sentence (lines 56-57).

## D058 — `skills/slack-setup.md:152` (doc-drift, medium, confirmed, phase 8)

**The 'Multiple workspaces' section instructs storing per-workspace Slack tokens in ~/.config/bobi/credentials.yaml, a credential store no code reads anymore.**

- Detail: A user with two workspaces follows the doc and writes xoxb- tokens into ~/.config/bobi/credentials.yaml; nothing loads that file, so the second workspace's bot silently never authenticates. The doc claims a named-credential store exists; reality is credentials live only in each agent's run/.env (and CLAUDE.md states 'Credentials belong in runtime .env files or environment variables').
- Evidence: grep for 'credentials.yaml' and 'slack_bot_token' across bobi/ (all .py) returns zero readers; the only other mention is a stale historical entry at CHANGELOG.md:1751. Current credential resolution is bobi/config.py:436 svc.credentials.get(key) fed from run/.env via ${VAR} refs (bobi/cli.py:719-768 install flow).

## D101 — `tests/integration/COVERAGE.md:13` (doc-drift, low, confirmed, phase 9)

**COVERAGE.md's marker column claims the subagent-launch and e2e-event-flow tests are claude-gated, but both files now run dual-brain with an unmarked stub leg in integration-fast.**

- Detail: The doc says subagent row is 'claude (launch only)' and the events row '— / claude', and that claude-marked tests 'run in integration-claude; all others run in integration-fast'. In reality test_agent_launch.py and test_e2e_event_flow.py carry no pytest.mark.claude at all — they bind bobi_env to dual_brain_env (stub param runs unconditionally, claude param gated by a skipif on the CLI, not the marker). Someone auditing CI coverage from this table concludes launch/e2e-flow tests never run on PRs, when the stub legs do — and conversely may not realize `-m "not claude"` does not deselect the dual-brain claude legs.
- Evidence: tests/integration/test_agent_launch.py:20-30 and tests/integration/test_e2e_event_flow.py:21-31 (dual_brain_env fixtures, no claude pytestmark; grep for pytestmark/mark.claude in both files returns nothing) vs COVERAGE.md rows 13 and 17-18; BRAIN_PARAMS in tests/integration/conftest.py:359-362 uses requires_claude (skipif), not the claude marker.

## Q044 — `tests/integration/test_context_rotation.py:1` (structure, medium, confirmed, phase 9)

**test_context_rotation.py lives in tests/integration/ but is a fully-mocked unit test (MagicMock SDK clients, no subprocess, no event server, no claude gate), so the default unit lane (`pytest tests/ --ignore=tests/integration/`) never runs it.**

- Detail: Its own docstring says 'They use mocked SDK clients to avoid real Claude sessions', and grep finds zero uses of subprocess, ensure_running, cli_run, or bobi_env — it uses the root conftest's in-process bobi_install fixture like every unit test. Under the repo's own definition (CLAUDE.md: 'Integration tests drive real Claude Code sessions'), it is misfiled: a fast, deterministic, hermetic suite is exiled to the slow lane that developers run less often, which is the opposite of what you want for rotation-regression coverage. The better shape: move it to tests/test_context_rotation.py (joining the existing unit-level test_rotation_metric.py and test_image_rotation.py). Behavior preserved trivially — nothing about the tests depends on the integration conftest (they never touch bobi_env or cli_run). test_completion_delivery_loop.py and the two test_pr_feedback_*_dispatch files are the same shape (in-process drain_loop with patches, no subprocess) and are candidates for the same move.
- Evidence: File docstring L3-5 states mocked SDK clients; grep 'subprocess|Popen|ensure_running|cli_run|bobi_env' in the file returns nothing; it consumes bobi_install (root tests/conftest.py fixture) at L87; grep in test_completion_delivery_loop.py for the same integration markers also returns 0 hits.

## D103 — `tests/integration/test_event_server.py:42` (duplication, low, confirmed, phase 9)

**An identical _free_port() helper is defined in seven separate integration test files instead of once in tests/integration/conftest.py.**

- Detail: Seven copies of the same socket-bind helper (test_event_server.py:42, test_slack_live.py:50, test_discord_gateway.py:56, test_channel_gateway.py:33, test_inbox_transport.py:29, test_whatsapp_gateway.py:49, test_event_isolation.py:72); the same port-picking logic is also inlined in conftest's _provision_bobi_env (lines 52-55). Any fix (e.g. adding SO_REUSEADDR or a retry against the pick-then-close race) has to be applied eight times.
- Evidence: grep 'def _free_port' tests/integration returns the seven sites listed; tests/integration/conftest.py:52-55 contains the same inline socket-bind pattern.

## D100 — `tests/integration/test_event_server.py:877` (test-quality, low, confirmed, phase 9)

**_send_and_drain ignores the ready.wait() result and its WS thread swallows every exception, so negative-delivery tests pass vacuously when the subscription never connects.**

- Detail: If websocket.create_connection raises in _ws_thread (auth regression on /deployments/<id>/subscribe, server crash), the exception dies in the daemon thread, ready.wait(timeout=5) at line 877 returns False unchecked, the webhook is still sent, and the helper returns []. test_bot_own_message_not_delivered (line 560) then asserts len(slack_events) == 0 and PASSES while proving nothing about self-reply-loop prevention; test_token_is_bubble_scoped (line 1603) likewise 'proves' bubble isolation with an empty list from a dead socket. By contrast the _live_subscriber sites all do `assert ready.wait(5)` (e.g. lines 993, 1047, 1142).
- Evidence: tests/integration/test_event_server.py:877 `ready.wait(timeout=5)` with return value discarded; lines 869-870 and the unguarded create_connection at 851-855 in _ws_thread; negative assertions at lines ~573 and ~1628 that expect empty event lists.

## Q128 — `tests/integration/test_gateway_openai_brain.py:244` (missed-reuse, low, unverified, phase 9)

**test_gateway_openai_brain.py defines its own async _drain(session) that duplicates tests/integration/conftest._drain — the helper whose docstring says it exists precisely so 'the brain message protocol is consumed in one place' by the gateway suites.**

- Detail: conftest._drain (L291-305) and the local copy (L244-253) are identical except the local one accumulates text ('text += msg.text') where conftest keeps only the last chunk ('text = msg.text'). The clean fix is to make the conftest version accumulate (strictly more information; existing consumers in test_cross_model_resume.py and test_gateway_brain.py assert on substring containment or the TurnResult, so accumulated text keeps their assertions true) and delete the local copy. Behavior preserved for every current caller; one canonical definition of turn-draining remains, as the conftest docstring already promises.
- Evidence: Read both functions: same signature, same AssistantText/TurnResult loop, one-token diff (+= vs =); conftest._drain docstring names 'gateway' as an intended consumer; grep '_drain(' shows test_gateway_brain.py and test_cross_model_resume.py already import the conftest one while this file shadows it.

## Q041 — `tests/integration/test_inbox_transport.py:53` (missed-reuse, medium, confirmed, phase 9)

**Four integration files hand-roll /health polling loops (raw urllib + json + status=='ok') that bobi.events.server.health() already implements — and that function's own docstring declares it 'the single definition of what counts as healthy'.**

- Detail: test_inbox_transport.py inlines the urllib poll twice (L53-64 and L281-292), test_event_server.py defines _wait_healthy (L165) on top of its own _get_json, and test_slack_socket_mode.py defines _wait_for_health (L451). Meanwhile test_event_isolation.py (L109, L133-138) already shows the house pattern: import health from bobi.events.server and poll it. The better shape is a tiny shared wait_healthy(base_url, timeout) in tests/integration/conftest.py that loops on bobi.events.server.health(). Behavior preserved: health() performs exactly the same GET /health + status=='ok' check the inline loops do, with the same swallow-exceptions semantics.
- Evidence: Read bobi/events/server.py L111-125 (health() docstring: 'The single definition of what counts as healthy'); read the four hand-rolled polls; test_event_isolation.py L109 imports and uses health() proving the reuse target works in this exact context.

## D012 — `tests/integration/test_manager_lifecycle.py:218` (test-quality, high, confirmed, phase 9)

**The only two checks in the whole suite that the manager's drain loop actually starts convert boot failure into pytest.skip, so a drain-loop regression turns CI silently green instead of red.**

- Detail: A regression breaks drain-loop startup (e.g. bobi/events/drain.py never logs 'Drain loop active'). The autouse _start_and_stop fixture in TestManagerMessaging waits 60s then calls pytest.skip('Manager did not become ready'), and the identical _start_stack fixture in tests/integration/test_e2e_event_flow.py:58 skips the entire TestEndToEndEventFlow class. Both files run the stub-brain leg in the integration-fast CI job precisely as the deterministic CI proof of this plumbing (their docstrings say so), so the regression lands with all CI checks passing — every test that could catch it skips.
- Evidence: grep 'Drain loop active' over tests/ hits only test_manager_lifecycle.py:211 and test_e2e_event_flow.py:51, and both readiness loops end in pytest.skip (lines 218 and 58) rather than an assert. TestManagerStartStop's assertions cover only pid-file/log-file existence, not drain readiness. bobi/events/drain.py:250 is the log line being waited on.

## Q043 — `tests/integration/test_pr_feedback_followup_dispatch.py:22` (structure, medium, confirmed, phase 9)

**The run-drain_loop-for-one-batch harness (_OneShotQueue + _CaptureInbox + register/unregister_local_inbox + patch time.sleep + swallow KeyboardInterrupt) is re-built in five files, and one of them documents the debt itself: 'minimal drain harness (mirrors test_pr_feedback_followup_dispatch)'.**

- Detail: _OneShotQueue appears in test_pr_feedback_followup_dispatch.py L22, test_pr_feedback_dispatch_hygiene.py L27, test_completion_delivery_loop.py L37, test_drain_dispatch.py L20, and test_sleep_cycle.py L980; the surrounding _drain_one_batch harness is re-built three times with drift (reactor parameter or not; capturing msg.text vs the Message object; different hardcoded session names). Beyond the verbatim queue class (previous pass's territory), the structural fix is harness-level: a tests/drain_utils.py beside the existing tests/workflow_utils.py exporting drain_one_batch(events, *, session, reactor=None, capture='text') that owns the inbox registration/unregistration, sleep patch, and KeyboardInterrupt protocol. Each caller then states only its events and assertions. Behavior preserved: all copies drive the identical drain_loop entry with the identical stop protocol; the drift points become explicit parameters.
- Evidence: grep 'class _OneShotQueue' hits 5 files; grep '_drain_one_batch' hits 3; test_completion_delivery_loop.py L33 comment literally says 'mirrors test_pr_feedback_followup_dispatch'; read the three harnesses and confirmed the differences are only reactor arg, captured payload, and session name.

## D105 — `tests/test_kb_embedder.py:27` (test-quality, low, confirmed, phase 9)

**The mock_project_root fixture is dead code — defined but never requested by any test.**

- Detail: Every test in the file uses the state_dir fixture instead; no test parameter or getfixturevalue call references mock_project_root anywhere in the suite (AST scan of all test files), so its _state_dir monkeypatch never executes.
- Evidence: tests/test_kb_embedder.py:26-33 defines it; project-wide grep for 'mock_project_root' matches only the definition.

## D102 — `tests/test_orchestrator.py:399` (duplication, low, confirmed, phase 9)

**The fake Claude-SDK message dataclasses (FakeResultMessage/FakeTextBlock/FakeAssistantMessage) are copy-pasted across four unit-test files, two of them verbatim identical.**

- Detail: When the SDK result-message shape changes (as it has repeatedly: deferred_tool_use, api_error_status, usage fields already diverge between copies), each of the four copies must be updated separately; a copy that lags silently tests an obsolete message shape. Consolidatable into a shared tests helper module (the repo already has tests/workflow_utils.py for exactly this pattern).
- Evidence: tests/test_orchestrator.py:399-418 and tests/test_notify_step.py:401-418 are verbatim identical; tests/test_subagent_blocking.py:67-104 and tests/test_completion_delivery.py:27-48 are near-identical variants of the same three dataclasses.

## Q110 — `tests/test_orchestrator.py:567` (simplification, medium, plausible, phase 9)

**test_orchestrator.py defines the same 4-line inline 'class FakeBrain: def make_session(...): calls.append(kwargs); return FakeBrainClient()' plus its calls/clients list boilerplate inside roughly a dozen separate test methods.**

- Detail: Lines 567, 596, 628, 655, 689, 714, 779, 809, 831, 857, 892, 932 each open with 'calls = []' and an inline FakeBrain whose make_session records kwargs and returns a FakeBrainClient, followed by the same monkeypatch.setattr('bobi.brain.get_brain', ...). One module-level factory — def _recording_brain(): calls, clients = [], []; class ...; return brain, calls, clients — or a small fixture collapses ~60 lines of repeated setup and makes each test body start at the interesting part (the Workflow under test). Behavior preserved: every inline copy is functionally identical; the tests only read calls[i]['options'] afterward.
- Evidence: grep 'class FakeBrain' in tests/test_orchestrator.py returns 12+ hits, all inside test methods; read L560-720 confirming each is the identical record-and-return-FakeBrainClient shape preceded by 'calls = []' and followed by the same get_brain monkeypatch.

## D104 — `tests/test_subagent.py:28` (test-quality, low, confirmed, phase 9)

**The tmp_cwd fixture is dead code — defined but never requested by any test.**

- Detail: No test in the suite lists tmp_cwd as a parameter or calls getfixturevalue('tmp_cwd') (AST scan over all test files confirms; the only occurrence of the name is its own definition), so the tempfile.mkdtemp/shutil.rmtree fixture never runs and misleads readers about how the file isolates cwd.
- Evidence: tests/test_subagent.py:27-32 defines it; project-wide grep for 'tmp_cwd' matches only the definition line.

## Q042 — `tests/test_subagent_blocking.py:67` (structure, medium, confirmed, phase 9)

**The fake brain/SDK message protocol (FakeTextBlock / FakeAssistantMessage / FakeResultMessage / FakeClient) is modeled independently in four unit files with drifting field sets, so there are four subtly different definitions of the same SDK contract.**

- Detail: test_subagent_blocking.py L67-140 (FakeResultMessage with 12 fields incl. stop_reason/errors/usage, rounds-based FakeClient), test_completion_delivery.py L27-75 (FakeResultMessage adds subtype/api_error_status, same rounds-based client), test_orchestrator.py L399-445 (7-field FakeResultMessage, single-turn FakeClient), and test_notify_step.py L401-450 (same 7-field shape but a client that yields normalized bobi.brain types instead) each re-derive the protocol. The drift is the cost: when the ResultMessage shape changes (as it did for deferred_tool_use, model_usage, effort), four models must be found and updated, and a stale one silently tests yesterday's contract. The better shape: one tests/brain_fakes.py module (the repo already has the precedent of a shared cross-file test helper in tests/workflow_utils.py) exporting the superset dataclasses and the rounds-based FakeClient; each file imports what it needs. Behavior preserved: the superset fields all have defaults, so existing constructions keep working unchanged.
- Evidence: Read all four fake families side by side: FakeResultMessage field sets are 12 vs 9 vs 7 vs 7 fields with different defaults (num_turns=5 vs 1, cost 0.10 vs 0.05 vs 0.01); FakeClient semantics split into rounds-based vs single-response variants; grep 'class Fake' across tests/ confirms no other shared definition exists.