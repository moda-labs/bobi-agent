import type { NormalizedEvent, SlackNormalizationResult } from "../core";

export function normalizeSlackWebhook(
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
	const userId = (event.user as string) || "";
	const rawText = ((event.text as string) || "").slice(0, 4000);
	const ts = (event.ts as string) || "";

	const topics: string[] = [];
	if (teamId) {
		topics.push(`slack:${teamId}`);
		// Channel-scoped topic so multiple teams can share one workspace/bot,
		// each subscribing only to its own channel(s). The workspace-level
		// topic above stays for teams that want every message.
		if (channel) topics.push(`slack:${teamId}:${channel}`);
	}

	const fields: Record<string, string | number | boolean> = {};
	if (userId) fields.user_id = userId;
	if (channel) fields.channel = channel;
	if (channelType) fields.channel_type = channelType;
	if (ts) fields.ts = ts;
	if (threadTs) fields.thread_ts = threadTs;

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
			},
		},
		skip: false,
	};
}
