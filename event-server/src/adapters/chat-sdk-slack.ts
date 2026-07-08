/**
 * Chat SDK adapter for Slack inbound webhooks (#191, #628).
 *
 * Wraps the Chat SDK's Slack webhook parser to produce our NormalizedEvent
 * envelope. This is the only Slack inbound normalizer; it replaced the
 * hand-rolled normalizeSlackWebhook once the bridge soaked (#629, #647).
 */
import { parseSlackWebhookBody } from "@chat-adapter/slack/webhook";
import type { NormalizedEvent, SlackNormalizationResult } from "../core";
import { slackConversation } from "../conversation";

export type ChatSdkBridgeResult = SlackNormalizationResult;

function escapeRegExp(value: string): string {
	return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Whether *text* @-mentions any of OUR bot users (`<@Uxxx>` or `<@Uxxx|label>`).
 * Accepts a bare string for single-bot callers/tests.
 */
export function mentionsAnySelfUser(
	text: string,
	selfBotUserIds?: string | Iterable<string>,
): boolean {
	const ids =
		typeof selfBotUserIds === "string"
			? new Set([selfBotUserIds])
			: new Set(selfBotUserIds ?? []);
	return ids.size > 0
		&& [...ids].some((id) =>
			new RegExp(`<@${escapeRegExp(id)}(?:\\|[^>]+)?>`).test(text));
}

/**
 * Bridge the Chat SDK's Slack webhook parsing into our NormalizedEvent
 * envelope.
 *
 * @param body - Raw JSON string from the Slack webhook POST body
 * @param selfBotId - Our bot's ID, used to filter self-loop messages
 * @param payload - Optional pre-parsed body (callers that already parsed it
 *   for signature/challenge handling pass it to avoid a second JSON.parse)
 */
export function bridgeSlackWebhook(
	body: string,
	// A workspace may host several of our bots, so both filters take a set
	// (bare string accepted for single-bot callers/tests).
	selfBotIds?: string | Iterable<string>,
	selfBotUserIds?: string | Iterable<string>,
	payload?: Record<string, unknown>,
): ChatSdkBridgeResult {
	let parsed;
	try {
		parsed = parseSlackWebhookBody(body);
	} catch (err) {
		// Never silent: a parser rejection here drops a user message. Keep the
		// skip (the route must still 200 so Slack stops retrying) but log it.
		console.warn(`chat-sdk bridge failed to parse Slack webhook: ${String(err)}`);
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
	const raw = payload ?? (JSON.parse(body) as Record<string, unknown>);
	if (raw.type !== "event_callback") {
		return { event: null, skip: true };
	}

	const innerEvent = raw.event as Record<string, unknown> | undefined;
	if (!innerEvent) return { event: null, skip: true };

	// Skip subtypes (message_changed, message_deleted, etc.)
	if (innerEvent.subtype) {
		return { event: null, skip: true };
	}

	// Self-loop filter: skip messages authored by ANY of our bots - both to
	// stop a bot looping on itself and to stop two of our bots looping on
	// each other. Messages from third-party bots (not ours) pass through.
	const selfSet =
		typeof selfBotIds === "string"
			? new Set([selfBotIds])
			: new Set(selfBotIds ?? []);
	if (innerEvent.bot_id && selfSet.has(innerEvent.bot_id as string)) {
		return { event: null, skip: true };
	}

	// Classify event type
	const eventType = innerEvent.type as string;
	const channelType = (innerEvent.channel_type as string) || "";
	const threadTs = (innerEvent.thread_ts as string) || "";
	const rawText = ((innerEvent.text as string) || "").slice(0, 4000);

	// Mention/message dedup: a channel @mention arrives as both app_mention
	// and message.* with the same ts; drop the message copy so one human
	// message yields one event.
	const mentionsSelfUser = mentionsAnySelfUser(rawText, selfBotUserIds);

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

	const teamId = (raw.team_id as string) || "";
	const appId = (raw.api_app_id as string) || "";
	const channel = (innerEvent.channel as string) || "";
	const userId = (innerEvent.user as string) || "";
	const ts = (innerEvent.ts as string) || "";
	const isDm = channelType === "im" || channelType === "mpim";

	// Legacy workspace/channel topics are emitted ONLY when Slack omits
	// api_app_id. Emitting both app-qualified and legacy topics lets stale
	// legacy subscriptions cross-deliver events between two apps in the same
	// workspace.
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

	// Extract file attachments (images, documents, etc.) from the event.
	// Slack includes a `files` array on messages with shared files.
	const rawFiles = innerEvent.files as Array<Record<string, unknown>> | undefined;
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
	// Preserve bot_id so the circuit breaker can detect bot authorship.
	if (botId) fields.bot_id = botId;
	if (files.length > 0) fields.files = JSON.stringify(files);

	// Channel-agnostic reply address (#618). Anchoring policy lives in
	// slackConversation.
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
				...(files.length > 0 ? { files } : {}),
			},
		},
		skip: false,
	};
}
