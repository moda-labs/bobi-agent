import type { NormalizedEvent } from "../core";

export function normalizeLinearWebhook(
	payload: Record<string, unknown>,
	deliveryId = "",
): NormalizedEvent {
	const action = (payload.action as string) || "unknown";
	const dataType = (payload.type as string) || "unknown";
	const data = payload.data as Record<string, unknown> | undefined;
	const teamKey =
		(data?.team as Record<string, unknown> | undefined)?.key as string | undefined;

	const topics: string[] = [];
	if (teamKey) topics.push(`linear:${teamKey}`);

	const fields: Record<string, string | number | boolean> = { action };
	if (data) {
		const identifier = data.identifier as string | undefined;
		const title = data.title as string | undefined;
		const state = data.state as Record<string, unknown> | undefined;
		const url = data.url as string | undefined;
		if (identifier) fields.identifier = identifier;
		if (title) fields.title = title;
		if (state?.name) fields.state = state.name as string;
		if (url) fields.url = url;
	}

	const identifier = data?.identifier || "";
	const title = data?.title || "";
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
