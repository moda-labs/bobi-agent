/**
 * WhatsApp (Meta Cloud API) inbound normalizer (#656, epic #190 Phase 3).
 *
 * Hand-rolled: the Cloud API surface used here (webhook parse, text send,
 * media upload) is small enough not to earn the @chat-adapter/whatsapp
 * dependency; re-evaluate it when template messaging is built.
 * Meta POSTs webhook payloads shaped
 * `{object, entry: [{changes: [{field, value}]}]}`; the `messages` field
 * carries user messages, `statuses` carries delivery receipts (skipped).
 *
 * One inbound user message becomes one NormalizedEvent on the global topic
 * `whatsapp:<phone_number_id>` with the reply address
 * `whatsapp:<pnid>:dm:<wa_id>` (the grammar in ../conversation.ts).
 */
import type { NormalizedEvent } from "../core.js";
import { buildConversation } from "../conversation.js";

export interface WhatsAppNormalization {
	events: NormalizedEvent[];
}

/** Human-readable fallback text for non-text message types. */
function messageText(msg: Record<string, unknown>): string {
	const type = (msg.type as string) || "unknown";
	if (type === "text") {
		return ((msg.text as Record<string, unknown>)?.body as string) || "";
	}
	// Media messages carry an optional caption; surface it with a type marker
	// so the agent knows a non-text payload arrived.
	const media = msg[type] as Record<string, unknown> | undefined;
	const caption = (media?.caption as string) || "";
	return caption ? `[${type}] ${caption}` : `[${type} message]`;
}

/**
 * Normalize one Meta webhook payload into NormalizedEvents. Never throws on
 * malformed input - unknown shapes yield zero events (the route must still
 * 200 so Meta does not retry forever).
 */
export function normalizeWhatsAppWebhook(
	payload: Record<string, unknown>,
): WhatsAppNormalization {
	const events: NormalizedEvent[] = [];

	const entries = Array.isArray(payload.entry) ? payload.entry : [];
	for (const entry of entries as Array<Record<string, unknown>>) {
		const changes = Array.isArray(entry?.changes) ? entry.changes : [];
		for (const change of changes as Array<Record<string, unknown>>) {
			if (change?.field !== "messages") continue;
			const value = (change.value as Record<string, unknown>) || {};
			const metadata = (value.metadata as Record<string, unknown>) || {};
			const pnid = (metadata.phone_number_id as string) || "";
			if (!pnid) continue;

			// Delivery receipts (value.statuses) intentionally produce nothing.
			const messages = Array.isArray(value.messages) ? value.messages : [];
			const contacts = Array.isArray(value.contacts) ? value.contacts : [];
			for (const msg of messages as Array<Record<string, unknown>>) {
				// Runtime type check, not a cast: a numeric `from` would throw
				// on .includes and turn the webhook into a retried 5xx.
				const waId = typeof msg.from === "string" ? msg.from : "";
				if (!waId || waId.includes(":")) continue;
				const text = messageText(msg).slice(0, 4000);
				const contact = (contacts as Array<Record<string, unknown>>).find(
					(c) => (c?.wa_id as string) === waId,
				);
				const profileName =
					((contact?.profile as Record<string, unknown>)?.name as string) || "";

				let conversation: string;
				try {
					conversation = buildConversation({
						source: "whatsapp", scope: pnid, chatType: "dm", chatId: waId,
					});
				} catch {
					continue; // an id carrying ":" cannot be addressed - drop, never 500
				}

				const fields: Record<string, string | number | boolean> = {
					user_id: waId,
					phone_number_id: pnid,
					message_type: (msg.type as string) || "unknown",
				};
				if (msg.id) fields.message_id = String(msg.id);
				if (profileName) fields.profile_name = profileName;
				// Meta stamps each message with unix seconds. Carry it so the
				// 24h-window bookkeeping records the message's own time, not
				// our arrival time - redeliveries can arrive days late.
				const tsSec = Number(msg.timestamp);
				if (Number.isFinite(tsSec) && tsSec > 0) {
					fields.message_timestamp = new Date(tsSec * 1000).toISOString();
				}

				events.push({
					v: 2,
					// Meta retries deliveries with the same message id; using it as
					// the event id gives downstream consumers a stable dedup key.
					id: (msg.id as string) || crypto.randomUUID(),
					source: "whatsapp",
					type: "whatsapp.message",
					topics: [`whatsapp:${pnid}`],
					delivery: "chat",
					text,
					conversation,
					fields,
					timestamp: new Date().toISOString(),
					payload: {
						user_id: waId,
						phone_number_id: pnid,
						text,
						message_type: (msg.type as string) || "unknown",
						...(msg.id ? { message_id: String(msg.id) } : {}),
						...(profileName ? { profile_name: profileName } : {}),
					},
				} as NormalizedEvent);
			}
		}
	}
	return { events };
}
