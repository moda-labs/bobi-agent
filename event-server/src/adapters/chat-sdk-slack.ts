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
import { slackConversation } from "../conversation";
import { escapeRegExp } from "./slack";

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
	// Same shapes as normalizeSlackWebhook: a workspace may host several of
	// our bots, so both filters take a set (bare string accepted for
	// single-bot callers/tests).
	selfBotIds?: string | Iterable<string>,
	selfBotUserIds?: string | Iterable<string>,
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

	// Self-loop filter: skip messages authored by ANY of our bots, matching
	// normalizeSlackWebhook's multi-bot set semantics.
	const selfSet =
		typeof selfBotIds === "string"
			? new Set([selfBotIds])
			: new Set(selfBotIds ?? []);
	if (innerEvent.bot_id && selfSet.has(innerEvent.bot_id as string)) {
		return { event: null, skip: true };
	}

	// Classify event type — matches our existing normalizer's categories
	const eventType = innerEvent.type as string;
	const channelType = (innerEvent.channel_type as string) || "";
	const threadTs = (innerEvent.thread_ts as string) || "";
	const rawText = ((innerEvent.text as string) || "").slice(0, 4000);

	// Mention/message dedup, identical to normalizeSlackWebhook: a channel
	// @mention arrives as both app_mention and message.* with the same ts;
	// drop the message copy so one human message yields one event.
	const selfUserSet =
		typeof selfBotUserIds === "string"
			? new Set([selfBotUserIds])
			: new Set(selfBotUserIds ?? []);
	const mentionsSelfUser =
		selfUserSet.size > 0
		&& [...selfUserSet].some((id) =>
			new RegExp(`<@${escapeRegExp(id)}(?:\\|[^>]+)?>`).test(rawText));

	let slackEventType: string;
	if (eventType === "app_mention") {
		slackEventType = "slack.mention";
	} else if (eventType === "message" && mentionsSelfUser && channelType !== "im" && channelType !== "mpim") {
		return { event: null, skip: true };
	} else if (channelType === "im" || channelType === "mpim") {
		slackEventType = "slack.dm";
	} else if (threadTs) {
		slackEventType = "slack.thread_reply";
	} else {
		return { event: null, skip: true };
	}

	// Extract fields — identical to our existing normalizer
	const teamId = (raw.team_id as string) || "";
	const appId = (raw.api_app_id as string) || "";
	const channel = (innerEvent.channel as string) || "";
	const userId = (innerEvent.user as string) || "";
	const ts = (innerEvent.ts as string) || "";
	const isDm = channelType === "im" || channelType === "mpim";

	// Topic emission matches normalizeSlackWebhook: legacy workspace/channel
	// topics ONLY when Slack omits api_app_id. Emitting both app-qualified and
	// legacy topics lets stale legacy subscriptions cross-deliver events
	// between two apps in the same workspace.
	const topics: string[] = [];
	if (teamId) {
		if (appId) {
			topics.push(`slack:${teamId}:app:${appId}`);
		} else {
			topics.push(`slack:${teamId}`);
		}
		if (channel && !isDm) {
			if (appId) {
				topics.push(`slack:${teamId}:app:${appId}:${channel}`);
			} else {
				topics.push(`slack:${teamId}:${channel}`);
			}
		}
	}

	const botId = (innerEvent.bot_id as string) || "";

	const fields: Record<string, string | number | boolean> = {};
	if (userId) fields.user_id = userId;
	if (channel) fields.channel = channel;
	if (channelType) fields.channel_type = channelType;
	if (appId) fields.api_app_id = appId;
	if (ts) fields.ts = ts;
	if (threadTs) fields.thread_ts = threadTs;
	// Preserve bot_id so the circuit breaker can detect bot authorship.
	if (botId) fields.bot_id = botId;

	// Channel-agnostic reply address (#618). Anchoring policy lives in
	// slackConversation so both normalizers cannot diverge.
	const conversation = slackConversation(teamId, channel, channelType, ts, threadTs);

	return {
		event: {
			v: 2,
			id: (raw.event_id as string) || crypto.randomUUID(),
			source: "slack",
			type: slackEventType,
			topics,
			delivery: "chat",
			text: rawText,
			...(conversation ? { conversation } : {}),
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
			},
		},
		skip: false,
	};
}
