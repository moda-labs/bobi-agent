export interface NormalizedEvent {
	v: 2;
	id: string;
	source: string;
	type: string;
	timestamp: string;
	topics: string[];
	delivery: "chat" | "bulk";
	text: string;
	fields?: Record<string, string | number | boolean>;
	run_key?: string;
	payload: Record<string, unknown>;
	// Set for events published by an authenticated bubble member (generic
	// /events/{topic} publishes). Webhook-ingested events leave this UNSET so
	// they fan out on the global resource topic. Drives bubble-scoped routing
	// in subscriptionKeysForEvent — see namespaceSubKey.
	bubble_id?: string;
}

export interface SlackNormalizationResult {
	event: NormalizedEvent | null;
	challenge?: string;
	skip: boolean;
}

// ---------------------------------------------------------------------------
// Storage adapter — implemented by each runtime (KV/DO vs in-memory Maps)
// ---------------------------------------------------------------------------

export interface DeploymentRecord {
	id: string;
	name: string;
	api_key: string;
	bubble_id: string;
	subscriptions: string[];
	created_at?: string;
	// Reserved for #215 (loop-safety per-deployment identities). Declared here
	// so whichever of #240/#215 lands second only rebases the consumer, not the
	// record shape.
	identities?: Record<string, unknown>;
}

// A trust bubble. Minted once per `modastack start`; every deployment of that
// instance JOINs it. The key signs publishes and join-registrations to prove
// bubble membership. See modastack/config.py:load_or_mint_bubble.
export interface BubbleRecord {
	id: string;
	key: string;
	created_at?: string;
}

export interface SlackBotRecord {
	bot_token: string;
	bot_id?: string;
	/** App-level signing secret — inbound events from this app are verified with it. */
	signing_secret?: string;
	/** Slack api_app_id; the map key, repeated here for convenience. */
	app_id?: string;
}

export interface SlackWorkspaceRecord {
	// Legacy single-bot fields (pre-multi-bot records). Kept so an existing
	// deployment keeps working WITHOUT re-registering — `getSlackBotForApp`
	// read-migrates them. `bots` is authoritative once present.
	bot_token?: string;
	bot_id?: string;
	// Multi-bot: api_app_id -> per-bot record. Keyed by api_app_id (NOT team_id
	// or bot_id) because two bots in one workspace share a team_id, and human
	// events carry no bot_id — api_app_id is the only identifier that is unique
	// per app AND present on every inbound event_callback. Keying by team_id
	// alone is what let a second bot clobber the first (self-spam incident
	// 2026-06-24).
	bots?: Record<string, SlackBotRecord>;
}

/**
 * Resolve the per-bot record for an inbound event's api_app_id, with
 * read-migration so pre-multi-bot single-bot records still resolve.
 */
export function getSlackBotForApp(
	ws: SlackWorkspaceRecord | null | undefined,
	apiAppId: string,
): SlackBotRecord | null {
	if (!ws) return null;
	if (apiAppId && ws.bots && ws.bots[apiAppId]) return ws.bots[apiAppId];
	// Single registered bot: use it even if api_app_id is absent/unmatched
	// (defensive — a client may not have sourced api_app_id).
	if (ws.bots) {
		const vals = Object.values(ws.bots);
		if (vals.length === 1) return vals[0];
	}
	// Legacy single-bot fields.
	if (ws.bot_token || ws.bot_id) {
		return { bot_token: ws.bot_token ?? "", bot_id: ws.bot_id };
	}
	return null;
}

/**
 * The signing secret to verify an inbound Slack webhook with: the authoring
 * app's per-app secret if registered, else the global fallback (legacy
 * single-app deployments). Keeps single-app working without a redeploy.
 */
export function resolveSlackSigningSecret(
	ws: SlackWorkspaceRecord | null | undefined,
	payload: Record<string, unknown>,
	fallback: string,
): string {
	const apiAppId = (payload.api_app_id as string) || "";
	const rec = getSlackBotForApp(ws, apiAppId);
	return rec?.signing_secret || fallback || "";
}

