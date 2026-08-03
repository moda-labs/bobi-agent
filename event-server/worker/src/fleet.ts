// Fleet read model + query surface (issue #6).
//
// CONTROL-PLANE surface of the Worker adapter. A fleet-of-instances view is
// meaningless in the single-team local dev server, so it ships with the Worker
// rather than with the local variants - but it is public: `docs/ADMIN_PROTOCOL.md`
// documents exactly the schemas below, and the admin contract is byte-bound to
// the Python side (`admin.py`), so a public protocol cannot have a private
// server half. The one shared seam with the local product is the `TeamRuntime`
// interface; this file is the backend that runtime and the e2e test drive.
//
// The fold sits behind a `FleetStorage` adapter (same discipline as core's
// StorageAdapter) so it is pure/testable and a real store can later replace the
// KV v1 "database" behind the same query API. Semantics honor the epic
// invariants: tier-1 heartbeats fold LATEST-VALUE (KV last-write-wins, no 48h
// replay mirror); tier-2 lifecycle events append into a bounded 48h trail;
// reachability is a read-model DERIVATION over heartbeat age (absence is
// inferred, never emitted by a dead instance).

import { constantTimeEqual } from "@moda-labs/bobi-events-core";

// The tier-1 heartbeat payload the sidecar publishes (see bobi_deploy
// supervisor/snapshot.py). Only the fields the read model reads are typed; the
// rest is carried through opaquely.
export interface DeploymentIdentity {
	fleet: string;
	instance: string;
	platform?: string | null;
	machine?: string | null;
	region?: string | null;
	node?: string | null;
}

export interface FleetSnapshot {
	deployment: DeploymentIdentity;
	supervisor?: unknown;
	manager?: Record<string, unknown>;
	sessions?: unknown[];
	event_client?: unknown;
	resources?: unknown;
	versions?: unknown;
	expectations?: unknown;
	generated_at?: string;
	[k: string]: unknown;
}

export interface LifecyclePayload {
	deployment: DeploymentIdentity;
	event: string;
	generated_at?: string;
	[k: string]: unknown;
}

export interface FleetInstanceRecord {
	snapshot: FleetSnapshot;
	received_at: number; // server receipt epoch ms - authoritative for reachability
	// The AUTHENTICATED bubble that published this heartbeat. Captured at ingest
	// so the Phase B command route can address the deployment's admin topic in
	// its own bubble (bubble namespacing is what makes admin delivery
	// fail-closed - see foldHeartbeat / the command route in index.ts).
	bubble_id?: string;
}

export interface FleetLifecycleRecord {
	payload: LifecyclePayload;
	received_at: number;
}

// A pending admin command (Phase B), written by the operator-authed POST route
// the moment it publishes the command onto the deployment's admin topic. The
// supervisor's reply lands as a SEPARATE FleetCommandResultRecord, so there is
// no read-modify-write on this record (append-only, mirroring the lifecycle
// trail): the command view (buildCommandView) merges the two at read time.
export interface FleetCommandRecord {
	command_id: string;
	fleet: string;
	instance: string;
	command: string;
	args?: Record<string, unknown>;
	issued_at: number; // server receipt epoch ms
}

// The supervisor's reply for a command, folded from a `fleet/command_result`
// event. One immutable write per command; an idempotent retry overwrites with
// identical content.
export interface FleetCommandResultRecord {
	command_id: string;
	status: "done" | "error";
	result?: unknown;
	error?: string;
	completed_at: number; // server receipt epoch ms
}

export interface FleetStorage {
	putInstance(record: FleetInstanceRecord): Promise<void>;
	getInstance(fleet: string, instance: string): Promise<FleetInstanceRecord | null>;
	listInstances(): Promise<FleetInstanceRecord[]>;
	appendLifecycle(fleet: string, instance: string, record: FleetLifecycleRecord): Promise<void>;
	listLifecycle(fleet: string, instance: string): Promise<FleetLifecycleRecord[]>;
	putCommand(record: FleetCommandRecord): Promise<void>;
	getCommand(fleet: string, instance: string, commandId: string): Promise<FleetCommandRecord | null>;
	putCommandResult(fleet: string, instance: string, record: FleetCommandResultRecord): Promise<void>;
	getCommandResult(fleet: string, instance: string, commandId: string): Promise<FleetCommandResultRecord | null>;
}

