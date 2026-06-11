import type { NormalizedEvent } from "../core";

export function normalizeGitHubWebhook(
	eventHeader: string,
	deliveryId: string,
	payload: Record<string, unknown>,
): NormalizedEvent | null {
	const repo = payload.repository as Record<string, unknown> | undefined;
	const repoFullName = repo?.full_name as string | undefined;

	if (!repoFullName) return null;

	const action = payload.action as string | undefined;

	// Extract the primary object (issue, PR, etc.)
	const issue = payload.issue as Record<string, unknown> | undefined;
	const pr = payload.pull_request as Record<string, unknown> | undefined;
	const obj = issue || pr;

	// Build fields — structurally guarantees key data is always present,
	// making the issues.assigned miss (Finding 4) impossible.
	const fields: Record<string, string | number | boolean> = {};
	if (action) fields.action = action;
	if (payload.sender) {
		const sender = payload.sender as Record<string, unknown>;
		if (sender.login) fields.sender = sender.login as string;
	}
	if (obj) {
		const num = obj.number as number | undefined;
		const title = obj.title as string | undefined;
		const state = obj.state as string | undefined;
		const url = obj.html_url as string | undefined;
		if (num !== undefined) fields.number = num;
		if (title) fields.title = title;
		if (state) fields.state = state;
		if (url) fields.url = url;

		// Assignee(s) — always extracted when present
		const assignee = obj.assignee as Record<string, unknown> | undefined;
		const assignees = obj.assignees as Array<Record<string, unknown>> | undefined;
		if (assignees?.length) {
			fields.assignees = assignees.map((a) => a.login as string).join(", ");
		} else if (assignee?.login) {
			fields.assignee = assignee.login as string;
		}
	}

	// Human-readable text summary
	const objLabel = pr ? "PR" : issue ? "issue" : eventHeader;
	const objNum = obj?.number ? `#${obj.number}` : "";
	const objTitle = obj?.title ? ` ${obj.title}` : "";
	const text = `[${repoFullName}] ${action || eventHeader} ${objLabel} ${objNum}${objTitle}`.trim();

	return {
		v: 2,
		id: deliveryId || crypto.randomUUID(),
		source: "github",
		type: `github.${eventHeader}`,
		topics: [`github:${repoFullName}`],
		delivery: "bulk",
		text,
		fields,
		timestamp: new Date().toISOString(),
		payload,
	};
}