/** Storage-aware convenience: load the workspace then resolve the signing secret. */
export async function slackSigningSecretFor(
	storage: StorageAdapter,
	payload: Record<string, unknown>,
	fallback: string,
): Promise<string> {
	const teamId = (payload.team_id as string) || "";
	const ws = teamId ? await storage.getSlackWorkspace(teamId) : null;
	return resolveSlackSigningSecret(ws, payload, fallback);
}

export interface StorageAdapter {
	getDeploymentByApiKey(apiKey: string): Promise<DeploymentRecord | null>;
	getDeploymentByName(name: string, bubbleId: string): Promise<DeploymentRecord | null>;
	putDeployment(deployment: DeploymentRecord): Promise<void>;
	removeDeployment(deployment: DeploymentRecord): Promise<void>;
	addSubscription(key: string, deploymentId: string): Promise<void>;
	removeSubscription(key: string, deploymentId: string): Promise<void>;
	deliver(event: NormalizedEvent): Promise<number>;
	getBubble(bubbleId: string): Promise<BubbleRecord | null>;
	putBubble(bubble: BubbleRecord): Promise<void>;
	getSlackWorkspace(workspaceId: string): Promise<SlackWorkspaceRecord | null>;
	putSlackWorkspace(workspaceId: string, record: SlackWorkspaceRecord): Promise<void>;
	initDeploymentSession(deploymentId: string, subscriptions: string[]): Promise<void>;
}

// ---------------------------------------------------------------------------
// Handler result — transport-agnostic response that entry files convert to
// their native response type (Response for CF workers, res.end for Node).
// ---------------------------------------------------------------------------

export interface HandlerResult {
	status: number;
	body: unknown;
}

export function createTopicEvent(
	topic: string,
	body: Record<string, unknown>,
	bubbleId?: string,
): NormalizedEvent {
	const payload = (body.payload as Record<string, unknown>) || body;

	// Build topics from any routing fields in the body
	const topics: string[] = [];
	if (body.repo) topics.push(`github:${body.repo as string}`);
	if (body.team_key) topics.push(`linear:${body.team_key as string}`);
	if (body.workspace) topics.push(`slack:${body.workspace as string}`);
	// Fallback: the topic path itself acts as the subscription key, plus the
	// source-qualified form (e.g. "monitor/support.email") so subscriptions
	// written as the full event string match too. Publishers strip the source
	// to the body when POSTing (see modastack events/publish.py) — without
	// this, "source/type" subscriptions silently never match (#235).
	if (topics.length === 0) {
		topics.push(topic);
		const source = body.source as string | undefined;
		if (source && !topic.startsWith(`${source}/`)) {
			topics.push(`${source}/${topic}`);
		}
	}

	const text = (body.text as string) || "";

	return {
		v: 2,
		id: (body.id as string) || crypto.randomUUID(),
		source: (body.source as string) || "custom",
		type: topic,
		timestamp: new Date().toISOString(),
		topics,
		delivery: (body.delivery as "chat" | "bulk") || "bulk",
		text,
		fields: body.fields as Record<string, string | number | boolean> | undefined,
		run_key: body.run_key as string | undefined,
		payload,
		bubble_id: bubbleId,
	};
}

// ---------------------------------------------------------------------------
// Adapters — canonical implementations live in adapters/*.ts.
// Re-exported here so existing imports from core continue to work.
// ---------------------------------------------------------------------------

import { normalizeGitHubWebhook } from "./adapters/github";
import { normalizeLinearWebhook } from "./adapters/linear";
import { normalizeSlackWebhook } from "./adapters/slack";

export { normalizeGitHubWebhook as normalizeGitHubPayload } from "./adapters/github";
export { normalizeLinearWebhook as normalizeLinearPayload } from "./adapters/linear";
export { normalizeSlackWebhook as normalizeSlackPayload } from "./adapters/slack";

// ---------------------------------------------------------------------------
// Routing — topics-based (v2)
// ---------------------------------------------------------------------------