export type Reachability = "live" | "stale" | "unreachable";

export interface ReachabilityWindows {
	liveMs: number;
	staleMs: number;
}

// Defaults keyed off the 30s heartbeat interval: live within ~3 intervals,
// stale within ~10, else unreachable. Overridable via env so the e2e test can
// shrink them (a dead instance must go unreachable in seconds, not minutes).
export const DEFAULT_WINDOWS: ReachabilityWindows = {
	liveMs: 90_000,
	staleMs: 300_000,
};

const LIFECYCLE_TTL_S = 48 * 60 * 60;
// Admin commands are short-lived operational records: the operator issues one
// and polls its result within seconds. An hour is ample and keeps the KV clean.
const COMMAND_TTL_S = 60 * 60;

// ---------------------------------------------------------------------------
// KV storage adapter (the v1 "database")
// ---------------------------------------------------------------------------

function enc(s: string): string {
	return encodeURIComponent(s);
}

// Parse a KV value, returning null on corruption instead of throwing - one bad
// value must not take down a whole list read.
function safeParse<T>(v: string): T | null {
	try {
		return JSON.parse(v) as T;
	} catch {
		return null;
	}
}

export function createFleetKVStorage(kv: KVNamespace): FleetStorage {
	return {
		async putInstance(record: FleetInstanceRecord): Promise<void> {
			const { fleet, instance } = record.snapshot.deployment;
			// LATEST-VALUE: KV last-write-wins IS the fold. No replay mirror.
			await kv.put(`fleet_instance:${enc(fleet)}:${enc(instance)}`, JSON.stringify(record));
		},

		async getInstance(fleet: string, instance: string): Promise<FleetInstanceRecord | null> {
			const data = await kv.get(`fleet_instance:${enc(fleet)}:${enc(instance)}`);
			return data ? (JSON.parse(data) as FleetInstanceRecord) : null;
		},

		async listInstances(): Promise<FleetInstanceRecord[]> {
			// One list page (1000 keys) is far beyond any real fleet size in the
			// KV v1; a real store replaces this behind the same query API.
			const listed = await kv.list({ prefix: "fleet_instance:" });
			const values = await Promise.all(listed.keys.map((k) => kv.get(k.name)));
			// Skip a corrupt/partial value rather than 500 the whole fleet view.
			return values
				.filter((v): v is string => v !== null)
				.map((v) => safeParse<FleetInstanceRecord>(v))
				.filter((r): r is FleetInstanceRecord => r !== null);
		},

		async appendLifecycle(fleet: string, instance: string, record: FleetLifecycleRecord): Promise<void> {
			// One immutable key per event with a 48h TTL - NO read-modify-write
			// array (that loses entries under concurrent appends, the same hazard
			// the ingest-token store documents). The trail read lists + sorts.
			const nonce = crypto.randomUUID().slice(0, 8);
			const key = `fleet_lifecycle:${enc(fleet)}:${enc(instance)}:${record.received_at}:${nonce}`;
			await kv.put(key, JSON.stringify(record), { expirationTtl: LIFECYCLE_TTL_S });
		},

		async listLifecycle(fleet: string, instance: string): Promise<FleetLifecycleRecord[]> {
			const listed = await kv.list({ prefix: `fleet_lifecycle:${enc(fleet)}:${enc(instance)}:` });
			const values = await Promise.all(listed.keys.map((k) => kv.get(k.name)));
			const records = values
				.filter((v): v is string => v !== null)
				.map((v) => safeParse<FleetLifecycleRecord>(v))
				.filter((r): r is FleetLifecycleRecord => r !== null);
			// Newest first.
			records.sort((a, b) => b.received_at - a.received_at);
			return records;
		},

		async putCommand(record: FleetCommandRecord): Promise<void> {
			const key = `fleet_command:${enc(record.fleet)}:${enc(record.instance)}:${enc(record.command_id)}`;
			await kv.put(key, JSON.stringify(record), { expirationTtl: COMMAND_TTL_S });
		},

		async getCommand(fleet: string, instance: string, commandId: string): Promise<FleetCommandRecord | null> {
			const data = await kv.get(`fleet_command:${enc(fleet)}:${enc(instance)}:${enc(commandId)}`);
			return data ? safeParse<FleetCommandRecord>(data) : null;
		},

		async putCommandResult(fleet: string, instance: string, record: FleetCommandResultRecord): Promise<void> {
			const key = `fleet_command_result:${enc(fleet)}:${enc(instance)}:${enc(record.command_id)}`;
			await kv.put(key, JSON.stringify(record), { expirationTtl: COMMAND_TTL_S });
		},

		async getCommandResult(fleet: string, instance: string, commandId: string): Promise<FleetCommandResultRecord | null> {
			const data = await kv.get(`fleet_command_result:${enc(fleet)}:${enc(instance)}:${enc(commandId)}`);
			return data ? safeParse<FleetCommandResultRecord>(data) : null;
		},
	};
}

