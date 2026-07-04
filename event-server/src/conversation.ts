/**
 * Conversation references - channel-agnostic addressing for chat replies (#618).
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
 * number id). Segment values must not contain ":" - buildConversation enforces
 * this, so a platform whose ids carry ":" (Matrix, Teams) needs a grammar
 * extension (escaping or a new trailer), not a silent mis-parse.
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
	const segments = [c.source, c.scope, c.chatType, c.chatId];
	if (c.threadId !== undefined) segments.push(c.threadId);
	for (const s of segments) {
		if (!s || s.includes(":")) {
			throw new Error(`invalid conversation segment: ${JSON.stringify(s)}`);
		}
	}
	const base = `${c.source}:${c.scope}:${c.chatType}:${c.chatId}`;
	return c.threadId ? `${base}:thread:${c.threadId}` : base;
}

export function parseConversation(ref: string): Conversation | null {
	if (typeof ref !== "string") return null;
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

// Build the reply address for an inbound Slack message, or undefined when the
// payload lacks usable ids. Owns the reply-anchoring policy: replies land in
// the originating thread, and a top-level message anchors its own thread (ts),
// matching where the placeholder handler posts.
export function slackConversation(
	teamId: string,
	channel: string,
	channelType: string,
	ts: string,
	threadTs?: string,
): string | undefined {
	if (!teamId || !channel) return undefined;
	const anchor = threadTs || ts;
	try {
		return buildConversation({
			source: "slack",
			scope: teamId,
			chatType: slackChatType(channelType),
			chatId: channel,
			...(anchor ? { threadId: anchor } : {}),
		});
	} catch {
		// A colon-bearing id in the webhook payload must not kill normalization;
		// the event just ships without a reply address.
		return undefined;
	}
}
