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
	service: "github" | "linear" | "slack" | "whatsapp";
	resource: string;
	granted_by: "upstream_token_verification" | "test_seed";
	// Linear: the team's organization id, recorded so a future fix can
	// disambiguate the workspace-ambiguous `linear:TEAM` topic (#488 §4). Null
	// / absent for github + slack.
	organization_id?: string | null;
	created_at: string;
	expires_at: string | null; // null = no expiry in MVP (not enforced yet)
}

// A scoped ingest token (#640): a revocable credential bound to one
// (bubble, topic) pair, minted by the instance for external systems that can
// only send static headers (alerting, CI, SaaS webhooks). The server stores
// ONLY the SHA-256 hash of the token — the token itself transits exactly once,
// in the mint response, over TLS. A leaked token exposes one topic's ingress
// in one bubble, never bubble membership or the bubble key.
export interface IngestTokenRecord {
	id: string;
	bubble_id: string;
	topic: string;
	token_hash: string;
	name?: string;
	created_at: string;
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
	// Generic per-channel state (#656): credential records and message-window
	// bookkeeping for channels beyond Slack. One store so a new channel never
	// adds storage methods (the Slack workspace store predates this).
	getChannelState(key: string): Promise<Record<string, unknown> | null>;
	putChannelState(key: string, value: Record<string, unknown>): Promise<void>;
	// Scoped ingest tokens (#640). Lookup at request time is by token HASH —
	// the store never sees a plaintext token. Listing is per-bubble (revoke
	// resolves the id inside the caller's own list, so one bubble can never
	// address another bubble's tokens).
	putIngestToken(record: IngestTokenRecord): Promise<void>;
	getIngestTokenByHash(hash: string): Promise<IngestTokenRecord | null>;
	listIngestTokens(bubbleId: string): Promise<IngestTokenRecord[]>;
	deleteIngestToken(record: IngestTokenRecord): Promise<void>;
}

// ---------------------------------------------------------------------------
// Handler result — transport-agnostic response that entry files convert to
// their native response type (Response for CF workers, res.end for Node).
// ---------------------------------------------------------------------------

export interface HandlerResult {
	status: number;
	body: unknown;
}

// The bare topic plus its source-qualified spelling (e.g. "monitor/support.email")
// so subscriptions written either way match (#235). The single helper both
// createTopicEvent and createIngestEvent route through, so the two spellings
// can never drift between the signed-publish and token-ingest paths.
export function sourceQualifiedTopics(topic: string, source?: string): string[] {
	const topics = [topic];
	if (source && !topic.startsWith(`${source}/`)) {
		topics.push(`${source}/${topic}`);
	}
	return topics;
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
		topics.push(...sourceQualifiedTopics(topic, body.source as string | undefined));
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
import { bridgeSlackWebhook } from "./adapters/chat-sdk-slack";
import { normalizeWhatsAppWebhook } from "./adapters/whatsapp";
import { chunkForChannel, getChannelAdapter, slackApiUrl, truncateForChannel, whatsappApi, type OutboundFile } from "./channels";
import { parseConversation, type Conversation } from "./conversation";

export { normalizeGitHubWebhook as normalizeGitHubPayload } from "./adapters/github";
export { normalizeLinearWebhook as normalizeLinearPayload } from "./adapters/linear";

// ---------------------------------------------------------------------------
// Routing — topics-based (v2)
// ---------------------------------------------------------------------------

// Webhook resource topics that stay GLOBAL (cross-bubble) in v1. Inbound
// webhooks fan out to every subscribing bubble regardless of bubble — an
// accepted cross-tenant read hole, to be closed by #239 (inbound subscription
// auth). Slack inbound rides this path, so it keeps working. Everything else
// (inbox/*, reply/*, monitor/*, agent/*, custom topics) is bubble-scoped.
const GLOBAL_TOPIC_PREFIXES = ["github:", "linear:", "slack:", "whatsapp:"];

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

function bytesToHex(buffer: ArrayBuffer): string {
	return Array.from(new Uint8Array(buffer))
		.map((b) => b.toString(16).padStart(2, "0"))
		.join("");
}

async function hmacSha256Hex(secret: string, data: Uint8Array | string): Promise<string> {
	const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
	const key = await crypto.subtle.importKey(
		"raw",
		new TextEncoder().encode(secret),
		{ name: "HMAC", hash: "SHA-256" },
		false,
		["sign"],
	);
	return bytesToHex(await crypto.subtle.sign("HMAC", key, bytes));
}

// SHA-256 hex digest — the one-way transform between a plaintext ingest token
// (on the wire) and the stored token_hash. Lookup by digest also gives a
// constant-time-safe comparison for free: the attacker-controlled token is
// hashed before any store access, so no stored byte is ever compared
// positionally against attacker input.
export async function sha256Hex(data: string): Promise<string> {
	return bytesToHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(data)));
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

// Linear replay window for the signed webhookTimestamp (ms since epoch),
// matching the ±300s the slack and bubble verifiers use.
const LINEAR_REPLAY_WINDOW_MS = 300_000;

// Linear signs the raw body with HMAC-SHA256 (hex) in the `linear-signature`
// header. `webhookTimestamp` is the payload's signed timestamp field; like
// verifySlackSignature, the freshness window lives INSIDE the verifier so a
// direct caller cannot get signature validation without replay protection.
// FAIL CLOSED on a missing or non-numeric timestamp: the signature covers the
// body, so its absence in a signed payload still leaves the request replayable
// forever if admitted.
export async function verifyLinearSignature(
	secret: string,
	body: string,
	signatureHeader: string,
	webhookTimestamp: unknown,
): Promise<boolean> {
	if (!signatureHeader) return false;
	if (
		typeof webhookTimestamp !== "number" ||
		Math.abs(Date.now() - webhookTimestamp) > LINEAR_REPLAY_WINDOW_MS
	) {
		return false;
	}
	const expected = await hmacSha256Hex(secret, body);
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
	// Inbound webhook pipeline (#639): requests admitted because the provider's
	// secret is unconfigured, and requests rejected by a source's verify slot.
	// Both surface on /health so a provider silently running unverified — or a
	// rotated secret 401-flooding — is visible without grepping logs.
	webhook_unverified: number;
	webhook_bad_signature: number;
}