// ---------------------------------------------------------------------------
// Fold (ingest-time)
// ---------------------------------------------------------------------------

function validIdentity(payload: { deployment?: DeploymentIdentity }): DeploymentIdentity | null {
	const d = payload.deployment;
	if (!d || typeof d.fleet !== "string" || typeof d.instance !== "string" || !d.fleet || !d.instance) {
		return null;
	}
	return d;
}

// tier-1: overwrite the instance's latest-value record keyed on (fleet, instance).
export async function foldHeartbeat(
	store: FleetStorage,
	payload: FleetSnapshot,
	receivedAt: number,
	bubbleId?: string,
): Promise<boolean> {
	const identity = validIdentity(payload);
	if (!identity) return false;
	// LATEST-VALUE overwrite. Deliberately a blind put (no read-modify-write):
	// KV last-write-wins IS the fold, and the heartbeat already carries the
	// restart summary (manager.restart_count / last_restart_reason / _at), so
	// there is nothing to merge in from a prior record. `bubble_id` (the
	// authenticated publisher) rides along so the command route can reach this
	// deployment's admin topic in its own bubble.
	await store.putInstance({ snapshot: payload, received_at: receivedAt, bubble_id: bubbleId });
	return true;
}

// tier-2: append to the bounded trail. NO read-modify-write of the instance
// record - a get/mutate/put would clobber a concurrent heartbeat's received_at
// (a false stale/unreachable). The "last lifecycle" the fleet list needs is
// already in the heartbeat's manager summary; the full trail (newest first) is
// served by the per-instance detail read.
export async function foldLifecycle(
	store: FleetStorage,
	payload: LifecyclePayload,
	receivedAt: number,
): Promise<boolean> {
	const identity = validIdentity(payload);
	if (!identity || typeof payload.event !== "string" || !payload.event) return false;
	await store.appendLifecycle(identity.fleet, identity.instance, { payload, received_at: receivedAt });
	return true;
}

// The reply half of the command RPC: a `fleet/command_result` the supervisor
// published after acting on an admin command. Folded into a per-command result
// record (append-only, keyed by command_id) that the operator reads back via
// GET .../commands/:id. Ignores a result with no command_id or invalid status
// so a malformed reply cannot mask a command as done.
export interface CommandResultPayload {
	deployment: DeploymentIdentity;
	command_id: string;
	status: "done" | "error";
	result?: unknown;
	error?: string;
	[k: string]: unknown;
}

