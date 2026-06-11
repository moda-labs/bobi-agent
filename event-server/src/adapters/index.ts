/**
 * Adapter registry — webhook normalizers indexed by source name.
 *
 * Each adapter converts a raw webhook payload into a v2 NormalizedEvent.
 * Webhook routes resolve their adapter by name instead of hard-coding
 * normalizer calls inline.
 */

export { normalizeGitHubWebhook } from "./github";
export { normalizeLinearWebhook } from "./linear";
export { normalizeSlackWebhook } from "./slack";
