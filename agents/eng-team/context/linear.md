# Linear Context

Use this context when an overlay configures Linear as the authoritative tracker.

- Treat the Linear identifier and UUID as source references. Mutations need the
  UUID.
- Move issues through Todo, In Progress, Blocked, In Review, and Done according
  to the engineer lifecycle.
- Include the Linear issue identifier, UUID when known, team key, repo, requester
  context, and relevant GitHub artifact URLs in worker launches.
- Prefer tracker state and workflow handoffs over conversational memory when
  reporting status.
