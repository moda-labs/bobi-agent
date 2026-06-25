import type { NormalizedEvent, SlackNormalizationResult } from "../core";

export function normalizeSlackWebhook(
	payload: Record<string, unknown>,
	// A workspace may host SEVERAL of our bots (one per team), so the self-filter
	// takes a SET of our bot ids — not one. A single id meant the second bot to
	// register clobbered the first, and the first then looped on its own messages
	// (self-spam incident 2026-06-24). A bare string is still accepted for
	// back-compat with single-bot callers/tests.
	selfBotIds?: string | Iterable<string>,
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

	const botId = (event.bot_id as string) || "";

	// Filter messages authored by ANY of OUR bots in this workspace — both to
	// stop a bot looping on itself and to stop two of our bots looping on each
	// other. Messages from third-party bots (not ours) pass through.
	const selfSet =
		typeof selfBotIds === "string"
			? new Set([selfBotIds])
			: new Set(selfBotIds ?? []);
	if (botId && selfSet.has(botId)) {
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
	const appId = (payload.api_app_id as string) || "";
	const channel = (event.channel as string) || "";
	const userId = (event.user as string) || "";
	const rawText = ((event.text as string) || "").slice(0, 4000);
	const ts = (event.ts as string) || "";

	const isDm = channelType === "im" || channelType === "mpim";

	const topics: string[] = [];
	if (teamId) {
		if (appId) {
			topics.push(`slack:${teamId}:app:${appId}`);
		} else {
			topics.push(`slack:${teamId}`);
		}
		// Channel-scoped topic so multiple teams can share one workspace/bot,
		// each subscribing only to its own channel(s). App-qualified topics keep
		// two apps in the same workspace from receiving each other's DMs.
		// Legacy workspace/channel topics are emitted only when Slack omits
		// api_app_id. Once app_id is present, emitting both app-qualified and
		// legacy topics lets stale legacy subscriptions cross-deliver events
		// between apps in the same workspace.
		if (channel && !isDm) {
			if (appId) {
				topics.push(`slack:${teamId}:app:${appId}:${channel}`);
			} else {
				topics.push(`slack:${teamId}:${channel}`);
			}
		}
	}

	// Extract file attachments (images, documents, etc.) from the event.
	// Slack includes a `files` array on messages with shared files.
	const rawFiles = event.files as Array<Record<string, unknown>> | undefined;
	const files: Array<Record<string, string>> = [];
	if (Array.isArray(rawFiles)) {
		for (const f of rawFiles) {
			const entry: Record<string, string> = {};
			if (f.id) entry.id = String(f.id);
			if (f.name) entry.name = String(f.name);
			if (f.mimetype) entry.mimetype = String(f.mimetype);
			if (f.filetype) entry.filetype = String(f.filetype);
			if (f.url_private) entry.url_private = String(f.url_private);
			if (f.url_private_download)
				entry.url_private_download = String(f.url_private_download);
			if (f.size) entry.size = String(f.size);
			files.push(entry);
		}
	}

	const fields: Record<string, string | number | boolean> = {};
	if (userId) fields.user_id = userId;
	if (channel) fields.channel = channel;
	if (channelType) fields.channel_type = channelType;
	if (appId) fields.api_app_id = appId;
	if (ts) fields.ts = ts;
	if (threadTs) fields.thread_ts = threadTs;
	// Carry bot_id through so the circuit breaker can recognise bot-authored
	// events (it reads payload.bot_id). Stripping it here is what blinded the
	// breaker to Slack loops. This only ever survives for THIRD-PARTY bots —
	// our own bots are filtered above.
	if (botId) fields.bot_id = botId;
	if (files.length > 0) fields.files = JSON.stringify(files);

	return {
		event: {
			v: 2,
			id: (payload.event_id as string) || crypto.randomUUID(),
			source: "slack",
			type: slackEventType,
			topics,
			delivery: "chat",
			text: rawText,
			fields,
			timestamp: new Date().toISOString(),
			payload: {
				user_id: userId,
				channel,
				channel_type: channelType,
				text: rawText,
				ts,
				thread_ts: threadTs,
				...(botId ? { bot_id: botId } : {}),
				...(files.length > 0 ? { files } : {}),
			},
		},
		skip: false,
	};
}