export async function foldCommandResult(
	store: FleetStorage,
	payload: CommandResultPayload,
	receivedAt: number,
): Promise<boolean> {
	const identity = validIdentity(payload);
	if (!identity) return false;
	if (typeof payload.command_id !== "string" || !payload.command_id) return false;
	if (payload.status !== "done" && payload.status !== "error") return false;
	await store.putCommandResult(identity.fleet, identity.instance, {
		command_id: payload.command_id,
		status: payload.status,
		result: payload.result,
		error: typeof payload.error === "string" ? payload.error : undefined,
		completed_at: receivedAt,
	});
	return true;
}

// ---------------------------------------------------------------------------
// Query (read model)
// ---------------------------------------------------------------------------

export function reachability(receivedAt: number, now: number, windows: ReachabilityWindows): Reachability {
	const age = now - receivedAt;
	if (age <= windows.liveMs) return "live";
	if (age <= windows.staleMs) return "stale";
	return "unreachable";
}

function statusEntry(record: FleetInstanceRecord, now: number, windows: ReachabilityWindows) {
	const s = record.snapshot;
	return {
		deployment: s.deployment,
		reachability: reachability(record.received_at, now, windows),
		last_heartbeat_at: s.generated_at ?? null,
		manager: s.manager ?? null,
		sessions: s.sessions ?? [],
		versions: s.versions ?? null,
	};
}

export async function buildFleetStatus(
	store: FleetStorage,
	now: number,
	windows: ReachabilityWindows,
): Promise<{ instances: ReturnType<typeof statusEntry>[] }> {
	const records = await store.listInstances();
	const instances = records
		.map((r) => statusEntry(r, now, windows))
		// Stable order so the fleet list and its tests are deterministic.
		.sort((a, b) =>
			`${a.deployment.fleet}/${a.deployment.instance}`.localeCompare(
				`${b.deployment.fleet}/${b.deployment.instance}`,
			),
		);
	return { instances };
}

// The admin command vocabulary. Phase B landed lifecycle control + a status
// echo (restart/stop/start/status). Phase C (#11) adds the on-demand tier-3
// reads and the data-plane chat over this SAME command->result plumbing: the
// supervisor executes them locally against public `bobi` (transcript/roster
// reads, `service.ask` for chat) and folds the reply as the command result.
// The server stays command-agnostic - it publishes `{command, args}` onto the
// deployment's admin topic and folds whatever result comes back; per-command
// arg shape is validated supervisor-side against live state.
//   chat        args {session?, text}   -> async: reply lands in the transcript
//   transcript  args {session?}         -> {messages: [...]}
//   roster      (no args)               -> {subagents: [...]}
//   spend       (no args)               -> {spend: {total_cost_usd, ...}}
//   session_log (no args)               -> {sessions: [...], counts: {...},
//                                           truncated: bool} (#733 vertical 3)
export const ADMIN_COMMANDS = [
	"restart",
	"stop",
	"start",
	"status",
	"chat",
	"transcript",
	"roster",
	"spend",
	"session_log",
] as const;
export type AdminCommand = (typeof ADMIN_COMMANDS)[number];

export function isAdminCommand(v: unknown): v is AdminCommand {
	return typeof v === "string" && (ADMIN_COMMANDS as readonly string[]).includes(v);
}

// The per-deployment admin topic. NON-global (see isGlobalTopic), so bubble
// namespacing keys it as `<bubble>:fleet/admin/<fleet>/<instance>`: the server
// publishes the command INTO the target deployment's bubble and only that
// deployment's supervisor (same bubble, subscribed to this exact string) can
// receive it - fail-closed by construction, no new server trust primitive. The
// supervisor builds the byte-identical string from the same identity.
export function adminTopic(fleet: string, instance: string): string {
	return `fleet/admin/${fleet}/${instance}`;
}

// The topic the supervisor publishes its command reply on (bubble-signed, like
// heartbeats/lifecycle). Folded server-side into the per-command result record.
export const COMMAND_RESULT_TOPIC = "fleet/command_result";

