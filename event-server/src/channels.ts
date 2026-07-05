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

// Meta Graph API base for WhatsApp Cloud API calls. Overridable so tests can
// stub the Graph API with a local server (local.ts wires
// BOBI_ES_WHATSAPP_API_URL); production always uses the default.
const DEFAULT_WHATSAPP_API_URL = "https://graph.facebook.com/v23.0/";
let whatsappApiUrlOverride: string | undefined;

export function setWhatsAppApiUrl(url: string | undefined): void {
	whatsappApiUrlOverride = url ? (url.endsWith("/") ? url : `${url}/`) : undefined;
}

export function whatsappApiUrl(): string {
	return whatsappApiUrlOverride ?? DEFAULT_WHATSAPP_API_URL;
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
 * Outbound budget enforcement (#651). Terminal sends (post / final) go out
 * whole as natural-boundary chunks via chunkForChannel; streaming updates
 * rewrite one message in place, so they stay on truncateForChannel - chunking
 * a rewrite would post a new message on every tick.
 */
const TRUNCATION_MARKER = "\n_(truncated)_";
/** Runaway guard: past this many chunks the tail is truncated with the marker. */
const MAX_CHUNKS = 8;
/** Appended to a chunk that splits inside a code fence, so it renders closed. */
const FENCE_CLOSE = "\n```";

/** Length of `s` in the channel's declared unit. */
function unitLength(s: string, caps: ChannelCapabilities): number {
	if (caps.lengthUnit === "utf16") return s.length;
	let n = 0;
	for (const _ch of s) n++;
	return n;
}

/**
 * UTF-16 index covering at most `units` of `s` in the channel's unit, never
 * splitting a surrogate pair.
 */
function unitIndex(s: string, units: number, caps: ChannelCapabilities): number {
	if (units <= 0) return 0;
	let idx: number;
	if (caps.lengthUnit === "utf16") {
		idx = Math.min(units, s.length);
	} else {
		idx = 0;
		let counted = 0;
		for (const ch of s) {
			if (counted >= units) break;
			idx += ch.length;
			counted++;
		}
	}
	// Never split a surrogate pair: if the boundary lands between a high and
	// low surrogate, step back one unit.
	if (idx > 0 && idx < s.length) {
		const cc = s.charCodeAt(idx - 1);
		if (cc >= 0xd800 && cc <= 0xdbff) idx--;
	}
	return idx;
}

export function truncateForChannel(text: string, caps: ChannelCapabilities): string {
	if (unitLength(text, caps) <= caps.maxLength) return text;
	// The marker must fit INSIDE the budget - maxLength is the channel's hard
	// limit, and overshooting it fails the whole send (msg_too_long).
	const keep = unitIndex(text, caps.maxLength - TRUNCATION_MARKER.length, caps);
	return text.slice(0, keep) + TRUNCATION_MARKER;
}

/**
 * Fence state after scanning `s`. `open` is "" outside a fence, else the
 * fence header to reopen with (e.g. "```python"). Fence lines toggle; an
 * opening line's info string is preserved for reopening. The info string may
 * not contain backticks (per CommonMark), which keeps a line-leading inline
 * span like ```cmd``` from reading as an opener; an implausibly long "info
 * string" (a one-line blob that happens to start with backticks) reopens as
 * a bare fence so the carried prefix can never blow the chunk budget.
 */
const MAX_FENCE_HEADER = 24;

function fenceStateAfter(s: string, open: string): string {
	for (const line of s.split("\n")) {
		const m = line.match(/^\s*(`{3,})([^`]*)$/);
		if (!m) continue;
		if (open) {
			open = "";
		} else {
			const info = m[2].trim();
			open = info.length > MAX_FENCE_HEADER ? m[1] : m[1] + info;
		}
	}
	return open;
}

/**
 * Split over-budget text into channel-sized chunks at natural boundaries:
 * paragraph break, then line break, then a hard (surrogate-safe) cut. A split
 * inside a code fence closes the fence at the chunk end and reopens it (with
 * its info string) at the start of the next chunk, so every chunk renders.
 * Within-budget text comes back as a single chunk, byte-identical.
 */
export function chunkForChannel(text: string, caps: ChannelCapabilities): string[] {
	if (unitLength(text, caps) <= caps.maxLength) return [text];

	const chunks: string[] = [];
	let remaining = text;
	let reopen = ""; // fence header carried into the next chunk

	while (chunks.length < MAX_CHUNKS - 1) {
		const prefix = reopen ? reopen + "\n" : "";
		if (unitLength(prefix, caps) + unitLength(remaining, caps) <= caps.maxLength) {
			chunks.push(prefix + remaining);
			return chunks;
		}
		// Reserve room for the prefix and a possible fence close so the chunk
		// never overshoots the hard limit (over-reserving costs a few chars).
		const avail = Math.max(
			1, caps.maxLength - unitLength(prefix, caps) - unitLength(FENCE_CLOSE, caps));
		const windowEnd = unitIndex(remaining, avail, caps);
		const window = remaining.slice(0, windowEnd);

		// Natural boundaries, but never a blank chunk: a cut that leaves only
		// whitespace (e.g. leading newlines) would fail the whole send.
		let cut = window.lastIndexOf("\n\n");
		if (cut <= 0 || !window.slice(0, cut).trim()) cut = window.lastIndexOf("\n");
		if (cut <= 0 || !window.slice(0, cut).trim()) cut = windowEnd; // hard split, surrogate-safe

		const head = remaining.slice(0, cut);
		remaining = remaining.slice(cut).replace(/^\n+/, "");

		reopen = fenceStateAfter(head, reopen);
		let chunk = prefix + head;
		if (reopen) chunk += FENCE_CLOSE;
		chunks.push(chunk);
	}

	// Chunk-count guard hit: truncate the tail instead of posting unbounded
	// follow-ups for pathological input, keeping any open fence closed so the
	// final chunk still renders.
	const prefix = reopen ? reopen + "\n" : "";
	let tail = truncateForChannel(prefix + remaining, caps);
	if (fenceStateAfter(tail, "")) {
		tail = truncateForChannel(
			prefix + remaining,
			{ ...caps, maxLength: caps.maxLength - unitLength(FENCE_CLOSE, caps) },
		) + FENCE_CLOSE;
	}
	chunks.push(tail);
	return chunks;
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
			// gateway sends markdown, so the stricter budget applies. The
			// budget has always been enforced in UTF-16 units (JS .length);
			// declare that unit so chunking keeps the proven behavior.
			maxLength: 12000,
			lengthUnit: "utf16",
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

// --- WhatsApp (Meta Cloud API) - #656, epic #190 Phase 3 ---

// One Graph API call. Returns the parsed JSON body; throws on transport
// errors; a Graph error body ({error: {message}}) is surfaced by callers.
async function whatsappApi(
	token: string,
	path: string,
	init: { method?: string; json?: unknown; form?: FormData },
): Promise<Record<string, unknown>> {
	const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
	let body: BodyInit | undefined;
	if (init.json !== undefined) {
		headers["Content-Type"] = "application/json";
		body = JSON.stringify(init.json);
	} else if (init.form) {
		body = init.form; // fetch sets the multipart boundary itself
	}
	const resp = await fetch(`${whatsappApiUrl()}${path}`, {
		method: init.method ?? "POST",
		headers,
		...(body !== undefined ? { body } : {}),
	});
	return (await resp.json()) as Record<string, unknown>;
}

function whatsappError(data: Record<string, unknown>): string {
	const err = data.error as Record<string, unknown> | undefined;
	return (err?.message as string) || "whatsapp api error";
}

const whatsappAdapter: ChannelAdapter = {
	descriptor: {
		name: "whatsapp",
		topicShape: "whatsapp:<phone_number_id>",
		transport: "webhook",
		capabilities: {
			// No message editing on WhatsApp - the gateway degrades placeholder
			// edits to follow-up posts. No typing indicator, no threads, no
			// native streaming: reactive-only replies (epic #190 Phase 3).
			edit: false,
			typing: false,
			streaming: "none",
			threads: false,
			files: true,
			// Cloud API text body limit, counted in characters (code points) -
			// the unit-aware budget #651 wired in.
			maxLength: 4096,
			lengthUnit: "chars",
			// Meta's customer-service window: replies are free-form for 24h
			// after the user's last inbound message; outside it only template
			// messages send (template support is a follow-up - the gateway
			// returns a typed error so the agent can report the situation).
			messageWindow: { hours: 24, outsideWindow: "template" },
		},
		credentials: [
			{ env: "WHATSAPP_ACCESS_TOKEN", secret: true, label: "Cloud API access token" },
			{ env: "WHATSAPP_PHONE_NUMBER_ID", secret: false, label: "Phone number ID" },
			{ env: "WHATSAPP_APP_SECRET", secret: true, label: "Meta app secret (webhook signatures)" },
			{ env: "WHATSAPP_VERIFY_TOKEN", secret: true, label: "Webhook verify token (your choice)" },
		],
		promptHint:
			"WhatsApp renders its own light formatting (*bold*, _italic_, "
			+ "```monospace```), not full markdown. Keep replies short and "
			+ "conversational; avoid headers and tables.",
		setupSkill: "skills/whatsapp-setup.md",
	},

	async send(token, conv, text) {
		try {
			const data = await whatsappApi(token, `${conv.scope}/messages`, {
				json: {
					messaging_product: "whatsapp",
					to: conv.chatId,
					type: "text",
					text: { body: text },
				},
			});
			const id = (data.messages as Array<Record<string, unknown>>)?.[0]?.id as string;
			if (!id) return { ok: false, error: whatsappError(data) };
			return { ok: true, ts: id };
		} catch (err) {
			return { ok: false, error: String(err) };
		}
	},

	// Two-step Cloud API upload: POST the bytes to /{pnid}/media, then send a
	// document message referencing the media id. Sent as `document` for any
	// mime type - it preserves the filename and works for all content. The
	// media endpoint validates the declared MIME, so it is inferred from the
	// filename (octet-stream is not on Meta's supported list).
	async uploadFiles(token, conv, files, comment) {
		let delivered = 0;
		try {
			let lastId = "";
			for (const [i, f] of files.entries()) {
				const mime = mimeForFilename(f.name);
				const form = new FormData();
				form.append("messaging_product", "whatsapp");
				form.append("type", mime);
				form.append("file", new Blob([f.data], { type: mime }), f.name);
				const uploaded = await whatsappApi(token, `${conv.scope}/media`, { form });
				const mediaId = uploaded.id as string;
				if (!mediaId) return { ok: false, error: partialUploadError(whatsappError(uploaded), i, files.length) };

				const caption = i === 0 ? comment || f.title || "" : f.title || "";
				const sent = await whatsappApi(token, `${conv.scope}/messages`, {
					json: {
						messaging_product: "whatsapp",
						to: conv.chatId,
						type: "document",
						document: {
							id: mediaId,
							filename: f.name,
							...(caption ? { caption } : {}),
						},
					},
				});
				const id = (sent.messages as Array<Record<string, unknown>>)?.[0]?.id as string;
				if (!id) return { ok: false, error: partialUploadError(whatsappError(sent), i, files.length) };
				lastId = id;
				delivered = i + 1;
			}
			return { ok: true, ts: lastId };
		} catch (err) {
			return { ok: false, error: partialUploadError(String(err), delivered, files.length) };
		}
	},
};

// Mirror the chunk path's partial-delivery contract: once any file reached
// the user, the error must tell the caller not to blind-retry the batch.
function partialUploadError(error: string, delivered: number, total: number): string {
	if (delivered === 0) return error;
	return `file ${delivered + 1}/${total} failed after partial delivery `
		+ `(do not resend; ${delivered} file(s) are already visible): ${error}`;
}

// Minimal extension map for the document types agents actually send. Meta's
// media endpoint rejects MIME types outside its supported list
// (application/octet-stream is not on it), so unknowns go out as text/plain.
const MIME_BY_EXT: Record<string, string> = {
	pdf: "application/pdf",
	txt: "text/plain",
	md: "text/plain",
	csv: "text/csv",
	png: "image/png",
	jpg: "image/jpeg",
	jpeg: "image/jpeg",
	webp: "image/webp",
	mp4: "video/mp4",
	doc: "application/msword",
	docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	xls: "application/vnd.ms-excel",
	xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
	pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
	zip: "application/zip",
};

function mimeForFilename(name: string): string {
	const ext = name.toLowerCase().split(".").pop() || "";
	return MIME_BY_EXT[ext] || "text/plain";
}

const CHANNEL_ADAPTERS: Record<string, ChannelAdapter> = {
	slack: slackAdapter,
	whatsapp: whatsappAdapter,
};

export function getChannelAdapter(source: string): ChannelAdapter | undefined {
	return CHANNEL_ADAPTERS[source];
}