// Webhook resource topics that stay GLOBAL (cross-bubble) in v1. Inbound
// webhooks fan out to every subscribing bubble regardless of bubble — an
// accepted cross-tenant read hole, to be closed by #239 (inbound subscription
// auth). Slack inbound rides this path, so it keeps working. Everything else
// (inbox/*, reply/*, monitor/*, agent/*, custom topics) is bubble-scoped.
const GLOBAL_TOPIC_PREFIXES = ["github:", "linear:", "slack:"];

export function isGlobalTopic(key: string): boolean {
	return GLOBAL_TOPIC_PREFIXES.some((p) => key.startsWith(p));
}

// The single source of truth for bubble namespacing — used identically when
// REGISTERING a subscription and when computing an event's delivery keys, so a
// publish and a subscription can only ever match within the same bubble.
// Global webhook topics are never namespaced. A non-global key with no bubble
// context (e.g. an unauthenticated publish) is returned bare — it then matches
// no bubble-namespaced subscription, so it silently reaches nobody.
export function namespaceSubKey(bubbleId: string | undefined, key: string): string {
	if (isGlobalTopic(key)) return key;
	if (!bubbleId) return key;
	return `${bubbleId}:${key}`;
}

export function subscriptionKeysForEvent(event: NormalizedEvent): string[] {
	const topics = event.topics?.length ? event.topics : [event.type];
	return topics.map((t) => namespaceSubKey(event.bubble_id, t));
}

// ---------------------------------------------------------------------------
// Signature verification
// ---------------------------------------------------------------------------

async function hmacSha256Hex(secret: string, data: Uint8Array | string): Promise<string> {
	const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
	const key = await crypto.subtle.importKey(
		"raw",
		new TextEncoder().encode(secret),
		{ name: "HMAC", hash: "SHA-256" },
		false,
		["sign"],
	);
	const sig = await crypto.subtle.sign("HMAC", key, bytes);
	return Array.from(new Uint8Array(sig))
		.map((b) => b.toString(16).padStart(2, "0"))
		.join("");
}

// Constant-time string compare. Portable across Node and the Cloudflare
// Worker runtime — `crypto.subtle.timingSafeEqual` does NOT exist (Node's
// is `node:crypto.timingSafeEqual`, Workers expose neither uniformly), so we
// do a length-check then XOR-accumulate over char codes. A length mismatch
// returns fast, which is fine: the compared values here are fixed-length hex
// digests, so length never leaks the secret — only that the attacker sent the
// wrong size. Never feed attacker-variable-length input expecting secrecy of
// length; always compare equal-length digests.
export function constantTimeEqual(a: string, b: string): boolean {
	if (a.length !== b.length) return false;
	let diff = 0;
	for (let i = 0; i < a.length; i++) {
		diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
	}
	return diff === 0;
}

// Allowed signature algorithms (the `x-moda-algo` header). The field is
// reserved so Ed25519 can slot in later (epic auth-v1); for now the server
// rejects anything else rather than trusting the client's choice.
const ALLOWED_BUBBLE_ALGOS = new Set(["hmac-sha256"]);

// Canonical string signed by every authenticated bubble request. The nonce
// field is included NOW so the wire format is forward-compatible with
// server-side replay dedup (deferred to a hardening follow-up — #240 does not
// yet maintain the seen-set). `path` is the exact bytes on the wire
// (pathname + search); `body` is the exact transmitted bytes (the client
// signs what it sends, never a re-serialization). timestamp is epoch SECONDS.
export function bubbleCanonicalString(
	timestamp: string,
	nonce: string,
	method: string,
	path: string,
	body: string,
): string {
	return `${timestamp}\n${nonce}\n${method.toUpperCase()}\n${path}\n${body}`;
}

export async function buildBubbleSignature(
	secret: string,
	timestamp: string,
	nonce: string,
	method: string,
	path: string,
	body: string,
): Promise<string> {
	return hmacSha256Hex(
		secret,
		bubbleCanonicalString(timestamp, nonce, method, path, body),
	);
}

