import type { NormalizedEvent } from "../core";

function asRecord(value: unknown): Record<string, unknown> | undefined {
	return value && typeof value === "object" ? value as Record<string, unknown> : undefined;
}

function stringField(record: Record<string, unknown> | undefined, key: string): string | undefined {
	const value = record?.[key];
	return typeof value === "string" ? value : undefined;
}

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