// Merge the pending command (POST) with its optional result (fold) into the
// operator-facing view. `pending` until the supervisor replies, then
// `done|error`. Null iff NEITHER record exists (a 404). Tolerating a missing
// pending record (result-only) keeps a folded result readable even if the
// pending write was lost after the command was delivered - the command is never
// executed-but-untrackable.
export async function buildCommandView(
	store: FleetStorage,
	fleet: string,
	instance: string,
	commandId: string,
): Promise<Record<string, unknown> | null> {
	const command = await store.getCommand(fleet, instance, commandId);
	const result = await store.getCommandResult(fleet, instance, commandId);
	if (!command && !result) return null;
	return {
		command_id: command?.command_id ?? result?.command_id ?? commandId,
		deployment: { fleet, instance },
		command: command?.command ?? null,
		args: command?.args ?? null,
		issued_at: command?.issued_at ?? null,
		status: result ? result.status : "pending",
		result: result ? (result.result ?? null) : null,
		error: result?.error ?? null,
		completed_at: result?.completed_at ?? null,
	};
}

// ---------------------------------------------------------------------------
// Issuing an admin command
// ---------------------------------------------------------------------------

// Publishes the admin event into the target deployment's bubble and returns the
// number of REGISTERED admin subscriptions it was buffered for. Injected rather
// than imported so this module stays free of the bus storage adapter: the two
// callers (the HTTP route and the MCP tools) both live in index.ts's world,
// where `createTopicEvent` + `storage.deliver` are already in scope.
export type AdminPublisher = (
	topic: string,
	payload: Record<string, unknown>,
	bubbleId: string,
) => Promise<number>;

// Why a command could not be issued. Each maps to a different HTTP status on
// the REST route and a different recovery sentence on the MCP surface, so the
// distinction is carried in the result rather than flattened to a boolean.
export type IssueFailure =
	// No heartbeat has ever been folded for this (fleet, instance).
	| "unknown_instance"
	// Known, but no bubble captured - the admin topic cannot be addressed.
	| "not_addressable"
	// Published, but no supervisor holds an admin subscription: the command
	// would black-hole, so nothing is recorded and the caller retries.
	| "not_delivered";

export type IssueOutcome =
	| { ok: true; command_id: string; delivered_to: number }
	| { ok: false; failure: IssueFailure };

/**
 * Issue an admin command against one instance.
 *
 * The single implementation behind both `POST /fleet/instances/:f/:i/commands`
 * and the MCP write tools. It publishes onto the deployment's OWN bubble (the
 * one captured from its last authenticated heartbeat), which is what makes
 * addressing fail-closed, then records the command pending. The supervisor's
 * reply arrives separately as a `fleet/command_result` fold; nothing here waits
 * for it.
 *
 * Ordering is deliberate: deliver BEFORE recording. A command recorded but
 * never delivered is a pending row that can never resolve, which is worse than
 * a delivered command whose pending row was lost - `buildCommandView` already
 * tolerates the latter (result-only reads fine) and cannot tolerate the former.
 */
export async function issueAdminCommand(
	store: FleetStorage,
	publish: AdminPublisher,
	params: {
		fleet: string;
		instance: string;
		command: AdminCommand;
		args?: Record<string, unknown>;
		now: number;
	},
): Promise<IssueOutcome> {
	const { fleet, instance, command, args, now } = params;
	const record = await store.getInstance(fleet, instance);
	if (!record) return { ok: false, failure: "unknown_instance" };
	if (!record.bubble_id) return { ok: false, failure: "not_addressable" };

	const commandId = crypto.randomUUID();
	const delivered = await publish(
		adminTopic(fleet, instance),
		{ command_id: commandId, command, args, issued_at: now },
		record.bubble_id,
	);
	if (delivered === 0) return { ok: false, failure: "not_delivered" };

	await store.putCommand({ command_id: commandId, fleet, instance, command, args, issued_at: now });
	return { ok: true, command_id: commandId, delivered_to: delivered };
}

// The bounded server-side wait (plan Q4). 5s by default, env-tunable: long
// enough that the fast commands - transcript/roster/spend/status and the
// lifecycle setters all answer well under a second - resolve in ONE tool call
// instead of three, and short enough that one wedged instance cannot hold an
// agent's tool call while its client counts down.
export const DEFAULT_COMMAND_WAIT_MS = 5_000;