export interface BubbleSignatureInput {
	secret: string;
	algo: string;
	timestamp: string;
	nonce: string;
	method: string;
	path: string;
	body: string;
	signature: string;
}

// Verify a bubble-signed request. Mirrors the CONSTRUCTION of
// verifySlackSignature (timestamp window + HMAC-SHA256) but uses a
// constant-time comparison. Rejects unknown algorithms and stale timestamps
// (±300s replay window). Returns false on any failure — callers respond with
// an opaque 403 and should perform a dummy HMAC on bubble-miss so that
// miss and signature-mismatch are timing-indistinguishable (no bubble_id
// enumeration).
export async function verifyBubbleSignature(input: BubbleSignatureInput): Promise<boolean> {
	const { secret, algo, timestamp, nonce, method, path, body, signature } = input;
	if (!timestamp || !nonce || !signature) return false;
	if (!ALLOWED_BUBBLE_ALGOS.has(algo)) return false;

	const ts = parseInt(timestamp, 10);
	if (!Number.isFinite(ts)) return false;
	const age = Math.abs(Date.now() / 1000 - ts);
	if (age > 300) return false;

	const expected = await buildBubbleSignature(secret, timestamp, nonce, method, path, body);
	return constantTimeEqual(expected, signature);
}

export async function verifySlackSignature(
	secret: string,
	timestamp: string,
	body: string,
	signature: string,
): Promise<boolean> {
	if (!timestamp || !signature) return false;

	const age = Math.abs(Date.now() / 1000 - parseInt(timestamp, 10));
	if (age > 300) return false;

	const hexSig = "v0=" + (await hmacSha256Hex(secret, `v0:${timestamp}:${body}`));
	return constantTimeEqual(hexSig, signature);
}

export async function verifyGitHubSignature(
	secret: string,
	body: Uint8Array,
	signatureHeader: string,
): Promise<boolean> {
	if (!signatureHeader) return false;

	const expected = "sha256=" + (await hmacSha256Hex(secret, body));
	return constantTimeEqual(expected, signatureHeader);
}

// A request carrying bubble-signing headers (x-moda-*) plus the exact wire
// bytes the signature covers. Entry files (local.ts / index.ts) build this from
// the incoming request; the raw body and full path (pathname + search) MUST be
// the exact transmitted bytes — never re-serialized — or the signature will not
// reproduce. See modastack/events/publish.py and client.py for the signer.
export interface BubbleAuthContext {
	bubbleId: string;
	algo: string;
	timestamp: string;
	nonce: string;
	signature: string;
	method: string;
	path: string;
	rawBody: string;
}

export function readBubbleAuthHeaders(
	get: (name: string) => string | null | undefined,
	method: string,
	path: string,
	rawBody: string,
): BubbleAuthContext {
	return {
		bubbleId: get("x-moda-bubble") || "",
		algo: get("x-moda-algo") || "",
		timestamp: get("x-moda-timestamp") || "",
		nonce: get("x-moda-nonce") || "",
		signature: get("x-moda-signature") || "",
		method,
		path,
		rawBody,
	};
}

export function hasBubbleSignature(ctx: BubbleAuthContext): boolean {
	return !!(ctx.bubbleId && ctx.signature && ctx.timestamp && ctx.nonce);
}

// True when SOME but not all signing headers are present — a malformed request
// (e.g. a proxy stripped a header). Registration must reject these rather than
// silently falling back to MINT, which would fork the session into a new
// bubble. A genuine mint carries NO signing headers.
export function hasPartialBubbleSignature(ctx: BubbleAuthContext): boolean {
	const any = !!(ctx.bubbleId || ctx.signature || ctx.timestamp || ctx.nonce || ctx.algo);
	return any && !hasBubbleSignature(ctx);
}

// A fixed dummy key used to run a constant-cost HMAC when the claimed bubble
// does not exist, so a bubble-miss and a signature-mismatch take the same time
// — an attacker cannot enumerate valid bubble_ids by timing.
const DUMMY_BUBBLE_KEY = "bkey_0000000000000000000000000000000000000000000000000000000000000000";

