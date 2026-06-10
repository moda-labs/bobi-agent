export interface NormalizedEvent {
	id: string;
	source: string;
	type: string;
	timestamp: string;
	payload: Record<string, unknown>;
	repo?: string;
	installation_id?: number;
	team_key?: string;
	workspace?: string;
	channel?: string;
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
	return {
		id: (body.id as string) || crypto.randomUUID(),
		source: (body.source as string) || "custom",
		type: topic,
		timestamp: new Date().toISOString(),
		payload: (body.payload as Record<string, unknown>) || body,
		repo: body.repo as string | undefined,
		team_key: body.team_key as string | undefined,
		workspace: body.workspace as string | undefined,
		channel: body.channel as string | undefined,
	};
}

export function normalizeGitHubPayload(
	eventHeader: string,
	deliveryId: string,
	payload: Record<string, unknown>,
): NormalizedEvent | null {
	const repoFullName =
		(payload.repository as Record<string, unknown> | undefined)?.full_name as string | undefined;
	const installationId =
		(payload.installation as Record<string, unknown> | undefined)?.id as number | undefined;

	if (!repoFullName) return null;

	return {
		id: deliveryId || crypto.randomUUID(),
		source: "github",
		type: `github.${eventHeader}`,
		repo: repoFullName,
		installation_id: installationId,
		timestamp: new Date().toISOString(),
		payload,
	};
}

export function normalizeLinearPayload(
	payload: Record<string, unknown>,
): NormalizedEvent {
	const action = (payload.action as string) || "unknown";
	const dataType = (payload.type as string) || "unknown";
	const teamKey =
		((payload.data as Record<string, unknown> | undefined)?.team as Record<string, unknown> | undefined)
			?.key as string | undefined;

	return {
		id: crypto.randomUUID(),
		source: "linear",
		type: `linear.${dataType}.${action}`,
		team_key: teamKey,
		timestamp: new Date().toISOString(),
		payload,
	};
}

export function normalizeSlackPayload(
	payload: Record<string, unknown>,
	selfBotId?: string,
): SlackNormalizationResult {
	if (payload.type === "url_verification") {
		return { event: null, challenge: payload.challenge as string, skip: true };
	}

	if (payload.type !== "event_callback") {
		return { event: null, skip: true };
	}

	const event = payload.event as Record<string, unknown> | undefined;
	if (!event) return { event: null, skip: true };

	if (event.subtype) {
		return { event: null, skip: true };
	}

	// Only filter our own bot's messages to prevent loops.
	// Messages from other bots (e.g. user-level Slack apps) pass through.
	if (event.bot_id && selfBotId && event.bot_id === selfBotId) {
		return { event: null, skip: true };
	}

	const eventType = event.type as string;
	const channelType = (event.channel_type as string) || "";
	const threadTs = (event.thread_ts as string) || "";

	let slackEventType: string;
	if (eventType === "app_mention") {
		slackEventType = "slack.mention";
	} else if (channelType === "im" || channelType === "mpim") {
		slackEventType = "slack.dm";
	} else if (threadTs) {
		slackEventType = "slack.thread_reply";
	} else {
		return { event: null, skip: true };
	}

	const teamId = (payload.team_id as string) || "";
	const channel = (event.channel as string) || "";

	return {
		event: {
			id: (payload.event_id as string) || crypto.randomUUID(),
			source: "slack",
			type: slackEventType,
			workspace: teamId,
			channel,
			timestamp: new Date().toISOString(),
			payload: {
				user_id: (event.user as string) || "",
				channel,
				channel_type: channelType,
				text: ((event.text as string) || "").slice(0, 4000),
				ts: (event.ts as string) || "",
				thread_ts: threadTs,
			},
		},
		skip: false,
	};
}

export function subscriptionKeysForEvent(event: NormalizedEvent): string[] {
	const keys: string[] = [];
	if (event.repo) keys.push(`github:${event.repo}`);
	if (event.team_key) keys.push(`linear:${event.team_key}`);
	if (event.workspace) {
		keys.push(`slack:${event.workspace}`);
	}
	// Generic topic routing — fallback for events without source-specific keys
	// (e.g. monitor-generated events posted to /events/{topic}).
	if (keys.length === 0 && event.type) keys.push(event.type);
	return keys;
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
	const event = normalizeGitHubPayload(eventHeader, deliveryId, payload);
	if (!event) return { status: 400, body: { error: "no repository in payload" } };
	const delivered = await storage.deliver(event);
	return { status: 200, body: { delivered_to: delivered } };
}

export async function handleLinearWebhook(
	storage: StorageAdapter,
	payload: Record<string, unknown>,
): Promise<HandlerResult> {
	const event = normalizeLinearPayload(payload);
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

	const result = normalizeSlackPayload(payload, selfBotId);

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
