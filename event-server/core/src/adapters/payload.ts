/**
 * Shared narrowing and extraction helpers for inbound webhook adapters.
 *
 * Everything an adapter receives is untrusted provider JSON typed as `unknown`.
 * The house policy — stated in whatsapp.ts ("Runtime type check, not a cast: a
 * numeric `from` would throw…") and followed by linear.ts and discord.ts — is
 * to NARROW at runtime and drop what does not fit, never to assert a shape with
 * a bare `as` cast that a malformed payload propagates straight past.
 *
 * Q117 — linear.ts defined asRecord/stringField module-locally, so github.ts
 * and chat-sdk-slack.ts each grew their own bare-cast style instead. They live
 * here now so all five adapters share one policy.
 *
 * D091 — normalizeAttachments carries the `files` fields contract that agents
 * consume (fields.files = JSON.stringify(files), payload.files = the parsed
 * array, every value stringified). It was hand-written twice, in the Discord
 * and Slack adapters, so a change to the shared contract had to be mirrored by
 * hand or drift.
 */

/** The value as a plain object, or undefined if it is not one. */
export function asRecord(value: unknown): Record<string, unknown> | undefined {
	return value && typeof value === "object" ? value as Record<string, unknown> : undefined;
}

/** `record[key]` when it is actually a string, else undefined. */
export function stringField(
	record: Record<string, unknown> | undefined,
	key: string,
): string | undefined {
	const value = record?.[key];
	return typeof value === "string" ? value : undefined;
}

/** `record[key]` when it is actually a number, else undefined. */
export function numberField(
	record: Record<string, unknown> | undefined,
	key: string,
): number | undefined {
	const value = record?.[key];
	return typeof value === "number" ? value : undefined;
}

/**
 * Provider attachment key → the key agents read. Each adapter owns its own
 * map; the extraction loop below is what they share.
 */
export type AttachmentKeyMap = Readonly<Record<string, string>>;

/** Slack's `files` entries (chat-sdk-slack.ts). */
export const SLACK_FILE_KEYS: AttachmentKeyMap = {
	id: "id",
	name: "name",
	mimetype: "mimetype",
	filetype: "filetype",
	url_private: "url_private",
	url_private_download: "url_private_download",
	size: "size",
};

/** Discord's `attachments` entries (discord.ts). */
export const DISCORD_ATTACHMENT_KEYS: AttachmentKeyMap = {
	id: "id",
	filename: "name",
	content_type: "mimetype",
	url: "url",
	size: "size",
};

/**
 * Copy the mapped keys off each attachment, stringifying every value (agents
 * read `size` as a string, not a number). A key whose source value is absent
 * or falsy is omitted, so consumers can test presence.
 *
 * A non-array input yields no files, and an entry that is not an object is
 * skipped rather than throwing — the drop-don't-propagate policy above.
 */
export function normalizeAttachments(
	raw: unknown,
	keys: AttachmentKeyMap,
): Array<Record<string, string>> {
	if (!Array.isArray(raw)) return [];
	const files: Array<Record<string, string>> = [];
	for (const item of raw) {
		const source = asRecord(item);
		if (!source) continue;
		const entry: Record<string, string> = {};
		for (const [from, to] of Object.entries(keys)) {
			if (source[from]) entry[to] = String(source[from]);
		}
		files.push(entry);
	}
	return files;
}
