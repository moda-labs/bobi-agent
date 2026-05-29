export { DeploymentSession } from "./deployment-session";

interface Env {
	EVENTS: KVNamespace;
	DEPLOYMENT_SESSION: DurableObjectNamespace;
	WEBHOOK_SECRET?: string;
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
		if (request.method === "POST" && path === "/deployments") {
			return handleRegisterDeployment(request, env);
		}
		if (request.method === "GET" && path.startsWith("/deployments/") && path.endsWith("/subscribe")) {
			return handleSubscribe(request, env, path);
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

async function findSubscribedDeployments(env: Env, key: string): Promise<string[]> {
	const data = await env.EVENTS.get(`subscriptions:${key}`);
	return data ? JSON.parse(data) : [];
}