// ---------------------------------------------------------------------------
// Auth rejection counters — in-memory, reset on restart. Surfaced via /health
// so a misconfigured or out-of-date client is visible without grepping logs.
// ---------------------------------------------------------------------------

export interface AuthRejectionCounters {
	bad_signature: number;
	stale_timestamp: number;
	unknown_bubble: number;
}

const _rejectionCounters: AuthRejectionCounters = {
	bad_signature: 0,
	stale_timestamp: 0,
	unknown_bubble: 0,
};

export function getAuthRejectionCounters(): AuthRejectionCounters {
	return { ..._rejectionCounters };
}

export function resetAuthRejectionCounters(): void {
	_rejectionCounters.bad_signature = 0;
	_rejectionCounters.stale_timestamp = 0;
	_rejectionCounters.unknown_bubble = 0;
}

// Resolve and verify the bubble that signed a request. Returns the bubble on a
// valid signature, else null (callers respond with an opaque 403). Always
// performs an HMAC even on bubble-miss to keep timing uniform. Increments
// rejection counters on failure so /health can surface misconfigured clients.
export async function authenticateBubble(
	storage: StorageAdapter,
	ctx: BubbleAuthContext,
): Promise<BubbleRecord | null> {
	const bubble = ctx.bubbleId ? await storage.getBubble(ctx.bubbleId) : null;
	const secret = bubble?.key ?? DUMMY_BUBBLE_KEY;

	// Check for stale timestamp before HMAC — the verifier rejects it anyway,
	// but we want to classify the rejection reason for observability.
	const ts = parseInt(ctx.timestamp, 10);
	const isStale = Number.isFinite(ts) && Math.abs(Date.now() / 1000 - ts) > 300;

	const ok = await verifyBubbleSignature({
		secret,
		algo: ctx.algo,
		timestamp: ctx.timestamp,
		nonce: ctx.nonce,
		method: ctx.method,
		path: ctx.path,
		body: ctx.rawBody,
		signature: ctx.signature,
	});

	if (!ok || !bubble) {
		// Classify the rejection for the counter.
		if (isStale) {
			_rejectionCounters.stale_timestamp++;
		} else if (!bubble && ctx.bubbleId) {
			_rejectionCounters.unknown_bubble++;
		} else {
			_rejectionCounters.bad_signature++;
		}
		return null;
	}
	return bubble;
}

function randomToken(prefix: string): string {
	return `${prefix}_${crypto.randomUUID().replace(/-/g, "")}${crypto.randomUUID().replace(/-/g, "")}`;
}

export interface SlackSendResult {
	ok: boolean;
	error?: string;
	ts?: string;
	[key: string]: unknown;
}

export async function sendSlackMessage(
	botToken: string,
	channel: string,
	text: string,
	threadTs?: string,
): Promise<SlackSendResult> {
	const body: Record<string, unknown> = { channel, text };
	if (threadTs) body.thread_ts = threadTs;

	const resp = await fetch("https://slack.com/api/chat.postMessage", {
		method: "POST",
		headers: {
			Authorization: `Bearer ${botToken}`,
			"Content-Type": "application/json",
		},
		body: JSON.stringify(body),
	});
	return (await resp.json()) as SlackSendResult;
}

// ---------------------------------------------------------------------------
// Transport-agnostic handlers
// ---------------------------------------------------------------------------

export async function authenticateDeployment(
	storage: StorageAdapter,
	apiKey: string,
	deploymentId: string,
): Promise<DeploymentRecord | null> {
	const deployment = await storage.getDeploymentByApiKey(apiKey);
	if (!deployment || deployment.id !== deploymentId) return null;
	return deployment;
}

export async function handleGitHubWebhook(
	storage: StorageAdapter,
	eventHeader: string,
	deliveryId: string,
	payload: Record<string, unknown>,
): Promise<HandlerResult> {
	const event = normalizeGitHubWebhook(eventHeader, deliveryId, payload);
	if (!event) return { status: 400, body: { error: "no repository in payload" } };
	const delivered = await storage.deliver(event);
	return { status: 200, body: { delivered_to: delivered } };
}

