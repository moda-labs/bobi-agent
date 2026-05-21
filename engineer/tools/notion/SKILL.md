# Knowledge Base — Notion

This documents how to interact with our knowledge base. Currently Notion.

**Note:** This integration is not yet implemented. When it is, it will
provide the following capabilities.

## What lives in the knowledge base

- Product requirements documents (PRDs)
- Architecture decision records (ADRs)
- Runbooks and onboarding guides
- API documentation
- Team processes and policies

## How the engineer uses it

- Read PRDs and ADRs when working on a task for deeper context
- Check if a similar feature or pattern was documented before
- Reference runbooks when debugging production issues

## How the manager uses it

- Look up team processes when deciding how to handle a situation
- Reference PRDs when prioritizing or scoping work
- Check ADRs when answering engineer questions about architecture choices

## Configuration

When implemented, configure in `.modastack.yaml`:

```yaml
knowledge_base:
  provider: notion           # or confluence, gitbook
  workspace: "your-workspace"
  # Provider-specific config
```
