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
	subscriptions: string[];
	created_at?: string;
}

export interface SlackWorkspaceRecord {
	bot_token: string;
	bot_id?: string;
}

export interface StorageAdapter {
	getDeploymentByApiKey(apiKey: string): Promise<DeploymentRecord | null>;
	putDeployment(deployment: DeploymentRecord): Promise<void>;
	addSubscription(key: string, deploymentId: string): Promise<void>;
	deliver(event: NormalizedEvent): Promise<number>;
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
): NormalizedEvent {
	const payload = (body.payload as Record<string, unknown>) || body;

	// Build topics from any routing fields in the body
	const topics: string[] = [];
	if (body.repo) topics.push(`github:${body.repo as string}`);
	if (body.team_key) topics.push(`linear:${body.team_key as string}`);
	if (body.workspace) topics.push(`slack:${body.workspace as string}`);
	// Fallback: the topic path itself acts as the subscription key
	if (topics.length === 0) topics.push(topic);

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

export function subscriptionKeysForEvent(event: NormalizedEvent): string[] {
	return event.topics?.length ? event.topics : [event.type];
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
	return hexSig === signature;
}

export async function verifyGitHubSignature(
	secret: string,
	body: Uint8Array,
	signatureHeader: string,
): Promise<boolean> {
	if (!signatureHeader) return false;

	const expected = "sha256=" + (await hmacSha256Hex(secret, body));
	return expected === signatureHeader;
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
	let selfBotId: string | undefined;
	if (teamId) {
		const ws = await storage.getSlackWorkspace(teamId);
		if (ws) selfBotId = ws.bot_id;
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

export async function handleRegisterDeployment(
	storage: StorageAdapter,
	body: Record<string, unknown>,
): Promise<HandlerResult> {
	const name = body.name as string;
	const subscriptions = body.subscriptions as string[];

	if (!name || !subscriptions?.length) {
		return { status: 400, body: { error: "name and subscriptions[] required" } };
	}

	const deploymentId = crypto.randomUUID();
	const apiKey = `moda_${crypto.randomUUID().replace(/-/g, "")}`;

	const deployment: DeploymentRecord = {
		id: deploymentId,
		name,
		api_key: apiKey,
		subscriptions,
		created_at: new Date().toISOString(),
	};

	await storage.putDeployment(deployment);

	for (const sub of subscriptions) {
		await storage.addSubscription(sub, deploymentId);
	}

	await storage.initDeploymentSession(deploymentId, subscriptions);

	return { status: 201, body: { deployment_id: deploymentId, api_key: apiKey } };
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

	let added = 0;
	for (const sub of newSubs) {
		if (!deployment.subscriptions.includes(sub)) {
			deployment.subscriptions.push(sub);
			added++;
		}
		await storage.addSubscription(sub, deploymentId);
	}

	await storage.putDeployment(deployment);

	return { status: 200, body: { subscriptions: deployment.subscriptions, added } };
}

export async function handleTopicEvent(
	storage: StorageAdapter,
	topic: string,
	body: Record<string, unknown>,
): Promise<HandlerResult> {
	const event = createTopicEvent(topic, body);
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

	let result;
	try {
		result = await sendSlackMessage(ws.bot_token, channel, text, body.thread_ts as string | undefined);
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

	let botId: string | undefined;
	try {
		const resp = await fetch("https://slack.com/api/auth.test", {
			headers: { Authorization: `Bearer ${botToken}` },
		});
		const data = (await resp.json()) as Record<string, unknown>;
		if (data.ok) {
			botId = data.bot_id as string;
		}
	} catch {
		// best-effort — self-loop filtering degrades gracefully without bot_id
	}

	await storage.putSlackWorkspace(workspaceId, { bot_token: botToken, bot_id: botId });
	return { status: 200, body: { ok: true, workspace_id: workspaceId, bot_id: botId } };
}
