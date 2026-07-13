/**
 * Discord inbound normalizer (Gateway MESSAGE_CREATE dispatches).
 *
 * One MESSAGE_CREATE becomes at most one NormalizedEvent on the global topic
 * `discord:<application_id>` with the reply address
 * `discord:<application_id>:channel:<channel_id>` (guild channels; Discord
 * threads are themselves channels) or `discord:<application_id>:dm:<channel_id>`
 * (the grammar in ../conversation.ts).
 *
 * v1 surface is deliberately narrow: DMs, guild messages that @mention the
 * bot, and replies to the bot's own messages. The full-channel firehose is
 * non-scope - it floods delivery and needs the privileged MESSAGE_CONTENT
 * intent anyway (DMs and bot-mentions are exempt from it).
 */
import type { NormalizedEvent } from "../core.js";
import { buildConversation } from "../conversation.js";

/**
 * Shown when Discord withholds a guild message's content (the privileged
 * MESSAGE_CONTENT intent is not enabled and the exemption did not apply,
 * e.g. a reply whose auto-mention was suppressed).
 */
const NO_CONTENT_MARKER = "[message content unavailable - enable the Message Content intent]";

/**
 * Normalize one MESSAGE_CREATE dispatch payload. Never throws on malformed
 * input - unknown shapes yield zero events (a bad frame must not kill the
 * Gateway connection).
 *
 * `botUserId` is the connection's own bot user (from the READY dispatch);
 * `applicationId` scopes topics and conversation refs.
 */
export function normalizeDiscordMessage(
	d: Record<string, unknown>,
	botUserId: string,
	applicationId: string,
): NormalizedEvent[] {
	const author = d.author as Record<string, unknown> | undefined;
	const authorId = typeof author?.id === "string" ? author.id : "";
	const channelId = typeof d.channel_id === "string" ? d.channel_id : "";
	const messageId = typeof d.id === "string" ? d.id : "";
	if (!authorId || !channelId || !messageId) return [];

	// Loop prevention: drop every bot-authored message, including our own
	// reply echoing back over the Gateway (the circuit breaker stays as
	// backstop). Third-party bots are also dropped - bot-to-bot chatter is
	// the classic Discord feedback loop.
	if (author?.bot) return [];

	const guildId = typeof d.guild_id === "string" ? d.guild_id : "";
	const mentions = Array.isArray(d.mentions) ? (d.mentions as Array<Record<string, unknown>>) : [];
	const mentionsBot = mentions.some((u) => u?.id === botUserId);
	const referenced = d.referenced_message as Record<string, unknown> | undefined;
	const isReplyToBot =
		(referenced?.author as Record<string, unknown> | undefined)?.id === botUserId;

	// Classification order: reply-to-bot before mention - a reply usually
	// carries the bot in `mentions` too (Discord's auto-mention), and the
	// reply type is the more specific signal.
	let type: string;
	if (!guildId) {
		type = "discord.dm";
	} else if (isReplyToBot) {
		type = "discord.reply";
	} else if (mentionsBot) {
		type = "discord.mention";
	} else {
		return []; // guild message not addressed to us - non-scope in v1
	}

	let conversation: string;
	try {
		conversation = buildConversation({
			source: "discord",
			scope: applicationId,
			chatType: guildId ? "channel" : "dm",
			chatId: channelId,
		});
	} catch {
		return []; // an id carrying ":" cannot be addressed - drop, never throw
	}

	const userName =
		(typeof author?.global_name === "string" && author.global_name)
		|| (typeof author?.username === "string" ? author.username : "")
		|| authorId;

	const rawAttachments = Array.isArray(d.attachments)
		? (d.attachments as Array<Record<string, unknown>>)
		: [];
	const files: Array<Record<string, string>> = [];
	for (const a of rawAttachments) {
		const entry: Record<string, string> = {};
		if (a.id) entry.id = String(a.id);
		if (a.filename) entry.name = String(a.filename);
		if (a.content_type) entry.mimetype = String(a.content_type);
		if (a.url) entry.url = String(a.url);
		if (a.size) entry.size = String(a.size);
		files.push(entry);
	}

	// Empty content is only "withheld" when nothing else came through: an
	// attachment-only message (a DM'd screenshot, say) legitimately has no
	// content, and telling the agent to enable an intent would be wrong.
	let text: string;
	if (typeof d.content === "string" && d.content) {
		text = d.content;
	} else if (files.length > 0) {
		text = `[attachment] ${files.map((f) => f.name || "file").join(", ")}`;
	} else {
		text = NO_CONTENT_MARKER;
	}
	text = text.slice(0, 4000);

	// The message's own send time when Discord stamped one - resume replays
	// deliver messages minutes late, and "now" would misdate them.
	const sentAt = Date.parse(typeof d.timestamp === "string" ? d.timestamp : "");

	const fields: Record<string, string | number | boolean> = {
		user_id: authorId,
		user_name: userName,
		channel_id: channelId,
		message_id: messageId,
		application_id: applicationId,
	};
	if (guildId) fields.guild_id = guildId;
	if (files.length > 0) fields.files = JSON.stringify(files);

	return [{
		v: 2,
		// The message snowflake: Discord replays missed dispatches on resume,
		// so downstream dedup needs a key stable across redeliveries.
		id: messageId,
		source: "discord",
		type,
		topics: [`discord:${applicationId}`],
		delivery: "chat",
		text,
		conversation,
		fields,
		timestamp: Number.isNaN(sentAt)
			? new Date().toISOString()
			: new Date(sentAt).toISOString(),
		payload: {
			user_id: authorId,
			user_name: userName,
			channel_id: channelId,
			message_id: messageId,
			text,
			...(guildId ? { guild_id: guildId } : {}),
			...(files.length > 0 ? { files } : {}),
		},
	}];
}
