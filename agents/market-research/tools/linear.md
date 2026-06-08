# Linear

Receive research requests from Linear and report results back, via the
`modastack` CLI.

## Get issue details

```bash
modastack linear issue <issue-id>
```

Returns title, description, state, assignee, labels, and linked items.
This is how you read a research-request ticket into a brief.

## Comment results back on an issue

```bash
modastack linear comment <issue-id> "message"
```

Post the research summary and a link to the full brief (or paste it) when
a `linear-research` run completes.

## Update issue state

```bash
modastack linear move <issue-id> <state>
```

States: `Todo`, `In Progress`, `In Review`, `Done`, `Blocked`.
Move a research ticket to `In Progress` when you pick it up and to `Done`
(or `In Review`) when you've commented the results.

## List issues

```bash
modastack linear issues --state "In Progress"
modastack linear issues --assignee "@me"
```

## What counts as a research request

A ticket assigned to this agent, or labeled for research (e.g. `research`),
whose body asks a market/landscape/PMF question. Parse the body into a
research brief, run the appropriate workflow, then comment the result and
move the ticket.