const _rejectionCounters: AuthRejectionCounters = {
	bad_signature: 0,
	stale_timestamp: 0,
	unknown_bubble: 0,
	webhook_unverified: 0,
	webhook_bad_signature: 0,
};

export function getAuthRejectionCounters(): AuthRejectionCounters {
	return { ..._rejectionCounters };
}

export function resetAuthRejectionCounters(): void {
	_rejectionCounters.bad_signature = 0;
	_rejectionCounters.stale_timestamp = 0;
	_rejectionCounters.unknown_bubble = 0;
	_rejectionCounters.webhook_unverified = 0;
	_rejectionCounters.webhook_bad_signature = 0;
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

// `body` is the raw webhook JSON string; `payload` its parsed form (the
// pipeline parses once before verification and passes both). Normalization
// runs through the Chat SDK bridge (#628) — the hand-rolled
// normalizeSlackWebhook remains only as the golden parity reference until
// the bridge has soaked (#629).
export async function handleSlackWebhook(
	storage: StorageAdapter,
	body: string,
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

	const result = bridgeSlackWebhook(body, selfBotIds, selfBotUserIds, payload);

	if (result.challenge !== undefined) {
		return { status: 200, body: { challenge: result.challenge } };
	}
	if (result.skip || !result.event) {
		return { status: 200, body: { ok: true } };
	}

	const delivered = await storage.deliver(result.event);
	return { status: 200, body: { delivered_to: delivered } };
}

// ---------------------------------------------------------------------------
// Inbound webhook pipeline (#639)
//
// One pipeline for every inbound webhook source, shared by both transports
// (the Worker and the local server):
//
//   route (/webhooks/<source>) -> verifier -> normalizer -> deliver()
//
// A source registers a REQUIRED verify slot plus a handler; the verify field
// is non-optional by type, so a route cannot exist without verification by
// construction. Transport entry files call handleWebhookRequest and never
// stitch verification per-route themselves.
// ---------------------------------------------------------------------------

// Transport-neutral view of the inbound request. `rawBody` is the exact wire
// bytes (signatures cover them — never a re-serialization); `header` is a
// case-insensitive lookup returning "" when absent; `subpath` is the matched
// route's topic remainder from matchWebhookSource ("" for exact provider
// routes). Required — a transport cannot forget to thread it.
export interface InboundWebhookRequest {
	rawBody: string;
	header(name: string): string;
	subpath: string;
}

// Provider verification secrets, resolved by the transport (Worker env vars /
// BOBI_ES_* process env), keyed by source name. An empty secret means
// verification is not configured for that provider and its webhooks are
// admitted unverified — the pre-#639 contract for github and slack, kept for
// zero-config local development. Unverified admission is counted on /health
// (webhook_unverified) so a misconfigured public server is visible.
export interface WebhookSecrets {
	github?: string;
	slack?: string;
	linear?: string;
	/** Meta app secret - verifies X-Hub-Signature-256 on POSTed events. */
	whatsapp?: string;
	/** Operator-chosen token echoed back in Meta's GET subscribe handshake. */
	whatsappVerifyToken?: string;
}

// Per-request pipeline context: the seam a verify slot uses to hand its
// verified principal to the handler. The ingest verifier stashes the token
// record here so its handler delivers on the token's binding — never on
// anything re-derived from the request.
export interface WebhookRequestContext {
	ingestToken?: IngestTokenRecord;
}

interface WebhookSource {
	// GET subscription handshake (#656): some providers (Meta) verify the
	// webhook URL with a GET carrying a challenge to echo back as RAW text.
	// Registered here so both transports serve it from one definition; absent
	// means the source has no GET surface (the transport 404s).
	handshake?(
		query: (name: string) => string,
		secrets: WebhookSecrets,
	): WebhookHandshakeResult;
	// Registered route shape: exact (`/webhooks/<source>`, the default) or
	// topic (`/webhooks/<source>/<topic>` with a required, slash-bearing
	// remainder). Part of the single route grammar in matchWebhookSource.
	topicRoute?: true;
	// Reject bodies longer than this BEFORE JSON.parse (413). Measured in
	// UTF-16 code units of the raw body — a lower bound on bytes, so nothing
	// under the cap is ever falsely rejected and any parse-scale attack body
	// is caught. Set for sources whose senders are untrusted at request time
	// (ingest); provider bodies are bounded upstream by their platforms.
	maxBodyBytes?: number;
	// Short-circuit responses that must run BEFORE signature verification.
	// Slack needs this: url_verification carries no signing headers (and its
	// retries must still answer the challenge), and retried event deliveries
	// dedup to {ok} without reprocessing.
	preVerify?(
		req: InboundWebhookRequest,
		payload: Record<string, unknown>,
	): HandlerResult | null;
	// REQUIRED verification slot — null admits the request, a HandlerResult
	// rejects it. Runs over the exact wire bytes. `secret` is the source's own
	// entry from WebhookSecrets, resolved by the pipeline so a verifier can
	// never read another provider's key.
	verify(
		storage: StorageAdapter,
		req: InboundWebhookRequest,
		payload: Record<string, unknown>,
		secret: string,
		ctx: WebhookRequestContext,
	): Promise<HandlerResult | null>;
	// Normalize + deliver.
	handle(
		storage: StorageAdapter,
		req: InboundWebhookRequest,
		payload: Record<string, unknown>,
		ctx: WebhookRequestContext,
	): Promise<HandlerResult>;
}

const INVALID_SIGNATURE: HandlerResult = { status: 401, body: { error: "invalid signature" } };
const INVALID_JSON: HandlerResult = { status: 400, body: { error: "invalid JSON" } };
// Ingest rejections are an opaque 403 (per #640 acceptance): missing, unknown,
// revoked, and wrong-topic tokens are indistinguishable to the caller.
const INGEST_FORBIDDEN: HandlerResult = { status: 403, body: { error: "forbidden" } };

// Ingest request-size cap. Generic publishes have no explicit byte cap today
// (they are bubble-signed, so the publisher is trusted); ingest is the first
// surface where the sender holds only a topic-scoped credential, so it gets
// an explicit one. Sized for alerting/CI/SaaS webhook payloads with headroom.
export const INGEST_MAX_BODY_BYTES = 256 * 1024;

// Per-token fixed-window rate limit. In-memory: authoritative on the local
// server (single process); per-isolate on the Worker, where it still bounds
// abuse per point of presence — a shared Durable Object limiter is deliberate
// non-scope for #640.
export const INGEST_RATE_LIMIT = 60;
export const INGEST_RATE_WINDOW_MS = 60_000;
const _ingestRateWindows = new Map<string, { start: number; count: number }>();

// Entries for tokens that stop sending (revoked, retired CI tokens) would
// otherwise accrete forever in this long-lived process; sweep expired windows
// once the map is large. O(n) only when the sweep fires.
const INGEST_RATE_SWEEP_THRESHOLD = 1024;

function ingestRateAllow(tokenId: string): boolean {
	const now = Date.now();
	if (_ingestRateWindows.size > INGEST_RATE_SWEEP_THRESHOLD) {
		for (const [id, w] of _ingestRateWindows) {
			if (now - w.start >= INGEST_RATE_WINDOW_MS) _ingestRateWindows.delete(id);
		}
	}
	const win = _ingestRateWindows.get(tokenId);
	if (!win || now - win.start >= INGEST_RATE_WINDOW_MS) {
		_ingestRateWindows.set(tokenId, { start: now, count: 1 });
		return true;
	}
	win.count++;
	return win.count <= INGEST_RATE_LIMIT;
}

export function resetIngestRateLimiter(): void {
	_ingestRateWindows.clear();
}

// Build the event an ingest request delivers: the raw JSON body rides as the
// payload with its top-level primitives mirrored into `fields`, on exactly the
// token's bound topic (bubble-scoped). Deliberately NOT createTopicEvent: that
// helper trusts routing fields in the body (repo/team_key/workspace → global
// topics), and an ingest body is attacker-adjacent external input that must
// never influence routing beyond the token's binding.
export function createIngestEvent(
	topic: string,
	body: Record<string, unknown>,
	bubbleId: string,
): NormalizedEvent {
	const fields: Record<string, string | number | boolean> = {};
	for (const [k, v] of Object.entries(body)) {
		if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
			fields[k] = v;
		}
	}
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "ingest",
		type: topic,
		timestamp: new Date().toISOString(),
		// Bare topic plus the source-qualified form, mirroring createTopicEvent's
		// fallback (#235), so both subscription spellings match.
		topics: [topic, `ingest/${topic}`],
		delivery: "bulk",
		text: typeof body.text === "string" ? body.text : "",
		fields,
		payload: body,
		bubble_id: bubbleId,
	};
}

