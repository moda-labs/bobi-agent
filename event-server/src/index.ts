export { DeploymentSession } from "./deployment-session";
import {
	type NormalizedEvent,
	createTopicEvent,
	normalizeGitHubPayload,
	normalizeLinearPayload,
	normalizeSlackPayload,
	subscriptionKeysForEvent,
	verifySlackSignature,
} from "./core";

interface Env {
	EVENTS: KVNamespace;
	DEPLOYMENT_SESSION: DurableObjectNamespace;
	WEBHOOK_SECRET?: string;
	SLACK_SIGNING_SECRET?: string;
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const path = url.pathname;

		if (request.method === "POST" && path === "/webhooks/github") {
			return handleGitHubWebhook(request, env);
		}
		if (request.method === "POST" && path === "/webhooks/linear") {
			return handleLinearWebhook(request, env);
		}
		if (request.method === "POST" && path === "/webhooks/slack") {
			return handleSlackWebhook(request, env);
		}
		if (request.method === "POST" && path === "/deployments") {
			return handleRegisterDeployment(request, env);
		}
		if (request.method === "GET" && path.startsWith("/deployments/") && path.endsWith("/subscribe")) {
			return handleSubscribe(request, env, path);
		}
		if (request.method === "PUT" && path.startsWith("/deployments/") && path.endsWith("/subscriptions")) {
			return handleUpdateSubscriptions(request, env, path);
		}
		// Generic topic: POST /events/{topic}
		const topicMatch = request.method === "POST" && path.match(/^\/events\/(.+)$/);
		if (topicMatch) {
			return handleTopicEvent(request, env, topicMatch[1]);
		}
		// Slack send-through: POST /slack/send
		if (request.method === "POST" && path === "/slack/send") {
			return handleSlackSend(request, env);
		}
		// Slack workspace registry: POST /slack/workspaces
		if (request.method === "POST" && path === "/slack/workspaces") {
			return handleSlackWorkspaceRegister(request, env);
		}
		if (request.method === "GET" && path === "/health") {
			return Response.json({ status: "ok" });
		}

