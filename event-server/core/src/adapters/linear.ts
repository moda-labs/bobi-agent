import type { NormalizedEvent } from "../core.js";
// Q117 — these were defined here, module-locally, so github.ts and
// chat-sdk-slack.ts grew a bare-cast style instead of reusing them.
import { asRecord, stringField } from "./payload.js";

export function normalizeLinearWebhook(
	payload: Record<string, unknown>,
	deliveryId = "",
): NormalizedEvent {
	const action = (payload.action as string) || "unknown";
	const dataType = (payload.type as string) || "unknown";
	const data = asRecord(payload.data);
	const issue = asRecord(data?.issue);
	const teamKey =
		stringField(asRecord(data?.team), "key") || stringField(asRecord(issue?.team), "key");
	const identifier = stringField(data, "identifier") || stringField(issue, "identifier") || "";
	const title = stringField(data, "title") || stringField(issue, "title") || "";
	const state = asRecord(data?.state) || asRecord(issue?.state);
	const stateName = stringField(state, "name");
	const url = stringField(data, "url") || stringField(issue, "url");

	const topics: string[] = [];
	if (teamKey) topics.push(`linear:${teamKey}`);

	const fields: Record<string, string | number | boolean> = { action };
	if (data) {
		if (identifier) fields.identifier = identifier;
		if (title) fields.title = title;
		if (stateName) fields.state = stateName;
		if (url) fields.url = url;
	}

	const text = `[Linear] ${action} ${dataType} ${identifier} ${title}`.trim();

	return {
		v: 2,
		id: deliveryId || crypto.randomUUID(),
		source: "linear",
		type: `linear.${dataType}.${action}`,
		topics,
		delivery: "bulk",
		text,
		fields,
		timestamp: new Date().toISOString(),
		payload,
	};
}
