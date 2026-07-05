/**
 * Channel adapters - outbound contract for the channel gateway (#190 Phase 2).
 *
 * Each chat channel is one adapter implementing this contract, registered in
 * CHANNEL_ADAPTERS. The generic /channels/* handlers in core.ts parse a
 * conversation reference, resolve the sending credential, and delegate here;
 * they never learn platform semantics. Capability degradation is the
 * gateway's job: an `update` on a channel without edit support becomes a
 * follow-up post, `typing` on a channel without indicators is a silent no-op.
 *
 * The Slack adapter is built on the Chat SDK's api module
 * (@chat-adapter/slack/api) - the same package whose webhook parser serves
 * inbound (adapters/chat-sdk-slack.ts). Outbound text arrives as markdown and
 * is delivered natively via Slack's `markdown_text`, so callers never convert
 * client-side.
 */
import {
	postSlackMessage,
	updateSlackMessage,
	uploadSlackFiles,
	fetchSlackThreadReplies,
	callSlackApi,
	SlackApiError,
	type SlackMessageOptions,
} from "@chat-adapter/slack/api";
import type { Conversation } from "./conversation";

export interface CredentialSpec {
	env: string;
	secret: boolean;
	label: string;
}

// Slack Web API base. Overridable so integration tests can stub the Slack
// API with a local server (local.ts wires BOBI_ES_SLACK_API_URL); production
// always uses the default. Must end with "/" - the SDK resolves method names
// relative to it.
const DEFAULT_SLACK_API_URL = "https://slack.com/api/";
let slackApiUrlOverride: string | undefined;

export function setSlackApiUrl(url: string | undefined): void {
	slackApiUrlOverride = url ? (url.endsWith("/") ? url : `${url}/`) : undefined;
}

export function slackApiUrl(): string {
	return slackApiUrlOverride ?? DEFAULT_SLACK_API_URL;
}

export interface ChannelCapabilities {
	edit: boolean;
	typing: boolean;
	streaming: "native" | "edit-fallback" | "none";
	threads: boolean;
	files: boolean;
	/** Outbound message budget; text beyond it is truncated with a marker. */
	maxLength: number;
	lengthUnit: "chars" | "utf16";
	messageWindow?: { hours: number; outsideWindow: "template" | "blocked" };
}

/**
 * One struct declaring a channel's entire integration surface, so adding a
 * channel means one adapter module and one registry entry, with zero core
 * edits (epic #190 design).
 */
export interface ChannelDescriptor {
	name: string;
	topicShape: string;
	transport: "webhook" | "socket";
	capabilities: ChannelCapabilities;
	credentials: CredentialSpec[];
	promptHint: string;
	setupSkill: string;
}

export interface SendResult {
	ok: boolean;
	/** Platform message id of the posted/updated message (Slack: ts). */
	ts?: string;
	error?: string;
}

export interface OutboundFile {
	name: string;
	data: Uint8Array;
	title?: string;
}

export interface ConversationMessage {
	user: string;
	text: string;
	ts: string;
	files?: Array<Record<string, string>>;
}

export interface ChannelAdapter {
	descriptor: ChannelDescriptor;
	send(token: string, conv: Conversation, text: string, opts?: { markdown?: boolean }): Promise<SendResult>;
	update?(token: string, conv: Conversation, messageId: string, text: string, opts?: { markdown?: boolean }): Promise<SendResult>;
	/** Set (on) or clear (off) the channel's thinking/typing indicator. Best-effort. */
	typing?(token: string, conv: Conversation, on: boolean): Promise<void>;
	fetchConversation?(token: string, conv: Conversation, limit?: number): Promise<ConversationMessage[]>;
	uploadFiles?(token: string, conv: Conversation, files: OutboundFile[], comment?: string): Promise<SendResult>;
}

/**
 * Truncate outbound text at the channel's declared budget. Matches the
 * behavior the Python client applied before conversion moved server-side;
 * natural-boundary chunking is a deliberate follow-up (epic #190).
 */
const TRUNCATION_MARKER = "\n_(truncated)_";

export function truncateForChannel(text: string, caps: ChannelCapabilities): string {
	if (text.length <= caps.maxLength) return text;
	// The marker must fit INSIDE the budget - maxLength is the channel's hard
	// limit, and overshooting it fails the whole send (msg_too_long).
	return text.slice(0, caps.maxLength - TRUNCATION_MARKER.length) + TRUNCATION_MARKER;
}

function slackError(err: unknown): string {
	if (err instanceof SlackApiError) {
		return (err.response?.error as string) || err.message;
	}
	return String(err);
}

function toConversationMessage(raw: Record<string, unknown>): ConversationMessage {
	const entry: ConversationMessage = {
		user: (raw.user as string) || "",
		text: (raw.text as string) || "",
		ts: (raw.ts as string) || "",
	};
	const rawFiles = raw.files as Array<Record<string, unknown>> | undefined;
	if (Array.isArray(rawFiles) && rawFiles.length > 0) {
		entry.files = rawFiles.map((f) => ({
			id: String(f.id ?? ""),
			name: String(f.name ?? ""),
			mimetype: String(f.mimetype ?? ""),
			url_private: String(f.url_private ?? ""),
		}));
	}
	return entry;
}

