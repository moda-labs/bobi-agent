/**
 * Conversation references — channel-agnostic addressing for chat replies (#618).
 *
 * A conversation reference is an opaque string the agent echoes back verbatim
 * to reply into the thread/DM an inbound event came from. Only adapters build
 * refs and only the gateway parses them; the agent never learns platform
 * addressing semantics.
 *
 * Grammar:
 *   <source>:<scope>:<chat_type>:<chat_id>[:thread:<thread_id>]
 *
 * where <scope> is the platform's tenancy unit (Slack team id, WhatsApp phone
 * number id). Segment values must not contain ":" — true for every platform id
 * shape we carry (Slack T/C/D ids and message ts, phone numbers).
 */

export type ChatType = "dm" | "group" | "channel";

export interface Conversation {
	source: string;
	scope: string;
	chatType: ChatType;
	chatId: string;
	threadId?: string;
}

const CHAT_TYPES: ReadonlySet<string> = new Set(["dm", "group", "channel"]);

export function buildConversation(c: Conversation): string {
	const base = `${c.source}:${c.scope}:${c.chatType}:${c.chatId}`;
	return c.threadId ? `${base}:thread:${c.threadId}` : base;
}

export function parseConversation(ref: string): Conversation | null {
	const parts = ref.split(":");
	if (parts.length !== 4 && parts.length !== 6) return null;
	if (parts.some((p) => p === "")) return null;
	const [source, scope, chatType, chatId] = parts;
	if (!CHAT_TYPES.has(chatType)) return null;
	const conv: Conversation = {
		source,
		scope,
		chatType: chatType as ChatType,
		chatId,
	};
	if (parts.length === 6) {
		if (parts[4] !== "thread") return null;
		conv.threadId = parts[5];
	}
	return conv;
}

// Slack channel_type -> chat_type. "im" is a 1:1 DM, "mpim" a group DM;
// public ("channel") and private ("group") channels both address as channel.
export function slackChatType(channelType: string): ChatType {
	if (channelType === "im") return "dm";
	if (channelType === "mpim") return "group";
	return "channel";
}

export function slackConversation(
	teamId: string,
	channel: string,
	channelType: string,
	threadTs?: string,
): string {
	return buildConversation({
		source: "slack",
		scope: teamId,
		chatType: slackChatType(channelType),
		chatId: channel,
		...(threadTs ? { threadId: threadTs } : {}),
	});
}
