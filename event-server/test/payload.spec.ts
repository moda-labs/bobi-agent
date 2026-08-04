import { describe, it, expect } from "vitest";
import {
	asRecord,
	stringField,
	numberField,
	normalizeAttachments,
	SLACK_FILE_KEYS,
	DISCORD_ATTACHMENT_KEYS,
} from "@moda-labs/bobi-events-core/adapters/payload";
import { normalizeGitHubWebhook } from "@moda-labs/bobi-events-core/adapters/github";

// Q117: the house policy for untrusted provider JSON is to narrow at runtime
// and drop what does not fit — not to assert a shape with a bare cast.
describe("narrowing helpers", () => {
	it("asRecord accepts objects and rejects everything else", () => {
		expect(asRecord({ a: 1 })).toEqual({ a: 1 });
		expect(asRecord([1, 2])).toEqual([1, 2]); // arrays are objects
		expect(asRecord(null)).toBeUndefined();
		expect(asRecord(undefined)).toBeUndefined();
		expect(asRecord("str")).toBeUndefined();
		expect(asRecord(42)).toBeUndefined();
	});

	it("stringField returns only actual strings", () => {
		const rec = { s: "yes", n: 42, nil: null, nested: { a: 1 } };
		expect(stringField(rec, "s")).toBe("yes");
		expect(stringField(rec, "n")).toBeUndefined();
		expect(stringField(rec, "nil")).toBeUndefined();
		expect(stringField(rec, "nested")).toBeUndefined();
		expect(stringField(rec, "missing")).toBeUndefined();
		expect(stringField(undefined, "s")).toBeUndefined();
	});

	it("numberField returns only actual numbers", () => {
		const rec = { n: 42, zero: 0, s: "42" };
		expect(numberField(rec, "n")).toBe(42);
		expect(numberField(rec, "zero")).toBe(0);
		expect(numberField(rec, "s")).toBeUndefined();
		expect(numberField(undefined, "n")).toBeUndefined();
	});
});

// D091: one extraction loop behind both adapters' key maps.
describe("normalizeAttachments", () => {
	it("maps Slack file keys and stringifies every value", () => {
		const files = normalizeAttachments([{
			id: "F1", name: "shot.png", mimetype: "image/png", filetype: "png",
			url_private: "https://x/p", url_private_download: "https://x/d", size: 12345,
		}], SLACK_FILE_KEYS);
		expect(files).toEqual([{
			id: "F1", name: "shot.png", mimetype: "image/png", filetype: "png",
			url_private: "https://x/p", url_private_download: "https://x/d", size: "12345",
		}]);
	});

	it("maps Discord attachment keys onto the same agent-facing names", () => {
		const files = normalizeAttachments([{
			id: "A1", filename: "log.txt", content_type: "text/plain",
			url: "https://cdn/log.txt", size: 99,
		}], DISCORD_ATTACHMENT_KEYS);
		// filename -> name and content_type -> mimetype: the two providers
		// disagree on spelling, agents see one contract.
		expect(files).toEqual([{
			id: "A1", name: "log.txt", mimetype: "text/plain",
			url: "https://cdn/log.txt", size: "99",
		}]);
	});

	it("omits absent keys so consumers can test presence", () => {
		expect(normalizeAttachments([{ id: "F1" }], SLACK_FILE_KEYS)).toEqual([{ id: "F1" }]);
	});

	it("yields no files for a non-array, and skips a non-object entry", () => {
		expect(normalizeAttachments(undefined, SLACK_FILE_KEYS)).toEqual([]);
		expect(normalizeAttachments("nope", SLACK_FILE_KEYS)).toEqual([]);
		expect(normalizeAttachments({ id: "F1" }, SLACK_FILE_KEYS)).toEqual([]);
		// A null entry used to throw on the first property access.
		expect(normalizeAttachments([null, { id: "F1" }], SLACK_FILE_KEYS))
			.toEqual([{ id: "F1" }]);
	});
});

// Q117 in effect: the bare casts github.ts used would have propagated these
// malformed values into fields as "undefined" or a wrong-typed value.
describe("normalizeGitHubWebhook narrowing", () => {
	function payload(extra: Record<string, unknown>) {
		return { repository: { full_name: "org/repo" }, action: "opened", ...extra };
	}

	it("drops an assignee entry whose login is not a string", () => {
		const event = normalizeGitHubWebhook("issues", "d1", payload({
			issue: { number: 1, assignees: [{ login: "alice" }, { login: 42 }, null] },
		}))!;
		expect(event.fields!.assignees).toBe("alice");
	});

	it("omits assignees entirely when none of them narrow to a string", () => {
		const event = normalizeGitHubWebhook("issues", "d1", payload({
			issue: { number: 1, assignees: [{ login: 42 }] },
		}))!;
		expect(event.fields!.assignees).toBeUndefined();
	});

	it("ignores a non-string title rather than emitting it", () => {
		const event = normalizeGitHubWebhook("issues", "d1", payload({
			issue: { number: 1, title: { rendered: "nope" } },
		}))!;
		expect(event.fields!.title).toBeUndefined();
		expect(event.text).toBe("[org/repo] opened issue #1");
	});

	it("ignores a non-number issue number rather than emitting it", () => {
		const event = normalizeGitHubWebhook("issues", "d1", payload({
			issue: { number: "12", title: "t" },
		}))!;
		expect(event.fields!.number).toBeUndefined();
	});

	it("still extracts a well-formed payload unchanged", () => {
		const event = normalizeGitHubWebhook("pull_request", "d2", payload({
			action: "closed",
			pull_request: {
				number: 7, title: "Fix it", state: "closed",
				html_url: "https://gh/pr/7", merged: true, head: { ref: "topic" },
			},
		}))!;
		expect(event.fields).toMatchObject({
			action: "closed", number: 7, title: "Fix it", state: "closed",
			url: "https://gh/pr/7", merged: true, head_branch: "topic",
		});
		expect(event.text).toBe("[org/repo] closed PR #7 Fix it");
	});
});
