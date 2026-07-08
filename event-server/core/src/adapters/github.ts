import type { NormalizedEvent } from "../core.js";

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

	// PR-specific fields — merged status and head branch for cleanup dispatch
	if (pr) {
		const head = pr.head as Record<string, unknown> | undefined;
		if (head?.ref) fields.head_branch = head.ref as string;
		if (typeof pr.merged === "boolean") fields.merged = pr.merged;
	}

	// Review-specific fields — pull_request_review and pull_request_review_comment
	// carry a review/comment object with state, body, and file context that the
	// consuming agent needs to decide whether to dispatch pr-feedback. The review
	// id is a stable per-review identifier used to dedup dispatch (issue #411).
	const review = payload.review as Record<string, unknown> | undefined;
	if (review) {
		if (review.state) fields.review_state = review.state as string;
		if (review.body) fields.review_body = (review.body as string).slice(0, 500);
		if (review.html_url) fields.review_url = review.html_url as string;
		if (review.id !== undefined) fields.review_id = review.id as number;
	}
	const comment = payload.comment as Record<string, unknown> | undefined;
	if (comment && (eventHeader === "pull_request_review_comment" ||
		eventHeader === "issue_comment")) {
		if (comment.body) fields.comment_body = (comment.body as string).slice(0, 500);
		if (comment.html_url) fields.comment_url = comment.html_url as string;
	}
	if (comment && eventHeader === "pull_request_review_comment") {
		if (comment.path) fields.comment_path = comment.path as string;
	}
	// Stable per-comment id — dedups dispatch so one comment can't fan out into
	// multiple feedback engines (issue #411). Present on issue_comment and
	// pull_request_review_comment payloads.
	if (comment && (eventHeader === "pull_request_review_comment" ||
		eventHeader === "issue_comment")) {
		if (comment.id !== undefined) fields.comment_id = comment.id as number;
	}

	// issue_comment on a PR — GitHub sends issue_comment for both issues and
	// PRs, but includes a pull_request key on the issue when it's a PR.
	if (eventHeader === "issue_comment" && issue) {
		const prRef = issue.pull_request as Record<string, unknown> | undefined;
		if (prRef) {
			fields.is_pull_request = true;
		}
	}

	// Human-readable text summary
	const objLabel = pr ? "PR" : issue ? "issue" : eventHeader;
	const objNum = obj?.number ? `#${obj.number}` : "";
	const objTitle = obj?.title ? ` ${obj.title}` : "";
	const reviewSuffix = review?.state ? ` (${review.state})` : "";
	const text = `[${repoFullName}] ${action || eventHeader} ${objLabel} ${objNum}${objTitle}${reviewSuffix}`.trim();

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