// A GET-handshake response. `text` is written as the RAW response body
// (text/plain) - Meta compares the echoed challenge byte-for-byte, so it must
// never be JSON-quoted.
export interface WebhookHandshakeResult {
	status: number;
	text: string;
}

// Serve a provider's GET verification handshake, if it defines one. Returns
// null when the source has no handshake (or is unregistered) - the transport
// falls through to its native 404.
export function handleWebhookHandshake(
	source: string,
	query: (name: string) => string,
	secrets: WebhookSecrets,
): WebhookHandshakeResult | null {
	const def = WEBHOOK_SOURCES[source];
	if (!def?.handshake) return null;
	return def.handshake(query, secrets);
}

const WEBHOOK_SOURCES: Record<string, WebhookSource> = {
	github: {
		async verify(_storage, req, _payload, secret) {
			if (!secret) return unverifiedAdmission();
			const valid = await verifyGitHubSignature(
				secret,
				new TextEncoder().encode(req.rawBody),
				req.header("x-hub-signature-256"),
			);
			return valid ? null : INVALID_SIGNATURE;
		},
		handle(storage, req, payload) {
			return handleGitHubWebhook(
				storage,
				req.header("x-github-event") || "unknown",
				req.header("x-github-delivery") || crypto.randomUUID(),
				payload,
			);
		},
	},

	linear: {
		async verify(_storage, req, payload, secret) {
			if (!secret) return unverifiedAdmission();
			const valid = await verifyLinearSignature(
				secret,
				req.rawBody,
				req.header("linear-signature"),
				payload.webhookTimestamp,
			);
			return valid ? null : INVALID_SIGNATURE;
		},
		handle(storage, _req, payload) {
			return handleLinearWebhook(storage, payload);
		},
	},

	slack: {
		preVerify(req, payload) {
			// url_verification must run before BOTH the retry short-circuit and the
			// signature check: it carries no signing headers, and Slack retries a
			// failed handshake with x-slack-retry-num set — swallowing retries here
			// would leave the request URL permanently unverified.
			if (payload.type === "url_verification") {
				return { status: 200, body: { challenge: payload.challenge } };
			}
			// Dedup retried EVENT deliveries so the agent doesn't double-process.
			if (req.header("x-slack-retry-num")) {
				return { status: 200, body: { ok: true } };
			}
			return null;
		},
		async verify(storage, req, payload, secret) {
			// Verify against the AUTHORING app's signing secret (resolved by
			// api_app_id), falling back to the global secret for legacy single-app
			// deployments. A second app in the workspace signs with its OWN secret;
			// validating only the global one 401'd it (and dropped its login DM).
			const signingSecret = await slackSigningSecretFor(storage, payload, secret);
			if (!signingSecret) return unverifiedAdmission();
			const valid = await verifySlackSignature(
				signingSecret,
				req.header("x-slack-request-timestamp"),
				req.rawBody,
				req.header("x-slack-signature"),
			);
			return valid ? null : INVALID_SIGNATURE;
		},
		handle(storage, req, payload) {
			return handleSlackWebhook(storage, req.rawBody, payload);
		},
	},

	whatsapp: {
		// Meta verifies the webhook URL with a GET subscribe handshake; the
		// challenge must echo back as raw text. An unset verify token rejects:
		// echoing for anyone would let a third party bind our URL to their app.
		handshake(query, secrets) {
			const token = secrets.whatsappVerifyToken || "";
			if (
				token
				&& query("hub.mode") === "subscribe"
				&& constantTimeEqual(token, query("hub.verify_token"))
			) {
				return { status: 200, text: query("hub.challenge") };
			}
			return { status: 403, text: "forbidden" };
		},
		async verify(_storage, req, _payload, secret) {
			// FAIL CLOSED, unlike github/slack: an unverified WhatsApp event is
			// not a read-only notification - it injects a chat message that
			// drives an outbound reply through the operator's real number and
			// opens the conversation's 24h send window. Meta always signs, and
			// there is no legacy-unverified deployment base to stay compatible
			// with, so a missing secret rejects instead of admitting.
			if (!secret) {
				return {
					status: 401,
					body: { error: "whatsapp webhook verification not configured (set the app secret)" },
				};
			}
			// Meta signs exactly like GitHub: HMAC-SHA256 over the raw body in
			// x-hub-signature-256 - one verifier serves both.
			const valid = await verifyGitHubSignature(
				secret,
				new TextEncoder().encode(req.rawBody),
				req.header("x-hub-signature-256"),
			);
			return valid ? null : INVALID_SIGNATURE;
		},
		handle(storage, _req, payload) {
			return handleWhatsAppWebhook(storage, payload);
		},
	},

	// Generic ingress (#640): POST /webhooks/ingest/<topic> with
	// `Authorization: Bearer <ingest token>`. The verify slot is the token
	// check itself — existence, topic binding, rate limit — so the structural
	// guarantee (#639) holds: this route cannot exist unverified. The body
	// size cap is declared here but enforced by the pipeline BEFORE the body
	// is parsed.
	ingest: {
		topicRoute: true,
		maxBodyBytes: INGEST_MAX_BODY_BYTES,
		async verify(storage, req, _payload, _secret, ctx) {
			// RFC 7235: the auth scheme is case-insensitive; several webhook
			// senders emit "bearer".
			const m = req.header("authorization").match(/^Bearer\s+(\S+)\s*$/i);
			if (!m) return INGEST_FORBIDDEN;
			const record = await storage.getIngestTokenByHash(await sha256Hex(m[1]));
			// One opaque 403 for unknown/revoked AND wrong-topic: a topic-A token
			// probing topic B learns nothing beyond "not authorized here".
			if (!record || record.topic !== req.subpath) return INGEST_FORBIDDEN;
			if (!ingestRateAllow(record.id)) {
				return { status: 429, body: { error: "rate limited" } };
			}
			ctx.ingestToken = record;
			return null;
		},
		async handle(storage, _req, payload, ctx) {
			// The verified token is the ONLY routing input — topic and bubble come
			// from its binding, never from the path or body. The verify slot set
			// it; a missing record here is a pipeline invariant violation, not a
			// client error.
			const record = ctx.ingestToken;
			if (!record) return { status: 500, body: { error: "internal error" } };
			const event = createIngestEvent(record.topic, payload, record.bubble_id);
			const delivered = await storage.deliver(event);
			return { status: 200, body: { delivered_to: delivered } };
		},
	},
};

