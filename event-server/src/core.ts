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
): SlackNormalizationResult {
	if (payload.type === "url_verification") {
		return { event: null, challenge: payload.challenge as string, skip: true };
	}

	if (payload.type !== "event_callback") {
		return { event: null, skip: true };
	}

	const event = payload.event as Record<string, unknown> | undefined;
	if (!event) return { event: null, skip: true };

	if (event.bot_id || event.subtype) {
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
	if (event.repo) keys.push(event.repo);
	if (event.team_key) keys.push(`linear:${event.team_key}`);
	if (event.workspace) {
		keys.push(`slack:${event.workspace}`);
		if (event.channel) {
			keys.push(`slack:${event.workspace}:${event.channel}`);
		}
	}
	return keys;
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

	const sigBase = `v0:${timestamp}:${body}`;
	const key = await crypto.subtle.importKey(
		"raw",
		new TextEncoder().encode(secret),
		{ name: "HMAC", hash: "SHA-256" },
		false,
		["sign"],
	);
	const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(sigBase));
	const hexSig =
		"v0=" +
		Array.from(new Uint8Array(sig))
			.map((b) => b.toString(16).padStart(2, "0"))
			.join("");

	return hexSig === signature;
}

export async function verifyGitHubSignature(
	secret: string,
	body: Uint8Array,
	signatureHeader: string,
): Promise<boolean> {
	if (!signatureHeader) return false;

	const key = await crypto.subtle.importKey(
		"raw",
		new TextEncoder().encode(secret),
		{ name: "HMAC", hash: "SHA-256" },
		false,
		["sign"],
	);
	const sig = await crypto.subtle.sign("HMAC", key, body);
	const expected =
		"sha256=" +
		Array.from(new Uint8Array(sig))
			.map((b) => b.toString(16).padStart(2, "0"))
			.join("");

	return expected === signatureHeader;
}
