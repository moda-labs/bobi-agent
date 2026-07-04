export interface NormalizedEvent {
	v: 2;
	id: string;
	source: string;
	type: string;
	timestamp: string;
	topics: string[];
	delivery: "chat" | "bulk";
	text: string;
	// Channel-agnostic reply address (#618). Set by chat adapters; the agent
	// echoes it back verbatim to /channels/send (via `bobi reply`) instead of
	// assembling platform-specific routing fields. See conversation.ts.
	conversation?: string;
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

// A trust bubble. Minted once per named agent start; every deployment of that
// instance JOINs it. The key signs publishes and join-registrations to prove
// bubble membership. See bobi/config.py:load_or_mint_bubble.
export interface BubbleRecord {
	id: string;
	key: string;
	created_at?: string;
}

// A server-verified grant that a bubble may subscribe to / receive a global
// webhook resource topic (#488). The event server verifies an upstream
// credential ONCE (GitHub repo read / Linear team read / Slack workspace
// registration) and stores ONLY this grant — never the credential. The grant,
// not the subscription index, is the source of truth at delivery time.
//
// Shaped now for the future account system so we never bake "bubble == user":
// `account_id` is null in the MVP (an account layer fills it later) and a grant
// is keyed by `bubble_id`.
export interface ResourceGrant {
	id: string;
	account_id: string | null; // null in MVP; account layer fills it later
	bubble_id: string;
	service: "github" | "linear" | "slack";
	resource: string;
	granted_by: "upstream_token_verification" | "test_seed";
	// Linear: the team's organization id, recorded so a future fix can
	// disambiguate the workspace-ambiguous `linear:TEAM` topic (#488 §4). Null
	// / absent for github + slack.
	organization_id?: string | null;
	created_at: string;
	expires_at: string | null; // null = no expiry in MVP (not enforced yet)
}

export interface SlackBotRecord {
	bot_token: string;
	bot_id?: string;
	bot_user_id?: string;
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
 * Every bot id we own in this workspace, across ALL registered apps (+ legacy).
 * The self-reply filter skips a message authored by ANY of these — not just the
 * RECEIVING app's bot. When two of our apps share a channel, Slack delivers each
 * app's events to BOTH apps' webhooks, so one bot's message arrives via the
 * other bot's webhook (a different api_app_id); keying "self" to the receiving
 * app alone let that message through and it looped.
 */
export function workspaceBotIds(ws: SlackWorkspaceRecord | null | undefined): Set<string> {
	const ids = new Set<string>();
	if (!ws) return ids;
	if (ws.bots) for (const b of Object.values(ws.bots)) if (b.bot_id) ids.add(b.bot_id);
	if (ws.bot_id) ids.add(ws.bot_id);
	return ids;
}

/**
 * Slack user ids for bot users belonging to the receiving app. Slack emits a
 * channel mention as both app_mention and message.*, and only the app_mention
 * should reach chat delivery; otherwise the drain posts two placeholders for
 * one ts. Self-authorship filtering is workspace-wide; mention dedupe must stay
 * app-scoped so one bot does not swallow thread replies mentioning another.
 */
export function workspaceBotUserIds(
	ws: SlackWorkspaceRecord | null | undefined,
	apiAppId: string,
): Set<string> {
	const ids = new Set<string>();
	if (!ws?.bots) return ids;
	if (apiAppId && ws.bots[apiAppId]?.bot_user_id) {
		ids.add(ws.bots[apiAppId].bot_user_id);
		return ids;
	}
	const bots = Object.values(ws.bots);
	if (bots.length === 1 && bots[0].bot_user_id) {
		ids.add(bots[0].bot_user_id);
	}
	return ids;
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
	// Resource grants (#488). `getDeploymentById` is needed by the delivery
	// filter to resolve a candidate's bubble before checking its grant.
	putResourceGrant(grant: ResourceGrant): Promise<void>;
	hasResourceGrant(service: string, resource: string, bubbleId: string): Promise<boolean>;
	getDeploymentById(id: string): Promise<DeploymentRecord | null>;
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
	// to the body when POSTing (see the Python event publisher) — without
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
		// Reply address survives publish/forward: an agent re-publishing a chat
		// event must not strip the field the receiver needs for `bobi reply`.
		...(typeof body.conversation === "string" && body.conversation
			? { conversation: body.conversation }
			: {}),
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
import { parseConversation } from "./conversation";

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

// Normalize a resource string so the grant key and the topic never diverge on
// case/alias (#488 §3.3). github `owner/repo` is lowercased and a trailing
// `.git` stripped (matching the git-remote slug the adapter produces); linear
// team keys and slack team ids are left verbatim (case-significant upstream).
// Applied IDENTICALLY at authorize time and at topic parsing.
export function normalizeResource(service: string, resource: string): string {
	let r = resource.trim();
	if (service === "github") {
		r = r.toLowerCase();
		if (r.endsWith(".git")) r = r.slice(0, -4);
	}
	return r;
}

// Parse a GLOBAL topic key into its {service, resource} for a grant lookup —
// the inverse of how topics are built, and the single helper both enforcement
// layers (registration + delivery) use so they can never disagree. Split on the
// FIRST `:` (github `owner/repo` and linear keys never contain a `:`). Slack
// topics may be `slack:{team}` OR `slack:{team}:{channel}`; the grant is keyed
// on the TEAM id, so the channel segment is dropped here. Returns null for a
// non-global or malformed key.
export function parseGlobalTopic(key: string): { service: string; resource: string } | null {
	if (!isGlobalTopic(key)) return null;
	const idx = key.indexOf(":");
	if (idx <= 0) return null;
	const service = key.slice(0, idx);
	let resource = key.slice(idx + 1);
	if (!resource) return null;
	// Slack channel-scoped topic — the grant gates the whole team, so reduce
	// `team:channel` to `team` before normalizing.
	if (service === "slack") {
		const c = resource.indexOf(":");
		if (c >= 0) resource = resource.slice(0, c);
	}
	resource = normalizeResource(service, resource);
	if (!resource) return null;
	return { service, resource };
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
// reproduce. See bobi/events/publish.py and client.py for the signer.
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
	return slackApi(botToken, "chat.postMessage", body);
}

async function slackApi(
	botToken: string,
	method: string,
	payload: Record<string, unknown>,
): Promise<SlackSendResult> {
	const resp = await fetch(`https://slack.com/api/${method}`, {
		method: "POST",
		headers: {
			Authorization: `Bearer ${botToken}`,
			"Content-Type": "application/json",
		},
		body: JSON.stringify(payload),
	});
	return (await resp.json()) as SlackSendResult;
}

export async function updateSlackMessage(
	botToken: string,
	channel: string,
	ts: string,
	text: string,
): Promise<SlackSendResult> {
	return slackApi(botToken, "chat.update", { channel, ts, text });
}

// Clear (or set) the assistant thread status. Used after a gateway edit so
// the "is thinking..." indicator does not linger until Slack's ~2min expiry.
// Best-effort: only DM threads support it, so failures are ignored.
export async function setSlackThreadStatus(
	botToken: string,
	channel: string,
	threadTs: string,
	status: string,
): Promise<void> {
	try {
		await slackApi(botToken, "assistant.threads.setStatus", {
			channel_id: channel,
			thread_ts: threadTs,
			status,
		});
	} catch {
		// non-fatal
	}
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
	let selfBotIds: Set<string> | undefined;
	let selfBotUserIds: Set<string> | undefined;
	if (teamId) {
		const ws = await storage.getSlackWorkspace(teamId);
		// Skip messages authored by ANY of our bots in this workspace — not just
		// the bot of the app that received this webhook. Two of our apps in one
		// channel each receive the other's messages (with their own api_app_id),
		// so a per-receiving-app "self" id let one bot's message loop in via the
		// other's webhook.
		selfBotIds = workspaceBotIds(ws);
		selfBotUserIds = workspaceBotUserIds(ws, apiAppId);
	}

	const result = normalizeSlackWebhook(payload, selfBotIds, selfBotUserIds);

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
//     returns the key ONCE (over TLS). Used only by named agent start's
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

	// Enforcement layer 1 (#488): every GLOBAL resource topic in the request must
	// have a matching grant for this bubble. Hard-reject the WHOLE request (no
	// partial write) so the client surfaces a configuration error rather than
	// silently running degraded (reviewer decision Q2). Delivery (§3.5) remains
	// the fail-closed boundary regardless. Non-global topics are never gated, so
	// the bootstrap MINT (`_bootstrap`) is unaffected.
	const unauthorized = await unauthorizedGlobalTopics(storage, bubble.id, subscriptions);
	if (unauthorized.length) {
		return { status: 400, body: { error: "unauthorized_topics", topics: unauthorized } };
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

	const replaceSubs = body.replace as string[] | undefined;
	const addSubs = body.add as string[] | undefined;
	if (replaceSubs !== undefined && !replaceSubs.length) {
		return { status: 400, body: { error: "replace[] must not be empty" } };
	}
	if (replaceSubs === undefined && !addSubs?.length) {
		return { status: 400, body: { error: "add[] or replace[] required" } };
	}
	const newSubs = replaceSubs !== undefined ? replaceSubs : addSubs!;

	// Enforcement layer 1 (#488): same grant gate as registration — a global
	// resource topic added here needs a matching grant for the deployment's
	// bubble. Reject the whole update (no partial write) on any ungranted topic.
	const unauthorized = await unauthorizedGlobalTopics(storage, deployment.bubble_id, newSubs);
	if (unauthorized.length) {
		return { status: 400, body: { error: "unauthorized_topics", topics: unauthorized } };
	}

	// Namespace from the AUTHENTICATED deployment record's bubble — never from a
	// client-supplied bubble_id — so update-subscriptions can't escape the
	// deployment's bubble. Same keying as registration (namespaceSubKey).
	if (replaceSubs !== undefined) {
		const desired = [...new Set(replaceSubs)];
		const desiredSet = new Set(desired);
		let removed = 0;
		let added = 0;
		for (const sub of deployment.subscriptions) {
			if (!desiredSet.has(sub)) {
				await storage.removeSubscription(
					namespaceSubKey(deployment.bubble_id, sub),
					deploymentId,
				);
				removed++;
			}
		}
		for (const sub of desired) {
			if (!deployment.subscriptions.includes(sub)) added++;
			await storage.addSubscription(namespaceSubKey(deployment.bubble_id, sub), deploymentId);
		}
		deployment.subscriptions = desired;
		await storage.putDeployment(deployment);
		return { status: 200, body: { subscriptions: deployment.subscriptions, added, removed } };
	}

	let added = 0;
	for (const sub of addSubs!) {
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
	if (body.repo || body.team_key || body.workspace) {
		return { status: 400, body: { error: "global routing fields are webhook-only" } };
	}

	const event = createTopicEvent(topic, body, bubble.id);
	if (event.topics.some((key) => isGlobalTopic(key))) {
		return { status: 400, body: { error: "global topics are webhook-only" } };
	}
	const delivered = await storage.deliver(event);
	return { status: 200, body: { delivered_to: delivered } };
}

// ---------------------------------------------------------------------------
// Resource grants (#488) — verify an upstream credential ONCE, store a grant.
// ---------------------------------------------------------------------------

// Upstream hosts are fixed constants, NEVER client-supplied (SSRF guard).
const GITHUB_API = "https://api.github.com";
const LINEAR_API = "https://api.linear.app/graphql";

export interface VerifyResult {
	ok: boolean;
	// Linear: the team's organization id (recorded in the grant for the future
	// `linear:TEAM` disambiguation, §4). Undefined for github.
	organizationId?: string | null;
}

// GitHub bar (resolved, Q4): the MINIMUM read probe — `GET /repos/{owner}/{repo}`
// returns 2xx means the token can read the repo, which is all webhook delivery
// needs. We deliberately do NOT parse `private`/`permissions` (the Rev-2
// tightening was dropped); only the 2xx/non-2xx distinction gates the grant.
async function verifyGitHubAccess(resource: string, credential: string): Promise<VerifyResult> {
	const resp = await fetch(`${GITHUB_API}/repos/${resource}`, {
		headers: {
			Authorization: `Bearer ${credential}`,
			"User-Agent": "bobi-event-server",
			Accept: "application/vnd.github+json",
		},
	});
	return { ok: resp.ok };
}

// Linear (Q3): keep TEAM-KEY granularity — confirm the credential can see the
// SPECIFIC team it claims, not merely that the token is valid org-wide. Records
// the team's organization id for the future disambiguation (§4).
async function verifyLinearAccess(resource: string, credential: string): Promise<VerifyResult> {
	// `resource` is validated against a strict charset by the caller before this
	// runs, so it cannot break out of the GraphQL string literal.
	const query = `{ teams(filter:{key:{eq:"${resource}"}}){ nodes { id key organization { id } } } }`;
	const resp = await fetch(LINEAR_API, {
		method: "POST",
		headers: { Authorization: credential, "Content-Type": "application/json" },
		body: JSON.stringify({ query }),
	});
	if (!resp.ok) return { ok: false };
	const data = (await resp.json()) as {
		data?: { teams?: { nodes?: Array<{ key?: string; organization?: { id?: string } }> } };
	};
	const nodes = data?.data?.teams?.nodes ?? [];
	const match = Array.isArray(nodes) ? nodes.find((n) => n?.key === resource) : undefined;
	if (!match) return { ok: false };
	return { ok: true, organizationId: match.organization?.id ?? null };
}

// A linear team key as it appears in a topic — uppercase alnum plus `-`/`_`.
// Anchored so a malicious `resource` can never inject into the GraphQL string.
const LINEAR_KEY_RE = /^[A-Za-z0-9_-]+$/;
// owner/repo shape (each segment non-empty, no extra slashes).
const GITHUB_SLUG_RE = /^[^/\s]+\/[^/\s]+$/;

// POST /resources/authorize handler. `bubbleId` is the AUTHENTICATED bubble —
// the entry file rejects an unsigned / partial / bad-signature request with an
// opaque 403 BEFORE calling this (mandatory auth, mirroring how /slack/send is
// wired). Verifies the upstream credential once, then stores ONLY a grant.
//
// The credential is NEVER logged and NEVER stored: the route is excluded from
// body logging, and a verification failure logs `{service, reason}` only.
export async function handleAuthorizeResource(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId: string,
): Promise<HandlerResult> {
	const service = typeof body.service === "string" ? body.service : "";
	const rawResource = typeof body.resource === "string" ? body.resource.trim() : "";
	const credential = typeof body.credential === "string" ? body.credential : "";

	// Slack authorizes via the bubble-signed /slack/workspaces registration
	// (§6) — there is no separate verify call — so only github/linear reach here.
	if ((service !== "github" && service !== "linear") || !rawResource || !credential) {
		return { status: 400, body: { error: "invalid_request" } };
	}

	const resource = normalizeResource(service, rawResource);
	if (service === "github" && !GITHUB_SLUG_RE.test(resource)) {
		return { status: 400, body: { error: "invalid_request" } };
	}
	if (service === "linear" && !LINEAR_KEY_RE.test(resource)) {
		return { status: 400, body: { error: "invalid_request" } };
	}

	let result: VerifyResult;
	try {
		result = service === "github"
			? await verifyGitHubAccess(resource, credential)
			: await verifyLinearAccess(resource, credential);
	} catch (err) {
		// Reason only — NEVER the credential or the raw body.
		console.warn(`resource authorize error: service=${service} reason=${String(err)}`);
		return { status: 403, body: { error: "forbidden" } };
	}

	if (!result.ok) {
		console.warn(`resource authorize denied: service=${service}`);
		return { status: 403, body: { error: "forbidden" } };
	}

	const grant: ResourceGrant = {
		id: `${service}:${resource}:${bubbleId}`,
		account_id: null,
		bubble_id: bubbleId,
		service,
		resource,
		granted_by: "upstream_token_verification",
		organization_id: result.organizationId ?? null,
		created_at: new Date().toISOString(),
		expires_at: null,
	};
	await storage.putResourceGrant(grant);
	return { status: 200, body: { ok: true } };
}

export async function handleTestSeedResourceGrants(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId: string,
): Promise<HandlerResult> {
	const grants = Array.isArray(body.grants) ? body.grants : [];
	if (!bubbleId || grants.length === 0) {
		return { status: 400, body: { error: "invalid_request" } };
	}

	for (const raw of grants) {
		if (!raw || typeof raw !== "object") {
			return { status: 400, body: { error: "invalid_request" } };
		}
		const grant = raw as Record<string, unknown>;
		const service = typeof grant.service === "string" ? grant.service : "";
		const rawResource = typeof grant.resource === "string" ? grant.resource.trim() : "";
		if ((service !== "github" && service !== "linear" && service !== "slack") || !rawResource) {
			return { status: 400, body: { error: "invalid_request" } };
		}
		const resource = normalizeResource(service, rawResource);
		await storage.putResourceGrant({
			id: `${service}:${resource}:${bubbleId}`,
			account_id: null,
			bubble_id: bubbleId,
			service,
			resource,
			granted_by: "test_seed",
			organization_id: null,
			created_at: new Date().toISOString(),
			expires_at: null,
		});
	}

	return { status: 200, body: { ok: true, grants: grants.length } };
}

// The global resource topics in `subs` that the bubble does NOT currently hold a
// grant for (#488 enforcement layer 1). A malformed global key (no parseable
// service/resource) counts as unauthorized. Non-global topics are ignored.
export async function unauthorizedGlobalTopics(
	storage: StorageAdapter,
	bubbleId: string,
	subs: string[],
): Promise<string[]> {
	const bad: string[] = [];
	for (const sub of subs) {
		if (!isGlobalTopic(sub)) continue;
		const parsed = parseGlobalTopic(sub);
		if (!parsed) {
			bad.push(sub);
			continue;
		}
		if (!(await storage.hasResourceGrant(parsed.service, parsed.resource, bubbleId))) {
			bad.push(sub);
		}
	}
	return bad;
}

// Resolve the deployment IDs an event should fan out to, applying the #488
// resource-grant filter to GLOBAL topics. `subscribersForKey` returns the
// subscriber ids the runtime has indexed for a subscription key. A non-global
// key admits every subscriber (the common bubble-scoped path pays nothing); a
// global key admits a subscriber ONLY if its bubble currently holds a matching
// grant — so a stale index entry for a revoked / never-granted bubble is dropped
// (fail-closed; delivery is the authoritative boundary, §3.5).
export async function admittedDeploymentIds(
	storage: StorageAdapter,
	event: NormalizedEvent,
	subscribersForKey: (key: string) => Promise<Iterable<string>>,
): Promise<Set<string>> {
	const admitted = new Set<string>();
	for (const key of subscriptionKeysForEvent(event)) {
		const ids = await subscribersForKey(key);
		if (isGlobalTopic(key)) {
			const parsed = parseGlobalTopic(key);
			if (!parsed) continue;
			for (const id of ids) {
				if (admitted.has(id)) continue;
				const dep = await storage.getDeploymentById(id);
				if (dep && (await storage.hasResourceGrant(parsed.service, parsed.resource, dep.bubble_id))) {
					admitted.add(id);
				}
			}
		} else {
			for (const id of ids) admitted.add(id);
		}
	}
	return admitted;
}

// Storage key for a bubble-scoped Slack workspace registration. Outbound
// /slack/send reads ONLY this key — never the bare global `slack_workspace:<id>`
// that drives inbound self-reply — so one bubble can never send through a
// workspace another bubble registered. With the KV adapter's `slack_workspace:`
// prefix this yields `slack_workspace:${bubbleId}:${workspaceId}`.
export function bubbleScopedWorkspaceKey(bubbleId: string, workspaceId: string): string {
	return `${bubbleId}:${workspaceId}`;
}

// Resolve the sending bot's token for an outbound Slack send. Bubble-scoped
// lookup ONLY - no fallback to the global workspace store. A workspace
// registered by another bubble (or only inbound-registered) is absent here and
// yields the same empty result as a truly unknown workspace, so an attacker
// can't probe which workspaces another bubble registered.
//
// Pick the sending bot's token: by app_id, then bot_id, then the single
// registered bot, then the legacy workspace token. With several bots in a
// workspace, "the workspace's bot_token" is ambiguous.
async function resolveSlackSendToken(
	storage: StorageAdapter,
	bubbleId: string,
	workspaceId: string,
	appId: string,
	botId: string,
): Promise<string> {
	if (!workspaceId) return "";
	const ws = await storage.getSlackWorkspace(bubbleScopedWorkspaceKey(bubbleId, workspaceId));
	if (!ws) return "";
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
	return botToken;
}

// `bubbleId` is the AUTHENTICATED bubble (the entry files reject an unsigned or
// bad-signature request with 403 before calling this). The Slack credentials
// are looked up under that bubble's scope only — the outbound tenancy boundary.
export async function handleSlackSend(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId: string,
): Promise<HandlerResult> {
	const channel = body.channel as string;
	const text = body.text as string;
	if (!channel || !text) {
		return { status: 400, body: { error: "channel and text required" } };
	}

	const botToken = await resolveSlackSendToken(
		storage,
		bubbleId,
		(body.workspace as string) || "",
		(body.app_id as string) || "",
		(body.bot_id as string) || "",
	);
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

// Channel-agnostic outbound send (#618): { conversation, text, mode?, edit_ref? }.
// Parses the conversation reference and delegates to the channel's send
// machinery - Slack is the only channel today. `mode: "update"` edits the
// message identified by `edit_ref` (e.g. a placeholder) instead of posting.
// Same auth contract as handleSlackSend: `bubbleId` is the authenticated
// bubble, and credentials resolve only under its scope.
//
// Text is sent raw: markdown->mrkdwn conversion lives client-side
// (bobi/slack.py format_slack_message), matching /slack/send. The Phase 2
// Chat SDK migration (#190) moves conversion into the gateway; until then
// callers must convert before POSTing.
export async function handleChannelsSend(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId: string,
): Promise<HandlerResult> {
	const ref = body.conversation;
	const text = body.text;
	// Explicit string checks: a non-string conversation (array, number) must
	// be a 400, not a TypeError escaping the handler as a 500.
	if (typeof ref !== "string" || !ref || typeof text !== "string" || !text) {
		return { status: 400, body: { error: "conversation and text required" } };
	}
	const conv = parseConversation(ref);
	if (!conv) {
		return { status: 400, body: { error: "invalid conversation reference" } };
	}
	if (conv.source !== "slack") {
		return { status: 400, body: { error: `unsupported channel: ${conv.source}` } };
	}

	const mode = (body.mode as string) || "post";
	if (mode !== "post" && mode !== "update") {
		return { status: 400, body: { error: `invalid mode: ${mode}` } };
	}
	const editRef = (body.edit_ref as string) || "";
	if (mode === "update" && !editRef) {
		return { status: 400, body: { error: "edit_ref required for mode: update" } };
	}

	const botToken = await resolveSlackSendToken(
		storage,
		bubbleId,
		conv.scope,
		(body.app_id as string) || "",
		(body.bot_id as string) || "",
	);
	if (!botToken) {
		return { status: 400, body: { error: "no bot token for workspace" } };
	}

	let result;
	try {
		result = mode === "update"
			? await updateSlackMessage(botToken, conv.chatId, editRef, text)
			: await sendSlackMessage(botToken, conv.chatId, text, conv.threadId);
	} catch (err) {
		return { status: 502, body: { ok: false, error: String(err) } };
	}
	if (!result.ok) {
		return { status: 502, body: { ok: false, error: result.error } };
	}
	// Replacing a placeholder must also clear the "is thinking..." status the
	// placeholder flow set, matching the CLI edit path (slack-reply --edit).
	if (mode === "update" && conv.threadId) {
		await setSlackThreadStatus(botToken, conv.chatId, conv.threadId, "");
	}
	return { status: 200, body: { ok: true, ts: result.ts } };
}

// `bubbleId`, when present, is the AUTHENTICATED bubble that signed the
// registration. The global record (bare workspaceId) is ALWAYS written so
// inbound webhook self-reply loop prevention keeps working for any client —
// signed or not. The bubble-scoped record is written ONLY for an authenticated
// bubble, and is the only store outbound /slack/send reads.
export async function handleSlackWorkspaceRegister(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId?: string,
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
	let botUserId = (body.bot_user_id as string) || undefined;
	let appId = (body.app_id as string) || undefined;
	const signingSecret = (body.signing_secret as string) || undefined;
	if (bubbleId) {
		try {
			const resp = await fetch("https://slack.com/api/auth.test", {
				headers: { Authorization: `Bearer ${botToken}` },
			});
			const data = (await resp.json()) as Record<string, unknown>;
			if (!data.ok || data.team_id !== workspaceId) {
				return { status: 403, body: { error: "forbidden" } };
			}
			if (data.bot_id) botId = data.bot_id as string;
		} catch {
			return { status: 403, body: { error: "forbidden" } };
		}
	} else if (!botId) {
		try {
			const resp = await fetch("https://slack.com/api/auth.test", {
				headers: { Authorization: `Bearer ${botToken}` },
			});
			const data = (await resp.json()) as Record<string, unknown>;
			if (data.ok) {
				botId = data.bot_id as string;
				botUserId = (data.user_id as string) || botUserId;
			}
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
	// Pure: applied INDEPENDENTLY to the global and the bubble-scoped record so
	// the two stores accrete in parallel and never alias.
	const mergeBot = (existing: SlackWorkspaceRecord | null): SlackWorkspaceRecord => {
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
			bot_user_id: botUserId ?? prev.bot_user_id,
			signing_secret: signingSecret ?? prev.signing_secret,
			app_id: appId ?? prev.app_id,
		};
		return {
			// Keep legacy fields reflecting the just-registered bot for back-compat
			// readers; `bots` is authoritative.
			bot_token: botToken,
			bot_id: botId,
			bots,
		};
	};

	// Global record — drives inbound webhook self-reply loop prevention. ALWAYS
	// written so a client that doesn't (yet) sign keeps loop prevention.
	const globalExisting = await storage.getSlackWorkspace(workspaceId);
	await storage.putSlackWorkspace(workspaceId, mergeBot(globalExisting));

	// Bubble-scoped record — the ONLY store outbound /slack/send reads. Written
	// only for an authenticated bubble, so that bubble (and no other) can send
	// through this workspace.
	if (bubbleId) {
		const scopedKey = bubbleScopedWorkspaceKey(bubbleId, workspaceId);
		const scopedExisting = await storage.getSlackWorkspace(scopedKey);
		await storage.putSlackWorkspace(scopedKey, mergeBot(scopedExisting));

		// Slack convergence (#488 §6): the signed registration — proving
		// possession of the bot token + signing secret — IS the proof of access,
		// so it doubles as the slack resource grant. `slack:{teamId}` inbound
		// delivery is then grant-filtered exactly like github/linear. Idempotent.
		await storage.putResourceGrant({
			id: `slack:${workspaceId}:${bubbleId}`,
			account_id: null,
			bubble_id: bubbleId,
			service: "slack",
			resource: workspaceId,
			granted_by: "upstream_token_verification",
			organization_id: null,
			created_at: new Date().toISOString(),
			expires_at: null,
		});
	}

	return {
		status: 200,
		body: { ok: true, workspace_id: workspaceId, bot_id: botId, bot_user_id: botUserId, app_id: appId },
	};
}