// Storage key for a conversation's message-window bookkeeping (#656). Written
// on every inbound message; read by handleChannelsSend when the channel
// declares a messageWindow. Channel-generic so future windowed channels reuse
// it.
export function channelWindowKey(source: string, scope: string, chatId: string): string {
	return `window:${source}:${scope}:${chatId}`;
}

export async function handleWhatsAppWebhook(
	storage: StorageAdapter,
	payload: Record<string, unknown>,
): Promise<HandlerResult> {
	const { events } = normalizeWhatsAppWebhook(payload);

	// Record the customer-service window: each inbound message re-opens 24h of
	// free-form replies for its conversation (checked at send time). Stamp the
	// message's OWN timestamp (newest per conversation) and never regress an
	// existing record - Meta redelivers failed webhooks for days, and a stale
	// redelivery must not falsely reopen a closed window.
	const latest = new Map<string, number>();
	for (const event of events) {
		const ref = event.conversation;
		if (!ref) continue;
		const ts = Date.parse(String(event.fields?.message_timestamp ?? ""));
		const stamp = Number.isNaN(ts) ? Date.now() : ts;
		if (stamp > (latest.get(ref) ?? 0)) latest.set(ref, stamp);
	}
	for (const [ref, stamp] of latest) {
		const conv = parseConversation(ref);
		if (!conv) continue;
		const key = channelWindowKey(conv.source, conv.scope, conv.chatId);
		const existing = await storage.getChannelState(key);
		const prev = Date.parse((existing?.last_inbound as string) || "");
		if (!Number.isNaN(prev) && prev >= stamp) continue;
		await storage.putChannelState(key, { last_inbound: new Date(stamp).toISOString() });
	}

	let delivered = 0;
	for (const event of events) {
		delivered += await storage.deliver(event);
	}
	return { status: 200, body: { ok: true, delivered_to: delivered } };
}

