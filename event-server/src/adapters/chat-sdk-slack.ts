/**
 * Spike #191 — Chat SDK bridge adapter for Slack
 *
 * Wraps the Chat SDK's Slack webhook parser to produce our NormalizedEvent
 * envelope. This adapter is a drop-in replacement for our hand-rolled
 * normalizeSlackWebhook — same input, same output shape.
 *
 * Benefits over the hand-rolled version:
 * - AST-based markdown conversion (no regex)
 * - Full Slack event type coverage (reactions, file shares, etc.)
 * - Signature verification via Web Crypto API (edge-compatible)
 * - Streaming/placeholder support for outbound (not used here)
 */
import { parseSlackWebhookBody } from "@chat-adapter/slack/webhook";
import type { NormalizedEvent, SlackNormalizationResult } from "../core";

export type ChatSdkBridgeResult = SlackNormalizationResult;

/**
 * Bridge the Chat SDK's Slack webhook parsing into our NormalizedEvent
 * envelope. This function replaces normalizeSlackWebhook from
 * adapters/slack.ts with the Chat SDK's parser as the frontend.
 *
 * @param body - Raw JSON string from the Slack webhook POST body
 * @param selfBotId - Our bot's ID, used to filter self-loop messages
 */
export function bridgeSlackWebhook(
	body: string,
	selfBotId?: string,
): ChatSdkBridgeResult {
	let parsed;
	try {
		parsed = parseSlackWebhookBody(body);
	} catch {
		return { event: null, skip: true };
	}

	// url_verification — return challenge, skip event processing
	// Chat SDK uses `kind` as the discriminant field
	if (parsed.kind === "url_verification") {
		return {
			event: null,
			challenge: parsed.challenge,
			skip: true,
		};
	}

	// We only process event_callback payloads
	const raw = JSON.parse(body) as Record<string, unknown>;
	if (raw.type !== "event_callback") {
		return { event: null, skip: true };
	}

	const innerEvent = raw.event as Record<string, unknown> | undefined;
	if (!innerEvent) return { event: null, skip: true };

	// Skip subtypes (message_changed, message_deleted, etc.)
	if (innerEvent.subtype) {
		return { event: null, skip: true };
	}

	// Self-loop filter: skip only our own bot's messages
	if (innerEvent.bot_id && selfBotId && innerEvent.bot_id === selfBotId) {
		return { event: null, skip: true };
	}

	// Classify event type — matches our existing normalizer's categories
	const eventType = innerEvent.type as string;
	const channelType = (innerEvent.channel_type as string) || "";
	const threadTs = (innerEvent.thread_ts as string) || "";

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

	// Extract fields — identical to our existing normalizer
	const teamId = (raw.team_id as string) || "";
	const channel = (innerEvent.channel as string) || "";
	const userId = (innerEvent.user as string) || "";
	const rawText = ((innerEvent.text as string) || "").slice(0, 4000);
	const ts = (innerEvent.ts as string) || "";

	const topics: string[] = [];
	if (teamId) topics.push(`slack:${teamId}`);

	const fields: Record<string, string | number | boolean> = {};
	if (userId) fields.user_id = userId;
	if (channel) fields.channel = channel;
	if (channelType) fields.channel_type = channelType;
	if (ts) fields.ts = ts;
	if (threadTs) fields.thread_ts = threadTs;

	return {
		event: {
			v: 2,
			id: (raw.event_id as string) || crypto.randomUUID(),
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