// Message body for a Slack post/update. Raw markdown goes out as Slack's
// native `markdown_text` (AST-rendered by Slack, 12k budget); pre-converted
// mrkdwn from the legacy /slack/send path goes out as `text` unchanged.
function slackContent(text: string, markdown: boolean): Pick<SlackMessageOptions, "markdownText" | "text"> {
	return markdown ? { markdownText: text } : { text };
}

const slackAdapter: ChannelAdapter = {
	descriptor: {
		name: "slack",
		topicShape: "slack:<team_id>[:app:<app_id>][:<channel>]",
		transport: "webhook",
		capabilities: {
			edit: true,
			typing: true,
			streaming: "native",
			threads: true,
			files: true,
			// Slack's markdown_text limit; plain text allows ~40k but the
			// gateway sends markdown, so the stricter budget applies.
			maxLength: 12000,
			lengthUnit: "chars",
		},
		credentials: [
			{ env: "SLACK_BOT_TOKEN", secret: true, label: "Bot User OAuth Token" },
			{ env: "SLACK_SIGNING_SECRET", secret: true, label: "App Signing Secret" },
		],
		promptHint:
			"Slack renders standard markdown. Keep replies concise; use "
			+ "triple-backtick blocks for multi-line code.",
		setupSkill: "skills/slack-setup.md",
	},

	async send(token, conv, text, opts) {
		try {
			const posted = await postSlackMessage({
				apiUrl: slackApiUrl(),
				token,
				channel: conv.chatId,
				...(conv.threadId ? { threadTs: conv.threadId } : {}),
				...slackContent(text, opts?.markdown ?? false),
			});
			return { ok: true, ts: posted.id };
		} catch (err) {
			return { ok: false, error: slackError(err) };
		}
	},

	async update(token, conv, messageId, text, opts) {
		try {
			const posted = await updateSlackMessage({
				apiUrl: slackApiUrl(),
				token,
				channel: conv.chatId,
				ts: messageId,
				...slackContent(text, opts?.markdown ?? false),
			});
			return { ok: true, ts: posted.id };
		} catch (err) {
			return { ok: false, error: slackError(err) };
		}
	},

	// Slack's "typing" is the assistant thread status ("is thinking..."),
	// which only exists in thread context and expires after ~2 minutes -
	// callers refresh while work runs. Best-effort: only DM threads support
	// it, so failures are swallowed.
	async typing(token, conv, on) {
		if (!conv.threadId) return;
		try {
			await callSlackApi("assistant.threads.setStatus", {
				channel_id: conv.chatId,
				thread_ts: conv.threadId,
				status: on ? "is thinking…" : "",
			}, { apiUrl: slackApiUrl(), token });
		} catch {
			// non-fatal
		}
	},

	async fetchConversation(token, conv, limit = 100) {
		// A ref without a thread anchor reads the channel/DM history instead
		// of thread replies (e.g. a proactive conversation with no thread).
		if (!conv.threadId) {
			const raw = await callSlackApi("conversations.history", {
				channel: conv.chatId,
				limit: Math.min(limit, 200),
			}, { apiUrl: slackApiUrl(), token });
			const rows = (raw.messages as Array<Record<string, unknown>> | undefined) ?? [];
			// conversations.history returns newest-first; deliver oldest-first
			// to match the thread-replies ordering.
			return rows.map(toConversationMessage).reverse().slice(0, limit);
		}
		const messages: ConversationMessage[] = [];
		let cursor: string | undefined;
		while (messages.length < limit) {
			const page = await fetchSlackThreadReplies({
				apiUrl: slackApiUrl(),
				token,
				channel: conv.chatId,
				ts: conv.threadId,
				limit: Math.min(limit - messages.length, 200),
				...(cursor ? { cursor } : {}),
			});
			for (const raw of page.messages as Array<Record<string, unknown>>) {
				messages.push(toConversationMessage(raw));
			}
			cursor = page.nextCursor;
			if (!cursor) break;
		}
		return messages.slice(0, limit);
	},

	async uploadFiles(token, conv, files, comment) {
		try {
			const result = await uploadSlackFiles(
				files.map((f) => ({
					data: f.data,
					filename: f.name,
					...(f.title ? { title: f.title } : {}),
				})),
				{
					apiUrl: slackApiUrl(),
					token,
					channelId: conv.chatId,
					...(conv.threadId ? { threadTs: conv.threadId } : {}),
					...(comment ? { initialComment: comment } : {}),
				},
			);
			return { ok: true, ts: result.fileIds[0] };
		} catch (err) {
			return { ok: false, error: slackError(err) };
		}
	},
};

const CHANNEL_ADAPTERS: Record<string, ChannelAdapter> = {
	slack: slackAdapter,
};

export function getChannelAdapter(source: string): ChannelAdapter | undefined {
	return CHANNEL_ADAPTERS[source];
}