// An admit-without-verification, counted so /health surfaces a provider
// running unverified (the misconfiguration class this pipeline exists to
// close). Returns null — the pipeline admits the request.
function unverifiedAdmission(): null {
	_rejectionCounters.webhook_unverified++;
	return null;
}

// A matched webhook route: the registered source plus, for topic routes, the
// slash-bearing remainder of the path (`/webhooks/ingest/alert/firing` →
// subpath "alert/firing"). Exact provider routes always carry subpath "".
export interface WebhookRouteMatch {
	source: string;
	subpath: string;
}

// Match a request path against the registered webhook routes — the single
// route grammar both transports use; they gate the body read on this, so an
// unregistered path 404s without ever consuming the request body. Exact
// sources match `/webhooks/<source>` only (AT MOST one trailing slash, the
// pre-#640 grammar — `/webhooks/github//` stays a 404); topic sources REQUIRE
// a non-empty remainder, which may itself contain slashes, again with at most
// one trailing slash tolerated.
export function matchWebhookSource(path: string): WebhookRouteMatch | null {
	const m = path.match(/^\/webhooks\/([^/]+)(?:\/(.*))?$/);
	if (!m) return null;
	const source = m[1];
	const def = WEBHOOK_SOURCES[source];
	if (!def) return null;
	if (def.topicRoute) {
		// A remainder that still ends in "/" after stripping one had multiple
		// trailing slashes — malformed, and no mintable topic could match it.
		const rest = (m[2] ?? "").replace(/\/$/, "");
		return rest && !rest.endsWith("/") ? { source, subpath: rest } : null;
	}
	// Exact route: the remainder must be absent (`/webhooks/github`) or empty
	// (`/webhooks/github/`) on the wire — anything else, including `//`, 404s.
	return m[2] === undefined || m[2] === "" ? { source, subpath: "" } : null;
}

// Run an inbound webhook through the pipeline. Returns null for an
// unregistered source (the transport falls through to its native 404;
// transports that gate on matchWebhookSource never hit this).
export async function handleWebhookRequest(
	storage: StorageAdapter,
	source: string,
	req: InboundWebhookRequest,
	secrets: WebhookSecrets,
): Promise<HandlerResult | null> {
	const def = WEBHOOK_SOURCES[source];
	if (!def) return null;

	// Size gate BEFORE parse: for a capped source, an oversize body must never
	// reach JSON.parse — the cap exists to bound work done for an untrusted
	// sender, and parsing is the work.
	if (def.maxBodyBytes && req.rawBody.length > def.maxBodyBytes) {
		return { status: 413, body: { error: "payload too large" } };
	}

	let payload: unknown;
	try {
		payload = JSON.parse(req.rawBody);
	} catch {
		return INVALID_JSON;
	}
	if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
		return INVALID_JSON;
	}
	const body = payload as Record<string, unknown>;

	const early = def.preVerify?.(req, body);
	if (early) return early;

	const ctx: WebhookRequestContext = {};
	const secret = secrets[source as keyof WebhookSecrets] || "";
	const rejected = await def.verify(storage, req, body, secret, ctx);
	if (rejected) {
		// Only auth failures feed the /health signature counter — policy
		// rejections from a VALID credential (429 rate limit) must not read as
		// a provider secret misconfiguration.
		if (rejected.status === 401 || rejected.status === 403) {
			_rejectionCounters.webhook_bad_signature++;
		}
		return rejected;
	}

	return def.handle(storage, req, body, ctx);
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
// Scoped ingest tokens (#640) — mint / list / revoke. Every handler takes the
// AUTHENTICATED bubble id (entry files reject unsigned or bad-signature
// requests with an opaque 403 before calling these, mirroring
// /resources/authorize), so a token is only ever visible to — and only ever
// binds — the bubble that minted it.
// ---------------------------------------------------------------------------

// An ingest topic in the token store: `source/type` form like a CLI publish
// topic (e.g. "alert/firing"), from a charset that never URL-encodes so the
// wire path and the stored binding compare byte-for-byte. The first segment
// must not impersonate a webhook provider, and the `:` exclusion structurally
// rules out every global (github:/linear:/slack:) routing key.
const INGEST_TOPIC_SEGMENT_RE = /^[A-Za-z0-9_.-]+$/;
const INGEST_TOPIC_MAX_LENGTH = 200;
const INGEST_RESERVED_SOURCES = new Set(["github", "linear", "slack"]);

export function validateIngestTopic(topic: string): string | null {
	if (!topic || topic.length > INGEST_TOPIC_MAX_LENGTH) {
		return "topic must be 1-200 characters";
	}
	const segments = topic.split("/");
	if (segments.length < 2) {
		return "topic must use source/type form, e.g. alert/firing";
	}
	if (!segments.every((s) => INGEST_TOPIC_SEGMENT_RE.test(s))) {
		return "topic segments must be non-empty [A-Za-z0-9_.-]";
	}
	if (INGEST_RESERVED_SOURCES.has(segments[0])) {
		return "github, linear, and slack sources are reserved for webhooks";
	}
	return null;
}

export async function handleIngestTokenCreate(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId: string,
): Promise<HandlerResult> {
	const topic = typeof body.topic === "string" ? body.topic.trim() : "";
	const name = typeof body.name === "string" && body.name ? body.name : undefined;
	const invalid = validateIngestTopic(topic);
	if (invalid) return { status: 400, body: { error: invalid } };

	const token = randomToken("ingt");
	const record: IngestTokenRecord = {
		id: crypto.randomUUID(),
		bubble_id: bubbleId,
		topic,
		token_hash: await sha256Hex(token),
		...(name ? { name } : {}),
		created_at: new Date().toISOString(),
	};
	await storage.putIngestToken(record);

	// The plaintext token appears here and nowhere else — it is never stored
	// and never recoverable from list.
	return {
		status: 201,
		body: {
			id: record.id,
			topic: record.topic,
			...(name ? { name } : {}),
			token,
			created_at: record.created_at,
		},
	};
}

