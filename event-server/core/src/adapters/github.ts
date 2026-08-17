import type { NormalizedEvent } from "../core.js";
// Q117 — untrusted provider JSON is NARROWED at runtime here, matching
// linear.ts/whatsapp.ts/discord.ts, rather than asserted with bare `as` casts
// that a malformed payload would propagate straight into fields.
import { asRecord, numberField, stringField } from "./payload.js";

export function normalizeGitHubWebhook(
	eventHeader: string,
	deliveryId: string,
	payload: Record<string, unknown>,
): NormalizedEvent | null {
	const repo = asRecord(payload.repository);
	const repoFullName = stringField(repo, "full_name");

	if (!repoFullName) return null;

	const action = stringField(payload, "action");

	// Extract the primary object (issue, PR, etc.)
	const issue = asRecord(payload.issue);
	const pr = asRecord(payload.pull_request);
	const obj = issue || pr;

	// Build fields — structurally guarantees key data is always present,
	// making the issues.assigned miss (Finding 4) impossible.
	const fields: Record<string, string | number | boolean> = {};
	if (action) fields.action = action;
	const senderLogin = stringField(asRecord(payload.sender), "login");
	if (senderLogin) fields.sender = senderLogin;
	if (obj) {
		const num = numberField(obj, "number");
		const title = stringField(obj, "title");
		const state = stringField(obj, "state");
		const url = stringField(obj, "html_url");
		if (num !== undefined) fields.number = num;
		if (title) fields.title = title;
		if (state) fields.state = state;
		if (url) fields.url = url;

		// Assignee(s) — always extracted when present. A malformed entry is
		// dropped rather than joined in as "undefined".
		const assignee = asRecord(obj.assignee);
		const assignees = Array.isArray(obj.assignees) ? obj.assignees : undefined;
		const assigneeLogins = assignees
			?.map((a) => stringField(asRecord(a), "login"))
			.filter((login): login is string => Boolean(login)) ?? [];
		if (assigneeLogins.length) {
			fields.assignees = assigneeLogins.join(", ");
		} else if (stringField(assignee, "login")) {
			fields.assignee = stringField(assignee, "login")!;
		}
	}

	// PR-specific fields — merged status and head branch for cleanup dispatch
	if (pr) {
		const headRef = stringField(asRecord(pr.head), "ref");
		if (headRef) fields.head_branch = headRef;
		if (typeof pr.merged === "boolean") fields.merged = pr.merged;
	}

	// Review-specific fields — pull_request_review and pull_request_review_comment
	// carry a review/comment object with state, body, and file context that the
	// consuming agent needs to decide whether to dispatch pr-feedback. The review
	// id is a stable per-review identifier used to dedup dispatch (issue #411).
	const review = asRecord(payload.review);
	if (review) {
		const reviewState = stringField(review, "state");
		const reviewBody = stringField(review, "body");
		const reviewUrl = stringField(review, "html_url");
		const reviewId = numberField(review, "id");
		if (reviewState) fields.review_state = reviewState;
		if (reviewBody) fields.review_body = reviewBody.slice(0, 500);
		if (reviewUrl) fields.review_url = reviewUrl;
		if (reviewId !== undefined) fields.review_id = reviewId;
	}
	const comment = asRecord(payload.comment);
	const isCommentEvent = eventHeader === "pull_request_review_comment"
		|| eventHeader === "issue_comment";
	if (comment && isCommentEvent) {
		const commentBody = stringField(comment, "body");
		const commentUrl = stringField(comment, "html_url");
		if (commentBody) fields.comment_body = commentBody.slice(0, 500);
		if (commentUrl) fields.comment_url = commentUrl;
		// Stable per-comment id — dedups dispatch so one comment can't fan out
		// into multiple feedback engines (issue #411). Present on issue_comment
		// and pull_request_review_comment payloads.
		const commentId = numberField(comment, "id");
		if (commentId !== undefined) fields.comment_id = commentId;
	}
	if (comment && eventHeader === "pull_request_review_comment") {
		const commentPath = stringField(comment, "path");
		if (commentPath) fields.comment_path = commentPath;
	}

	// issue_comment on a PR — GitHub sends issue_comment for both issues and
	// PRs, but includes a pull_request key on the issue when it's a PR.
	if (eventHeader === "issue_comment" && issue && asRecord(issue.pull_request)) {
		fields.is_pull_request = true;
	}

	// Human-readable text summary
	const objLabel = pr ? "PR" : issue ? "issue" : eventHeader;
	const objNumber = obj ? numberField(obj, "number") : undefined;
	const objNum = objNumber ? `#${objNumber}` : "";
	const objTitleText = obj ? stringField(obj, "title") : undefined;
	const objTitle = objTitleText ? ` ${objTitleText}` : "";
	const reviewState = stringField(review, "state");
	const reviewSuffix = reviewState ? ` (${reviewState})` : "";
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
