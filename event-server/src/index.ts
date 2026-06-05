export { DeploymentSession } from "./deployment-session";

interface Env {
	EVENTS: KVNamespace;
	DEPLOYMENT_SESSION: DurableObjectNamespace;
	WEBHOOK_SECRET?: string;
	SLACK_SIGNING_SECRET?: string;
	GITHUB_CLIENT_ID?: string;
	GITHUB_CLIENT_SECRET?: string;
}

interface UserRecord {
	github_user_id: number;
	github_username: string;
	github_token: string;
	session_token: string;
	created_at: string;
	last_seen: string;
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
		if (request.method === "DELETE" && path.match(/^\/deployments\/[^/]+$/)) {
			return handleDeleteDeployment(request, env, path);
		}
		if (request.method === "GET" && path === "/auth/config") {
			return Response.json({ client_id: env.GITHUB_CLIENT_ID || "" });
		}
		if (request.method === "POST" && path === "/auth/github/callback") {
			return handleAuthCallback(request, env);
		}
		if (request.method === "GET" && path === "/health") {
			return Response.json({ status: "ok" });
		}

		return new Response("Not Found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;

async function handleGitHubWebhook(request: Request, env: Env): Promise<Response> {
	const body = await request.text();
	let payload: Record<string, unknown>;
	try {
		payload = JSON.parse(body);
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const event = request.headers.get("x-github-event") || "unknown";
	const deliveryId = request.headers.get("x-github-delivery") || crypto.randomUUID();

	const repoFullName =
		(payload.repository as Record<string, unknown>)?.full_name as string | undefined;
	const installationId =
		(payload.installation as Record<string, unknown>)?.id as number | undefined;

	if (!repoFullName) {
		return new Response("No repository in payload", { status: 400 });
	}

	const normalizedEvent = {
		id: deliveryId,
		source: "github",
		type: `github.${event}`,
		repo: repoFullName,
		installation_id: installationId,
		timestamp: new Date().toISOString(),
		payload,
	};

	const deploymentIds = await findSubscribedDeployments(env, repoFullName);

	for (const deploymentId of deploymentIds) {
		const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
		const stub = env.DEPLOYMENT_SESSION.get(doId);
		await stub.fetch(new Request("https://internal/event", {
			method: "POST",
			body: JSON.stringify(normalizedEvent),
		}));
	}

	return Response.json({ delivered_to: deploymentIds.length });
}

async function handleLinearWebhook(request: Request, env: Env): Promise<Response> {
	const body = await request.text();
	let payload: Record<string, unknown>;
	try {
		payload = JSON.parse(body);
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const action = (payload.action as string) || "unknown";
	const dataType = (payload.type as string) || "unknown";

	const teamKey =
		((payload.data as Record<string, unknown>)?.team as Record<string, unknown>)?.key as
			| string
			| undefined;

	const normalizedEvent = {
		id: crypto.randomUUID(),
		source: "linear",
		type: `linear.${dataType}.${action}`,
		team_key: teamKey,
		timestamp: new Date().toISOString(),
		payload,
	};

	const deploymentIds = teamKey
		? await findSubscribedDeployments(env, `linear:${teamKey}`)
		: [];

	for (const deploymentId of deploymentIds) {
		const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
		const stub = env.DEPLOYMENT_SESSION.get(doId);
		await stub.fetch(new Request("https://internal/event", {
			method: "POST",
			body: JSON.stringify(normalizedEvent),
		}));
	}

	return Response.json({ delivered_to: deploymentIds.length });
}

async function handleSlackWebhook(request: Request, env: Env): Promise<Response> {
	const body = await request.text();

	// Slack retries — ack immediately to prevent reprocessing
	if (request.headers.get("x-slack-retry-num")) {
		return new Response("OK");
	}

	let payload: Record<string, unknown>;
	try {
		payload = JSON.parse(body);
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	// URL verification challenge (sent during Slack app setup)
	if (payload.type === "url_verification") {
		return Response.json({ challenge: payload.challenge });
	}

	// Verify request signature
	if (env.SLACK_SIGNING_SECRET) {
		const timestamp = request.headers.get("x-slack-request-timestamp") || "";
		const signature = request.headers.get("x-slack-signature") || "";

		if (!timestamp || !signature) {
			return new Response("Missing signature headers", { status: 401 });
		}

		const age = Math.abs(Date.now() / 1000 - parseInt(timestamp, 10));
		if (age > 300) {
			return new Response("Request too old", { status: 401 });
		}

		const sigBase = `v0:${timestamp}:${body}`;
		const key = await crypto.subtle.importKey(
			"raw",
			new TextEncoder().encode(env.SLACK_SIGNING_SECRET),
			{ name: "HMAC", hash: "SHA-256" },
			false,
			["sign"],
		);
		const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(sigBase));
		const hexSig = "v0=" + Array.from(new Uint8Array(sig))
			.map(b => b.toString(16).padStart(2, "0")).join("");

		if (hexSig !== signature) {
			return new Response("Invalid signature", { status: 401 });
		}
	}

	if (payload.type !== "event_callback") {
		return new Response("OK");
	}

	const event = payload.event as Record<string, unknown>;
	if (!event) {
		return new Response("OK");
	}

	// Skip bot messages and subtypes (edits, deletes, etc.)
	if (event.bot_id || event.subtype) {
		return new Response("OK");
	}

	const eventType = event.type as string;
	const channelType = event.channel_type as string || "";
	const threadTs = event.thread_ts as string || "";

	let slackEventType: string;
	if (eventType === "app_mention") {
		slackEventType = "slack.mention";
	} else if (channelType === "im") {
		slackEventType = "slack.dm";
	} else if (threadTs) {
		slackEventType = "slack.thread_reply";
	} else {
		// Non-thread channel message — skip
		return new Response("OK");
	}

	const teamId = payload.team_id as string || "";
	const normalizedEvent = {
		id: (payload.event_id as string) || crypto.randomUUID(),
		source: "slack",
		type: slackEventType,
		workspace: teamId,
		timestamp: new Date().toISOString(),
		payload: {
			user_id: event.user as string || "",
			channel: event.channel as string || "",
			channel_type: channelType,
			text: (event.text as string || "").slice(0, 4000),
			ts: event.ts as string || "",
			thread_ts: threadTs,
		},
	};

	const deploymentIds = teamId
		? await findSubscribedDeployments(env, `slack:${teamId}`)
		: [];

	for (const deploymentId of deploymentIds) {
		const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
		const stub = env.DEPLOYMENT_SESSION.get(doId);
		await stub.fetch(new Request("https://internal/event", {
			method: "POST",
			body: JSON.stringify(normalizedEvent),
		}));
	}

	return Response.json({ delivered_to: deploymentIds.length });
}

async function handleRegisterDeployment(request: Request, env: Env): Promise<Response> {
	let body: Record<string, unknown>;
	try {
		body = await request.json() as Record<string, unknown>;
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const name = body.name as string;
	const subscriptions = body.subscriptions as string[];

	if (!name || !subscriptions?.length) {
		return Response.json(
			{ error: "name and subscriptions[] required" },
			{ status: 400 },
		);
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

	// Initialize the Durable Object
	const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
	const stub = env.DEPLOYMENT_SESSION.get(doId);
	await stub.fetch(new Request("https://internal/init", {
		method: "POST",
		body: JSON.stringify({ deployment_id: deploymentId, subscriptions }),
	}));

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
		body = await request.json() as Record<string, unknown>;
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

async function findSubscribedDeployments(env: Env, key: string): Promise<string[]> {
	const data = await env.EVENTS.get(`subscriptions:${key}`);
	return data ? JSON.parse(data) : [];
}

// ---------------------------------------------------------------------------
// Auth: GitHub OAuth callback
// ---------------------------------------------------------------------------

async function handleAuthCallback(request: Request, env: Env): Promise<Response> {
	let body: Record<string, unknown>;
	try {
		body = await request.json() as Record<string, unknown>;
	} catch {
		return new Response("Invalid JSON", { status: 400 });
	}

	const code = body.code as string;
	const redirectUri = body.redirect_uri as string;

	if (!code) {
		return Response.json({ error: "code is required" }, { status: 400 });
	}

	if (!env.GITHUB_CLIENT_ID || !env.GITHUB_CLIENT_SECRET) {
		return Response.json({ error: "GitHub OAuth not configured" }, { status: 500 });
	}

	// Exchange code for access token
	const tokenResp = await fetch("https://github.com/login/oauth/access_token", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"Accept": "application/json",
		},
		body: JSON.stringify({
			client_id: env.GITHUB_CLIENT_ID,
			client_secret: env.GITHUB_CLIENT_SECRET,
			code,
			redirect_uri: redirectUri,
		}),
	});
	const tokenData = await tokenResp.json() as Record<string, unknown>;
	const accessToken = tokenData.access_token as string;

	if (!accessToken) {
		return Response.json(
			{ error: "GitHub token exchange failed", detail: tokenData.error },
			{ status: 400 },
		);
	}

	// Fetch user info
	const userResp = await fetch("https://api.github.com/user", {
		headers: {
			"Authorization": `Bearer ${accessToken}`,
			"User-Agent": "modastack-event-server",
		},
	});
	const userData = await userResp.json() as Record<string, unknown>;
	const githubUserId = userData.id as number;
	const githubUsername = userData.login as string;

	if (!githubUserId || !githubUsername) {
		return Response.json({ error: "Failed to fetch GitHub user info" }, { status: 400 });
	}

	// Create/update user + session
	const sessionToken = `moda_sess_${crypto.randomUUID().replace(/-/g, "")}`;
	const now = new Date().toISOString();

	const userRecord: UserRecord = {
		github_user_id: githubUserId,
		github_username: githubUsername,
		github_token: accessToken,
		session_token: sessionToken,
		created_at: now,
		last_seen: now,
	};

	await env.EVENTS.put(`users:${githubUserId}`, JSON.stringify(userRecord));
	await env.EVENTS.put(`session:${sessionToken}`, JSON.stringify({ github_user_id: githubUserId }));

	return Response.json({
		token: sessionToken,
		github_username: githubUsername,
		github_user_id: githubUserId,
	});
}

// ---------------------------------------------------------------------------
// Auth helper: authenticate user from session token
// ---------------------------------------------------------------------------

async function authenticateUser(request: Request, env: Env): Promise<UserRecord | null> {
	const token = extractBearerToken(request);
	if (!token || !token.startsWith("moda_sess_")) return null;

	const sessionData = await env.EVENTS.get(`session:${token}`);
	if (!sessionData) return null;

	const { github_user_id } = JSON.parse(sessionData);
	const userData = await env.EVENTS.get(`users:${github_user_id}`);
	return userData ? JSON.parse(userData) : null;
}

function extractBearerToken(request: Request): string | null {
	const auth = request.headers.get("authorization") || "";
	if (auth.startsWith("Bearer ")) return auth.slice(7);
	return null;
}

// ---------------------------------------------------------------------------
// Auth helper: check GitHub repo access
// ---------------------------------------------------------------------------

async function checkGitHubRepoAccess(githubToken: string, repoFullName: string): Promise<boolean> {
	try {
		const resp = await fetch(`https://api.github.com/repos/${repoFullName}`, {
			headers: {
				"Authorization": `Bearer ${githubToken}`,
				"User-Agent": "modastack-event-server",
			},
		});
		return resp.status === 200;
	} catch {
		return false;
	}
}

// ---------------------------------------------------------------------------
// DELETE /deployments/{id}
// ---------------------------------------------------------------------------

async function handleDeleteDeployment(request: Request, env: Env, path: string): Promise<Response> {
	const match = path.match(/^\/deployments\/([^/]+)$/);
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

	// Clean up subscription index
	const subscriptions: string[] = deployment.subscriptions || [];
	for (const sub of subscriptions) {
		const key = `subscriptions:${sub}`;
		const existing = await env.EVENTS.get(key);
		if (existing) {
			const ids: string[] = JSON.parse(existing);
			const filtered = ids.filter(id => id !== deploymentId);
			if (filtered.length > 0) {
				await env.EVENTS.put(key, JSON.stringify(filtered));
			} else {
				await env.EVENTS.delete(key);
			}
		}
	}

	// Delete deployment records
	await env.EVENTS.delete(`deployments:${apiKey}`);
	await env.EVENTS.delete(`deployment_id:${deploymentId}`);

	return Response.json({ deleted: true });
}