export async function handleLinearWebhook(
	storage: StorageAdapter,
	payload: Record<string, unknown>,
): Promise<HandlerResult> {
	const event = normalizeLinearWebhook(payload);
	const delivered = await storage.deliver(event);
	return { status: 200, body: { delivered_to: delivered } };
}

export async function handleSlackWebhook(
	storage: StorageAdapter,
	payload: Record<string, unknown>,
): Promise<HandlerResult> {
	const teamId = (payload.team_id as string) || "";
	const apiAppId = (payload.api_app_id as string) || "";
	let selfBotId: string | undefined;
	if (teamId) {
		const ws = await storage.getSlackWorkspace(teamId);
		// Resolve THIS app's own bot id (keyed by api_app_id) so its messages —
		// and only its own — are skipped. A second bot in the workspace no longer
		// hides this one's id.
		selfBotId = getSlackBotForApp(ws, apiAppId)?.bot_id;
	}

	const result = normalizeSlackWebhook(payload, selfBotId);

	if (result.challenge !== undefined) {
		return { status: 200, body: { challenge: result.challenge } };
	}
	if (result.skip || !result.event) {
		return { status: 200, body: { ok: true } };
	}

	const delivered = await storage.deliver(result.event);
	return { status: 200, body: { delivered_to: delivered } };
}

// Register a deployment into a bubble — MINT or JOIN.
//   MINT (no bubble-signing headers): server generates a fresh bubble + key,
//     returns the key ONCE (over TLS). Used only by `modastack start`'s
//     one-time bootstrap.
//   JOIN (signed with an existing bubble's key): server verifies the signature
//     against THAT bubble's stored key and attaches the deployment to it. Every
//     session of a running instance joins the bubble minted at start.
// Subscriptions are stored under bubble-namespaced keys so a deployment only
// ever receives its own bubble's events (plus global webhook topics).
export async function handleRegisterDeployment(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	ctx: BubbleAuthContext,
): Promise<HandlerResult> {
	const name = body.name as string;
	const subscriptions = body.subscriptions as string[];

	if (!name || !subscriptions?.length) {
		return { status: 400, body: { error: "name and subscriptions[] required" } };
	}

	// A request with incomplete signing headers is malformed — reject it
	// rather than treating it as an (unsigned) MINT, which would silently fork
	// the caller into a brand-new bubble.
	if (hasPartialBubbleSignature(ctx)) {
		return { status: 403, body: { error: "forbidden" } };
	}

	let bubble: BubbleRecord;
	const minting = !hasBubbleSignature(ctx);
	if (minting) {
		bubble = {
			id: randomToken("bub"),
			key: randomToken("bkey"),
			created_at: new Date().toISOString(),
		};
		await storage.putBubble(bubble);
	} else {
		const authed = await authenticateBubble(storage, ctx);
		if (!authed) return { status: 403, body: { error: "forbidden" } };
		bubble = authed;
	}

	// Supersede any prior deployment with the same name in this bubble — a
	// re-register (e.g. after losing deployment_state.json or a --fresh start)
	// must not leave a stale deployment in the subscription index, otherwise
	// directed events (inbox/<name>) get delivered twice (#278 bug 1).
	const prior = await storage.getDeploymentByName(name, bubble.id);
	if (prior) {
		for (const sub of prior.subscriptions) {
			await storage.removeSubscription(namespaceSubKey(bubble.id, sub), prior.id);
		}
		await storage.removeDeployment(prior);
	}

	const deploymentId = crypto.randomUUID();
	const apiKey = `moda_${crypto.randomUUID().replace(/-/g, "")}`;

	const deployment: DeploymentRecord = {
		id: deploymentId,
		name,
		api_key: apiKey,
		bubble_id: bubble.id,
		subscriptions,
		created_at: new Date().toISOString(),
	};

	await storage.putDeployment(deployment);

	for (const sub of subscriptions) {
		await storage.addSubscription(namespaceSubKey(bubble.id, sub), deploymentId);
	}

	await storage.initDeploymentSession(deploymentId, subscriptions);

	const resp: Record<string, unknown> = {
		deployment_id: deploymentId,
		api_key: apiKey,
		bubble_id: bubble.id,
	};
	// The bubble key transits exactly once, at mint, over TLS. Never on join.
	if (minting) resp.bubble_key = bubble.key;

	return { status: 201, body: resp };
}

