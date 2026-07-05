# Linear Setup

Get a Linear API key for bobi to scan issues and post comments, plus an
optional webhook signing secret so bobi can verify inbound Linear events.

**Time:** ~2 minutes.

## 1. Create a Personal API Key

1. Go to https://linear.app/settings/api
2. Click **Create key**
3. Name it `bobi`
4. Copy the key (starts with `lin_api_`)

That's it. Personal API keys have access to all teams/projects your account can see.

## 2. Add the key to bobi

Install or reinstall the Bobi Agent that needs Linear. `bobi agents install`
prompts for any missing `${VAR}` credentials and writes them to that named
agent's `run/.env`.

```bash
bobi agents install <source> --name <name>
# Paste the lin_api_ key when prompted for LINEAR_API_KEY
```

For non-interactive installs, provide the value in the environment:

```bash
LINEAR_API_KEY=lin_api_... bobi agents install <source> --name <name> --non-interactive
```

## 3. Configure your project

The project key is used for event routing so workflows can match events
to the correct project.

## Finding your project key

The project key is the prefix on your issue IDs. If your issues look like `ENG-42`, `ENG-108`, then your project key is `ENG`.

You can also find it in Linear: **Settings** → **Teams** → your team → the **Identifier** field.

## 4. Optional: verify Linear webhooks

If this agent receives Linear events through the event server, create a webhook
in **Settings** → **API** → **Webhooks**, point it at
`<event-server-url>/webhooks/linear`, and copy the webhook's signing secret.
Store it as `LINEAR_WEBHOOK_SECRET` alongside `LINEAR_API_KEY`.

For non-interactive installs:

```bash
LINEAR_API_KEY=lin_api_... LINEAR_WEBHOOK_SECRET=... \
  bobi agents install <source> --name <name> --non-interactive
```

## 5. Label issues for automation

Create a label in Linear called `agent` (or whatever you set in `trigger_labels`). When you want dispatch to pick up an issue, add that label.

Dispatch only picks up issues in `Triage` or `Unstarted` states. Once it starts working, it moves the issue to `In Progress`.

## Multiple Linear teams

If you work across different Linear organizations, create separate API keys and
install separate named Bobi Agents with different `run/.env` values.

```bash
BOBI_HOME=~/.bobi-work LINEAR_API_KEY=lin_api_work_org_key \
  bobi agents install <source> --name work --non-interactive
BOBI_HOME=~/.bobi-personal LINEAR_API_KEY=lin_api_personal_org_key \
  bobi agents install <source> --name personal --non-interactive
```

Then reference the appropriate workspace when configuring your projects.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No issues found | Check `trigger_labels` matches a real label in Linear |
| Wrong project | Verify the project key matches your issue ID prefix |
| `401 Unauthorized` | API key expired or revoked — regenerate at linear.app/settings/api |
| Inbound webhook returns `401` | `LINEAR_WEBHOOK_SECRET` does not match the webhook signing secret |
| Issues not picked up | They must be in `Triage` or `Unstarted` state, not `In Progress` or `Done` |