		return new Response("Not Found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;

async function routeToDeployments(env: Env, event: NormalizedEvent): Promise<number> {
	const keys = subscriptionKeysForEvent(event);
	const ids = new Set<string>();
	for (const key of keys) {
		const data = await env.EVENTS.get(`subscriptions:${key}`);
		if (data) {
			for (const id of JSON.parse(data)) {
				ids.add(id);
			}
		}
	}

	for (const deploymentId of ids) {
		const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
		const stub = env.DEPLOYMENT_SESSION.get(doId);
		await stub.fetch(
			new Request("https://internal/event", {
				method: "POST",
				body: JSON.stringify(event),
			}),
		);
	}

	return ids.size;
}

async function handleGitHubWebhook(request: Request, env: Env): Promise<Response> {
	const body = await request.text();
	let payload: Record<string, unknown>;
	try {
		payload = JSON.parse(body);
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const eventHeader = request.headers.get("x-github-event") || "unknown";
	const deliveryId = request.headers.get("x-github-delivery") || crypto.randomUUID();

	const event = normalizeGitHubPayload(eventHeader, deliveryId, payload);
	if (!event) {
		return new Response("No repository in payload", { status: 400 });
	}

	const delivered = await routeToDeployments(env, event);
	return Response.json({ delivered_to: delivered });
}

async function handleLinearWebhook(request: Request, env: Env): Promise<Response> {
	const body = await request.text();
	let payload: Record<string, unknown>;
	try {
		payload = JSON.parse(body);
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const event = normalizeLinearPayload(payload);
	const delivered = await routeToDeployments(env, event);
	return Response.json({ delivered_to: delivered });
}

async function handleSlackWebhook(request: Request, env: Env): Promise<Response> {
	const body = await request.text();

	if (request.headers.get("x-slack-retry-num")) {
		return new Response("OK");
	}

	let payload: Record<string, unknown>;
	try {
		payload = JSON.parse(body);
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	if (env.SLACK_SIGNING_SECRET) {
		const timestamp = request.headers.get("x-slack-request-timestamp") || "";
		const signature = request.headers.get("x-slack-signature") || "";
		const valid = await verifySlackSignature(env.SLACK_SIGNING_SECRET, timestamp, body, signature);
		if (!valid) {
			return new Response("Invalid signature", { status: 401 });
		}
	}

	const result = normalizeSlackPayload(payload);

	if (result.challenge !== undefined) {
		return Response.json({ challenge: result.challenge });
	}

	if (result.skip || !result.event) {
		return new Response("OK");
	}

	const delivered = await routeToDeployments(env, result.event);
	return Response.json({ delivered_to: delivered });
}

async function handleRegisterDeployment(request: Request, env: Env): Promise<Response> {
	let body: Record<string, unknown>;
	try {
		body = (await request.json()) as Record<string, unknown>;
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const name = body.name as string;
	const subscriptions = body.subscriptions as string[];

	if (!name || !subscriptions?.length) {
		return Response.json({ error: "name and subscriptions[] required" }, { status: 400 });
	}

	const deploymentId = crypto.randomUUID();
	const apiKey = `moda_${crypto.randomUUID().replace(/-/g, "")}`;

	const deployment = {
		id: deploymentId,
		name,
		api_key: apiKey,
		subscriptions,
		created_at: new Date().toISOString(),
	};

	await env.EVENTS.put(`deployments:${apiKey}`, JSON.stringify(deployment));
	await env.EVENTS.put(`deployment_id:${deploymentId}`, JSON.stringify(deployment));

	for (const sub of subscriptions) {
		const key = `subscriptions:${sub}`;
		const existing = await env.EVENTS.get(key);
		const ids: string[] = existing ? JSON.parse(existing) : [];
		if (!ids.includes(deploymentId)) {
			ids.push(deploymentId);
			await env.EVENTS.put(key, JSON.stringify(ids));
		}
	}

	const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
	const stub = env.DEPLOYMENT_SESSION.get(doId);
	await stub.fetch(
		new Request("https://internal/init", {
			method: "POST",
			body: JSON.stringify({ deployment_id: deploymentId, subscriptions }),
		}),
	);

	return Response.json({ deployment_id: deploymentId, api_key: apiKey }, { status: 201 });
}

async function handleSubscribe(request: Request, env: Env, path: string): Promise<Response> {
	const match = path.match(/^\/deployments\/([^/]+)\/subscribe$/);
	if (!match) {
		return new Response("Invalid path", { status: 400 });
	}
	const deploymentId = match[1];

	const authHeader = request.headers.get("authorization");
	if (!authHeader?.startsWith("Bearer ")) {
		return new Response("Unauthorized", { status: 401 });
	}
	const apiKey = authHeader.slice(7);

	const deploymentData = await env.EVENTS.get(`deployments:${apiKey}`);
	if (!deploymentData) {
		return new Response("Invalid API key", { status: 403 });
	}
	const deployment = JSON.parse(deploymentData);
	if (deployment.id !== deploymentId) {
		return new Response("API key does not match deployment", { status: 403 });
	}

	const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
	const stub = env.DEPLOYMENT_SESSION.get(doId);
	return stub.fetch(request);
}

async function handleUpdateSubscriptions(request: Request, env: Env, path: string): Promise<Response> {
	const match = path.match(/^\/deployments\/([^/]+)\/subscriptions$/);
	if (!match) {
		return new Response("Invalid path", { status: 400 });
	}
	const deploymentId = match[1];

	const authHeader = request.headers.get("authorization");
	if (!authHeader?.startsWith("Bearer ")) {
		return new Response("Unauthorized", { status: 401 });
	}
	const apiKey = authHeader.slice(7);

	const deploymentData = await env.EVENTS.get(`deployments:${apiKey}`);
	if (!deploymentData) {
		return new Response("Invalid API key", { status: 403 });
	}
	const deployment = JSON.parse(deploymentData);
	if (deployment.id !== deploymentId) {
		return new Response("API key does not match deployment", { status: 403 });
	}

	let body: Record<string, unknown>;
	try {
		body = (await request.json()) as Record<string, unknown>;
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const newSubs = body.add as string[] | undefined;
	if (!newSubs?.length) {
		return Response.json({ error: "add[] required" }, { status: 400 });
	}

	const existingSubs: string[] = deployment.subscriptions || [];
	let added = 0;

	for (const sub of newSubs) {
		if (!existingSubs.includes(sub)) {
			existingSubs.push(sub);
			added++;
		}
		const key = `subscriptions:${sub}`;
		const existing = await env.EVENTS.get(key);
		const ids: string[] = existing ? JSON.parse(existing) : [];
		if (!ids.includes(deploymentId)) {
			ids.push(deploymentId);
			await env.EVENTS.put(key, JSON.stringify(ids));
		}
	}

	deployment.subscriptions = existingSubs;
	await env.EVENTS.put(`deployments:${apiKey}`, JSON.stringify(deployment));
	await env.EVENTS.put(`deployment_id:${deploymentId}`, JSON.stringify(deployment));

	return Response.json({ subscriptions: existingSubs, added });
}

async function handleTopicEvent(request: Request, env: Env, topic: string): Promise<Response> {
	let body: Record<string, unknown>;
	try {
		body = (await request.json()) as Record<string, unknown>;
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const event = createTopicEvent(topic, body);
	const delivered = await routeToDeployments(env, event);
	return Response.json({ delivered_to: delivered });
}

async function handleSlackWorkspaceRegister(request: Request, env: Env): Promise<Response> {
	let body: Record<string, unknown>;
	try {
		body = (await request.json()) as Record<string, unknown>;
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const workspaceId = body.workspace_id as string;
	const botToken = body.bot_token as string;
	if (!workspaceId || !botToken) {
		return Response.json({ error: "workspace_id and bot_token required" }, { status: 400 });
	}

	await env.EVENTS.put(`slack_workspace:${workspaceId}`, JSON.stringify({ bot_token: botToken }));
	return Response.json({ ok: true, workspace_id: workspaceId });
}

async function handleSlackSend(request: Request, env: Env): Promise<Response> {
	let body: Record<string, unknown>;
	try {
		body = (await request.json()) as Record<string, unknown>;
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const workspaceId = body.workspace as string;
	const channel = body.channel as string;
	const text = body.text as string;
	if (!channel || !text) {
		return Response.json({ error: "channel and text required" }, { status: 400 });
	}

	let botToken = "";
	if (workspaceId) {
		const wsData = await env.EVENTS.get(`slack_workspace:${workspaceId}`);
		if (wsData) {
			botToken = (JSON.parse(wsData) as Record<string, string>).bot_token || "";
		}
	}
	if (!botToken) {
		return Response.json({ error: "no bot token for workspace" }, { status: 400 });
	}

	const payload: Record<string, unknown> = { channel, text };
	if (body.thread_ts) payload.thread_ts = body.thread_ts;

	const resp = await fetch("https://slack.com/api/chat.postMessage", {
		method: "POST",
		headers: {
			Authorization: `Bearer ${botToken}`,
			"Content-Type": "application/json",
		},
		body: JSON.stringify(payload),
	});
	const result = (await resp.json()) as Record<string, unknown>;

	if (!result.ok) {
		return Response.json({ ok: false, error: result.error }, { status: 502 });
	}
	return Response.json({ ok: true, ts: result.ts });
}