export async function handleUpdateSubscriptions(
	storage: StorageAdapter,
	deploymentId: string,
	apiKey: string,
	body: Record<string, unknown>,
): Promise<HandlerResult> {
	const deployment = await authenticateDeployment(storage, apiKey, deploymentId);
	if (!deployment) return { status: 403, body: { error: "unauthorized" } };

	const newSubs = body.add as string[] | undefined;
	if (!newSubs?.length) {
		return { status: 400, body: { error: "add[] required" } };
	}

	// Namespace from the AUTHENTICATED deployment record's bubble — never from a
	// client-supplied bubble_id — so update-subscriptions can't escape the
	// deployment's bubble. Same keying as registration (namespaceSubKey).
	let added = 0;
	for (const sub of newSubs) {
		if (!deployment.subscriptions.includes(sub)) {
			deployment.subscriptions.push(sub);
			added++;
		}
		await storage.addSubscription(namespaceSubKey(deployment.bubble_id, sub), deploymentId);
	}

	await storage.putDeployment(deployment);

	return { status: 200, body: { subscriptions: deployment.subscriptions, added } };
}

export async function handleDeregisterDeployment(
	storage: StorageAdapter,
	deploymentId: string,
	apiKey: string,
): Promise<HandlerResult> {
	const deployment = await authenticateDeployment(storage, apiKey, deploymentId);
	if (!deployment) {
		return { status: 403, body: { error: "unauthorized" } };
	}

	for (const sub of deployment.subscriptions) {
		await storage.removeSubscription(namespaceSubKey(deployment.bubble_id, sub), deploymentId);
	}
	await storage.removeDeployment(deployment);

	return { status: 200, body: { ok: true } };
}

// Publish to a generic topic. The publisher MUST sign with its bubble key —
// the authenticated bubble stamps the event so it routes only within that
// bubble (global webhook topics excepted). An unsigned/invalid publish is
// rejected; without this an attacker could inject into any bubble by naming a
// topic, since namespacing alone is not authentication.
export async function handleTopicEvent(
	storage: StorageAdapter,
	topic: string,
	body: Record<string, unknown>,
	ctx: BubbleAuthContext,
): Promise<HandlerResult> {
	const bubble = await authenticateBubble(storage, ctx);
	if (!bubble) return { status: 403, body: { error: "forbidden" } };

	const event = createTopicEvent(topic, body, bubble.id);
	const delivered = await storage.deliver(event);
	return { status: 200, body: { delivered_to: delivered } };
}

export async function handleSlackSend(
	storage: StorageAdapter,
	body: Record<string, unknown>,
): Promise<HandlerResult> {
	const channel = body.channel as string;
	const text = body.text as string;
	if (!channel || !text) {
		return { status: 400, body: { error: "channel and text required" } };
	}

	const workspaceId = body.workspace as string;
	if (!workspaceId) {
		return { status: 400, body: { error: "no bot token for workspace" } };
	}

	const ws = await storage.getSlackWorkspace(workspaceId);
	if (!ws) {
		return { status: 400, body: { error: "no bot token for workspace" } };
	}

	// Pick the sending bot's token: by app_id, then bot_id, then the single
	// registered bot, then the legacy workspace token. With several bots in a
	// workspace, "the workspace's bot_token" is ambiguous.
	const appId = (body.app_id as string) || "";
	const botId = (body.bot_id as string) || "";
	let botToken = ws.bot_token || "";
	if (appId && ws.bots?.[appId]) {
		botToken = ws.bots[appId].bot_token;
	} else if (botId && ws.bots) {
		const match = Object.values(ws.bots).find((b) => b.bot_id === botId);
		if (match) botToken = match.bot_token;
	} else if (!botToken && ws.bots) {
		const vals = Object.values(ws.bots);
		if (vals.length >= 1) botToken = vals[0].bot_token;
	}
	if (!botToken) {
		return { status: 400, body: { error: "no bot token for workspace" } };
	}

	let result;
	try {
		result = await sendSlackMessage(botToken, channel, text, body.thread_ts as string | undefined);
	} catch (err) {
		return { status: 502, body: { ok: false, error: String(err) } };
	}
	if (!result.ok) {
		return { status: 502, body: { ok: false, error: result.error } };
	}
	return { status: 200, body: { ok: true, ts: result.ts } };
}