// Matches the hosted console's COMMAND_POLL_INTERVAL. The console has polled
// this exact read path in production since 2026-07-10, which is the evidence
// that a result folded by a different request becomes visible to a poller.
const COMMAND_POLL_INTERVAL_MS = 250;

export function commandWaitMsFromEnv(raw?: string): number {
	const parsed = raw ? Number(raw) : NaN;
	// A non-negative finite override wins; 0 means "never wait, always poll".
	return Number.isFinite(parsed) && parsed >= 0 ? parsed : DEFAULT_COMMAND_WAIT_MS;
}

/**
 * Poll a command's view until it resolves or the budget runs out.
 *
 * Returns the view as of the last read - `status: "pending"` if the supervisor
 * had not replied in time, which is a normal outcome and not an error. The
 * caller hands the command id back so the agent can poll `bobi_command_result`.
 *
 * Reads via `buildCommandView`, i.e. two exact-key `get()`s, and deliberately
 * never a prefix `list()`: KV listings are eventually consistent while a keyed
 * read is not, so a wait built on a listing would report `pending` for a
 * command that had already resolved. Verified rather than assumed - the suite
 * folds a result from a separate request while a call is in flight.
 */
export async function awaitCommandResult(
	store: FleetStorage,
	fleet: string,
	instance: string,
	commandId: string,
	waitMs: number,
	now: () => number = Date.now,
): Promise<Record<string, unknown> | null> {
	const deadline = now() + waitMs;
	// Always read once, even at waitMs=0: a command that resolved between the
	// publish and this read should not be reported pending.
	for (;;) {
		const view = await buildCommandView(store, fleet, instance, commandId);
		if (view && view.status !== "pending") return view;
		if (now() >= deadline) return view;
		const remaining = deadline - now();
		await new Promise((resolve) => setTimeout(resolve, Math.min(COMMAND_POLL_INTERVAL_MS, remaining)));
	}
}

export async function buildInstanceDetail(
	store: FleetStorage,
	fleet: string,
	instance: string,
	now: number,
	windows: ReachabilityWindows,
): Promise<Record<string, unknown> | null> {
	const record = await store.getInstance(fleet, instance);
	if (!record) return null;
	const trail = await store.listLifecycle(fleet, instance);
	return {
		...record.snapshot,
		reachability: reachability(record.received_at, now, windows),
		last_heartbeat_at: record.snapshot.generated_at ?? null,
		lifecycle: trail.map((r) => ({ ...r.payload, received_at: r.received_at })),
	};
}

// ---------------------------------------------------------------------------
// Operator auth + config
// ---------------------------------------------------------------------------

// Operator authentication - one credential for the whole control plane (the
// epic's "exactly one client to authorize"). Distinct from bus bubble auth:
// this is the control-plane -> server direction. Fail CLOSED: an unset token
// rejects every request rather than opening the fleet to the world.
export function requireOperator(
	authHeader: string | null,
	operatorToken: string | undefined,
): Response | null {
	if (!operatorToken) {
		return Response.json({ error: "fleet API not configured" }, { status: 503 });
	}
	const presented = authHeader?.startsWith("Bearer ") ? authHeader.slice(7) : "";
	if (!presented || !constantTimeEqual(presented, operatorToken)) {
		return Response.json({ error: "unauthorized" }, { status: 401 });
	}
	return null;
}

export function windowsFromEnv(liveS?: string, staleS?: string): ReachabilityWindows {
	const live = liveS ? Number(liveS) : NaN;
	const stale = staleS ? Number(staleS) : NaN;
	return {
		liveMs: Number.isFinite(live) ? live * 1000 : DEFAULT_WINDOWS.liveMs,
		staleMs: Number.isFinite(stale) ? stale * 1000 : DEFAULT_WINDOWS.staleMs,
	};
}