export async function handleIngestTokenList(
	storage: StorageAdapter,
	bubbleId: string,
): Promise<HandlerResult> {
	const records = await storage.listIngestTokens(bubbleId);
	return {
		status: 200,
		body: {
			tokens: records.map((r) => ({
				id: r.id,
				topic: r.topic,
				...(r.name ? { name: r.name } : {}),
				created_at: r.created_at,
			})),
		},
	};
}

export async function handleIngestTokenRevoke(
	storage: StorageAdapter,
	tokenId: string,
	bubbleId: string,
): Promise<HandlerResult> {
	// Resolve the id inside the caller's OWN token list — another bubble's
	// token id yields the same 404 as a nonexistent one.
	const records = await storage.listIngestTokens(bubbleId);
	const record = records.find((r) => r.id === tokenId);
	if (!record) return { status: 404, body: { error: "not found" } };
	await storage.deleteIngestToken(record);
	return { status: 200, body: { ok: true } };
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
		if ((service !== "github" && service !== "linear" && service !== "slack"
			&& service !== "whatsapp") || !rawResource) {
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

// Resolve the sending credential for a conversation's channel, scoped to the
// AUTHENTICATED bubble. The switch is the single place Phase 3 extends when a
// new channel brings its own credential store.
async function resolveChannelSendToken(
	storage: StorageAdapter,
	bubbleId: string,
	conv: Conversation,
	body: Record<string, unknown>,
): Promise<string> {
	if (conv.source === "slack") {
		return resolveSlackSendToken(
			storage,
			bubbleId,
			conv.scope,
			(body.app_id as string) || "",
			(body.bot_id as string) || "",
		);
	}
	if (conv.source === "whatsapp") {
		// Bubble-scoped lookup only, same tenancy boundary as Slack: a number
		// registered by another bubble is indistinguishable from an unknown one.
		const rec = await storage.getChannelState(whatsappNumberKey(bubbleId, conv.scope));
		return (rec?.access_token as string) || "";
	}
	return "";
}

// Storage key for a bubble-scoped WhatsApp number registration (#656). There
// is no global record: unlike Slack, our own sends never come back as inbound
// message webhooks, so no self-reply loop prevention is needed.
export function whatsappNumberKey(bubbleId: string, phoneNumberId: string): string {
	return `whatsapp_number:${bubbleId}:${phoneNumberId}`;
}

// POST /whatsapp/numbers - registration mirror of handleSlackWorkspaceRegister,
// but signed-only: there is no unsigned (global) use case, so an
// unauthenticated registration is rejected outright. Verifies the access token
// against the Graph API (the phone-number node must be readable with it), then
// stores the bubble-scoped send credential and writes the whatsapp resource
// grant that lets this bubble subscribe to `whatsapp:<pnid>` (#488).
export async function handleWhatsAppNumberRegister(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId?: string,
): Promise<HandlerResult> {
	const phoneNumberId = body.phone_number_id as string;
	const accessToken = body.access_token as string;
	if (!phoneNumberId || !accessToken) {
		return { status: 400, body: { error: "phone_number_id and access_token required" } };
	}
	if (!bubbleId) {
		return { status: 403, body: { error: "forbidden" } };
	}

	try {
		const data = await whatsappApi(
			accessToken,
			`${encodeURIComponent(phoneNumberId)}?fields=id`,
			{ method: "GET" },
		);
		if (String(data.id ?? "") !== phoneNumberId) {
			return { status: 403, body: { error: "forbidden" } };
		}
	} catch {
		return { status: 403, body: { error: "forbidden" } };
	}

	await storage.putChannelState(whatsappNumberKey(bubbleId, phoneNumberId), {
		access_token: accessToken,
	});
	// Upstream token verification IS the proof of access, same convergence as
	// Slack (#488 §6): the grant gates inbound `whatsapp:<pnid>` delivery.
	await storage.putResourceGrant({
		id: `whatsapp:${phoneNumberId}:${bubbleId}`,
		account_id: null,
		bubble_id: bubbleId,
		service: "whatsapp",
		resource: phoneNumberId,
		granted_by: "upstream_token_verification",
		organization_id: null,
		created_at: new Date().toISOString(),
		expires_at: null,
	});

	return { status: 200, body: { ok: true, phone_number_id: phoneNumberId } };
}

// `bubbleId` is the AUTHENTICATED bubble (the entry files reject an unsigned or
// bad-signature request with 403 before calling this). The Slack credentials
// are looked up under that bubble's scope only — the outbound tenancy boundary.
//
// Legacy Slack-shaped send, kept as a shim over the channel adapter for
// pre-#618 clients. Text arrives pre-converted (mrkdwn) and is sent verbatim;
// the generic /channels/send path takes raw markdown instead.
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

	const adapter = getChannelAdapter("slack")!;
	const conv: Conversation = {
		source: "slack",
		scope: (body.workspace as string) || "",
		chatType: "channel",
		chatId: channel,
		...(body.thread_ts ? { threadId: body.thread_ts as string } : {}),
	};
	let result;
	try {
		result = await adapter.send(botToken, conv, text);
	} catch (err) {
		return { status: 502, body: { ok: false, error: String(err) } };
	}
	if (!result.ok) {
		return { status: 502, body: { ok: false, error: result.error } };
	}
	return { status: 200, body: { ok: true, ts: result.ts } };
}

// One outbound file on /channels/send: { name, content_b64, title? }.
function decodeOutboundFiles(raw: unknown): OutboundFile[] | null {
	if (raw === undefined) return [];
	if (!Array.isArray(raw)) return null;
	const files: OutboundFile[] = [];
	for (const item of raw) {
		if (!item || typeof item !== "object") return null;
		const f = item as Record<string, unknown>;
		if (typeof f.name !== "string" || !f.name || typeof f.content_b64 !== "string" || !f.content_b64) {
			return null;
		}
		let data: Uint8Array;
		try {
			data = Uint8Array.from(atob(f.content_b64), (c) => c.charCodeAt(0));
		} catch {
			return null;
		}
		files.push({
			name: f.name,
			data,
			...(typeof f.title === "string" && f.title ? { title: f.title } : {}),
		});
	}
	return files;
}

// Channel-agnostic outbound send (#618, #629):
//   { conversation, text?, mode?, edit_ref?, files? }
// Parses the conversation reference and delegates to the channel's adapter.
// `bubbleId` is the authenticated bubble; credentials resolve only under its
// scope (same contract as handleSlackSend).
//
// Text arrives as raw markdown — formatting is the gateway's job (the Slack
// adapter delivers it natively via markdown_text). Modes:
//   post    post a new message
//   update  edit the message named by edit_ref (e.g. a placeholder)
//   final   resolve the response context: edit edit_ref when given, else
//           post; then clear the typing indicator either way
// Capability degradation happens here: update on a channel without edit
// support becomes a follow-up post; typing on a channel without indicators
// is a silent no-op.
// Spacing between follow-up chunk posts (#651): chunked sends fire several
// posts into one channel back-to-back, and chat platforms rate-limit sustained
// per-channel posting (Slack: ~1/sec with burst allowance).
const CHUNK_SPACING_MS = 300;

export async function handleChannelsSend(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId: string,
): Promise<HandlerResult> {
	const ref = body.conversation;
	const text = body.text;
	// Explicit string checks: a non-string conversation (array, number) must
	// be a 400, not a TypeError escaping the handler as a 500.
	if (typeof ref !== "string" || !ref) {
		return { status: 400, body: { error: "conversation required" } };
	}
	const files = decodeOutboundFiles(body.files);
	if (files === null) {
		return { status: 400, body: { error: "invalid files: expected [{name, content_b64, title?}]" } };
	}
	// Text is required unless files carry the payload (it then becomes the
	// upload comment). A present-but-non-string text is always a 400.
	if (text !== undefined && typeof text !== "string") {
		return { status: 400, body: { error: "text must be a string" } };
	}
	if (!text && files.length === 0) {
		return { status: 400, body: { error: "text or files required" } };
	}
	const conv = parseConversation(ref);
	if (!conv) {
		return { status: 400, body: { error: "invalid conversation reference" } };
	}
	const adapter = getChannelAdapter(conv.source);
	if (!adapter) {
		return { status: 400, body: { error: `unsupported channel: ${conv.source}` } };
	}

	const mode = (body.mode as string) || "post";
	if (mode !== "post" && mode !== "update" && mode !== "final") {
		return { status: 400, body: { error: `invalid mode: ${mode}` } };
	}
	const editRef = (body.edit_ref as string) || "";
	if (mode === "update" && !editRef) {
		return { status: 400, body: { error: "edit_ref required for mode: update" } };
	}

	const caps = adapter.descriptor.capabilities;

	// The credential and window-state reads are independent; start both
	// together and keep the credential error's precedence.
	const [botToken, windowState] = await Promise.all([
		resolveChannelSendToken(storage, bubbleId, conv, body),
		caps.messageWindow
			? storage.getChannelState(channelWindowKey(conv.source, conv.scope, conv.chatId))
			: Promise.resolve(null),
	]);
	if (!botToken) {
		return { status: 400, body: { error: `no send credential registered for ${conv.source}:${conv.scope}` } };
	}

	// Message-window enforcement (#656): a windowed channel (WhatsApp's 24h
	// customer-service window) only accepts free-form replies for N hours
	// after the user's last inbound message. A KNOWN-stale window fails with
	// a TYPED error so the agent can report the situation instead of a
	// mystery platform rejection. A MISSING record passes through: it can
	// mean KV replication lag right after a fresh inbound (Workers), so
	// rejecting would break the most common flow - the platform itself is
	// the authoritative enforcer and its rejection surfaces as a send error.
	// Template messaging (the outside-window escape hatch) is a follow-up.
	if (caps.messageWindow) {
		const state = windowState;
		const last = Date.parse((state?.last_inbound as string) || "");
		if (!Number.isNaN(last)
			&& Date.now() - last > caps.messageWindow.hours * 3600_000) {
			return {
				status: 400,
				body: {
					error: "outside_message_window",
					detail: `no inbound message from this user in the last `
						+ `${caps.messageWindow.hours}h; free-form replies are closed`
						+ (caps.messageWindow.outsideWindow === "template"
							? " (template messages are not supported yet)"
							: ""),
				},
			};
		}
	}

	const rawText = (text as string) || "";

	let result;
	try {
		if (files.length > 0) {
			// File sends stay single-message: the text is a comment or a
			// placeholder replacement, so over-budget text truncates.
			const outText = truncateForChannel(rawText, caps);
			if (!adapter.uploadFiles || !caps.files) {
				return { status: 400, body: { error: `channel ${conv.source} does not support files` } };
			}
			// A file reply that resolves a placeholder: a message cannot be
			// edited INTO a file share, so replace the placeholder text first,
			// then attach the file without a duplicate comment.
			if (editRef && (mode === "update" || mode === "final")) {
				if (!outText) {
					return { status: 400, body: { error: "text required when edit_ref is combined with files" } };
				}
				if (adapter.update && caps.edit) {
					const edited = await adapter.update(botToken, conv, editRef, outText, { markdown: true });
					if (!edited.ok) {
						return { status: 502, body: { ok: false, error: edited.error } };
					}
				}
				result = await adapter.uploadFiles(botToken, conv, files);
			} else {
				result = await adapter.uploadFiles(botToken, conv, files, outText || undefined);
			}
		} else {
			// Editing a placeholder degrades to a follow-up post on a channel
			// without edit support - one degradation rule for update and final.
			// mode "post" always posts, even if a stray edit_ref is present.
			const wantsEdit = Boolean(editRef) && (mode === "update" || mode === "final");
			const editOrSend = (txt: string) =>
				wantsEdit && adapter.update && caps.edit
					? adapter.update(botToken, conv, editRef, txt, { markdown: true })
					: adapter.send(botToken, conv, txt, { markdown: true });

			if (editRef && mode === "update") {
				// Streaming rewrite of one message: chunking would post a new
				// message on every tick, so the budget stays truncation-enforced
				// until the final send.
				result = await editOrSend(truncateForChannel(rawText, caps));
			} else {
				// Terminal send (post, or final resolving a placeholder):
				// over-budget text goes out whole as natural-boundary chunks.
				// The first chunk carries the message identity (placeholder
				// edit, returned ts); later chunks are follow-up posts in the
				// same conversation, lightly paced for channel rate limits.
				const chunks = chunkForChannel(rawText, caps);
				result = await editOrSend(chunks[0]);
				for (let i = 1; result.ok && i < chunks.length; i++) {
					await new Promise((r) => setTimeout(r, CHUNK_SPACING_MS));
					const follow = await adapter.send(botToken, conv, chunks[i], { markdown: true });
					if (!follow.ok) {
						// Chunks 1..i are already visible: clear the typing
						// indicator (the reply IS partially delivered) and
						// surface an error a caller will not blindly retry.
						if ((mode === "update" || mode === "final") && adapter.typing && caps.typing) {
							await adapter.typing(botToken, conv, false);
						}
						return {
							status: 502,
							body: {
								ok: false,
								ts: result.ts,
								error: `chunk ${i + 1}/${chunks.length} failed after partial delivery `
									+ `(do not resend; the reply is partially visible): ${follow.error}`,
							},
						};
					}
				}
			}
		}
	} catch (err) {
		return { status: 502, body: { ok: false, error: String(err) } };
	}
	if (!result.ok) {
		return { status: 502, body: { ok: false, error: result.error } };
	}
	// Resolving a response context (placeholder edit or final reply) also
	// clears the "is thinking..." indicator the placeholder flow set.
	if ((mode === "update" || mode === "final") && adapter.typing && caps.typing) {
		await adapter.typing(botToken, conv, false);
	}
	return { status: 200, body: { ok: true, ts: result.ts } };
}

// POST /channels/typing (#629): { conversation, on }. Sets or clears the
// channel's thinking/typing indicator. A channel without typing support is a
// silent no-op (200, supported: false) — capability degradation is the
// gateway's job, not the caller's.
export async function handleChannelsTyping(
	storage: StorageAdapter,
	body: Record<string, unknown>,
	bubbleId: string,
): Promise<HandlerResult> {
	const ref = body.conversation;
	if (typeof ref !== "string" || !ref || typeof body.on !== "boolean") {
		return { status: 400, body: { error: "conversation and on required" } };
	}
	const conv = parseConversation(ref);
	if (!conv) {
		return { status: 400, body: { error: "invalid conversation reference" } };
	}
	const adapter = getChannelAdapter(conv.source);
	if (!adapter) {
		return { status: 400, body: { error: `unsupported channel: ${conv.source}` } };
	}
	if (!adapter.typing || !adapter.descriptor.capabilities.typing) {
		return { status: 200, body: { ok: true, supported: false } };
	}
	const botToken = await resolveChannelSendToken(storage, bubbleId, conv, body);
	if (!botToken) {
		return { status: 400, body: { error: `no send credential registered for ${conv.source}:${conv.scope}` } };
	}
	await adapter.typing(botToken, conv, body.on);
	return { status: 200, body: { ok: true, supported: true } };
}

// GET /channels/history (#629): ?conversation=...&limit=... — read the
// messages of a conversation (Slack: the thread the ref anchors). Returns
// { ok, messages: [{user, text, ts, files?}] } oldest-first.
export async function handleChannelsHistory(
	storage: StorageAdapter,
	conversationRef: string,
	limit: number,
	bubbleId: string,
): Promise<HandlerResult> {
	if (!conversationRef) {
		return { status: 400, body: { error: "conversation required" } };
	}
	const conv = parseConversation(conversationRef);
	if (!conv) {
		return { status: 400, body: { error: "invalid conversation reference" } };
	}
	const adapter = getChannelAdapter(conv.source);
	if (!adapter) {
		return { status: 400, body: { error: `unsupported channel: ${conv.source}` } };
	}
	if (!adapter.fetchConversation) {
		return { status: 400, body: { error: `channel ${conv.source} does not support history` } };
	}
	const botToken = await resolveChannelSendToken(storage, bubbleId, conv, {});
	if (!botToken) {
		return { status: 400, body: { error: `no send credential registered for ${conv.source}:${conv.scope}` } };
	}
	const capped = Number.isFinite(limit) && limit > 0 ? Math.min(limit, 1000) : 100;
	let messages;
	try {
		messages = await adapter.fetchConversation(botToken, conv, capped);
	} catch (err) {
		return { status: 502, body: { ok: false, error: String(err) } };
	}
	return { status: 200, body: { ok: true, messages } };
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
			const resp = await fetch(`${slackApiUrl()}auth.test`, {
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
			const resp = await fetch(`${slackApiUrl()}auth.test`, {
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
				`${slackApiUrl()}bots.info?bot=${encodeURIComponent(botId)}`,
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