export async function handleSlackWorkspaceRegister(
	storage: StorageAdapter,
	body: Record<string, unknown>,
): Promise<HandlerResult> {
	const workspaceId = body.workspace_id as string;
	const botToken = body.bot_token as string;
	if (!workspaceId || !botToken) {
		return { status: 400, body: { error: "workspace_id and bot_token required" } };
	}

	// Accept explicit bot_id/app_id when the caller already resolved them (e.g.
	// tests, or a Python client). Fall back to auth.test (bot_id) and
	// bots.info (app_id) — auth.test does NOT return app_id.
	let botId = (body.bot_id as string) || undefined;
	let appId = (body.app_id as string) || undefined;
	const signingSecret = (body.signing_secret as string) || undefined;
	if (!botId) {
		try {
			const resp = await fetch("https://slack.com/api/auth.test", {
				headers: { Authorization: `Bearer ${botToken}` },
			});
			const data = (await resp.json()) as Record<string, unknown>;
			if (data.ok) botId = data.bot_id as string;
		} catch {
			// best-effort — self-loop filtering degrades gracefully without bot_id
		}
	}
	if (!appId && botId) {
		try {
			const resp = await fetch(
				`https://slack.com/api/bots.info?bot=${encodeURIComponent(botId)}`,
				{ headers: { Authorization: `Bearer ${botToken}` } },
			);
			const data = (await resp.json()) as Record<string, unknown>;
			if (data.ok) appId = (data.bot as Record<string, unknown>)?.app_id as string;
		} catch {
			// best-effort — falls back to bot_id-keyed storage below
		}
	}

	// UPSERT one entry into the per-app map — never overwrite the whole record,
	// so a second bot registering the same workspace doesn't clobber the first.
	const existing = await storage.getSlackWorkspace(workspaceId);
	const bots: Record<string, SlackBotRecord> = { ...(existing?.bots ?? {}) };
	// Migrate a pre-existing legacy single-bot record into the map.
	if (existing?.bot_id && existing.bot_token && !bots[existing.bot_id]) {
		bots[existing.bot_id] = { bot_token: existing.bot_token, bot_id: existing.bot_id };
	}
	// Key by api_app_id; fall back to bot_id when app_id couldn't be resolved
	// (still unique per bot within a workspace, just not loop-safe across two
	// bots that share a bot_id — which never happens).
	const key = appId || botId || "default";
	// MERGE, don't replace: a registration that omits a field (e.g. an older
	// client that doesn't send signing_secret) must NOT wipe a value a previous
	// registration set — otherwise every restart of such a client drops the
	// per-app signing secret and the app's events fall back to the global secret
	// and 401. Always refresh the bot_token (it may rotate); preserve the rest.
	const prev = bots[key] ?? {};
	bots[key] = {
		bot_token: botToken,
		bot_id: botId ?? prev.bot_id,
		signing_secret: signingSecret ?? prev.signing_secret,
		app_id: appId ?? prev.app_id,
	};

	await storage.putSlackWorkspace(workspaceId, {
		// Keep legacy fields reflecting the just-registered bot for back-compat
		// readers; `bots` is authoritative.
		bot_token: botToken,
		bot_id: botId,
		bots,
	});
	return { status: 200, body: { ok: true, workspace_id: workspaceId, bot_id: botId, app_id: appId } };
}
