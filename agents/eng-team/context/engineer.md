# Engineer Context

Use this context when launching or reviewing async engineer worker tasks.

- Workers own executable work: issue pickup, specs, implementation, PR prep, QA,
  feedback handling, merge-conflict resolution, build-failure fixes, cleanup,
  bounded investigations, and handoff writing.
- The director must provide the source event type, repo, workflow, original
  artifact reference, requester attribution, relevant URLs, and bounded excerpts
  needed to act.
- Workers should not rely on director memory. Source artifacts and handoffs are
  the durable operational record for a workflow run.
