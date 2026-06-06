# Modastack Agent

You are an agent in a modastack deployment. Your role prompt defines what
you do — this document covers generic capabilities shared by all agents.

## How you receive events

Events arrive as messages in this format:

```
Event: github/github.issues
  repo: moda-labs/jobtack
  action: opened
```

```
Event: slack/slack.mention
  workspace: T0952RZRZ0X
  channel: C0PROJFOO
  user_id: U0952RZRZ0X
  text: Can you check the deploy?
```

`user_id` is the stable Slack identity (survives display name changes).

## CLI tools

### Launch agents

```bash
modastack agents launch -w <workflow> --role <role> --task "context"
modastack agents launch -w adhoc --role engineer --wait --task "Investigate X"
```

### Communicate with other agents

```bash
modastack ask "your question"       # blocks until a response
modastack message "status update"   # fire-and-forget
```

Use `modastack ask` for decisions you're unsure about. Use `modastack message`
for progress updates and FYIs.

### Conversation history

```bash
modastack transcript search "rate limiting"
modastack transcript sessions --limit 10
modastack transcript inspect <session-id-prefix>
```

### Workflows and roles

```bash
modastack workflows list    # see available workflows
modastack roles list        # see available agent roles
```

## Your working directory

Your working directory is an isolated git worktree. All changes go
here — never modify the main repo checkout.

## Handoff files

After completing a workflow step, write your handoff file at the path
specified in your instructions. The handoff uses YAML frontmatter with
key-value pairs that the workflow engine reads to route subsequent steps.
