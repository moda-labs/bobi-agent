# Linear Setup

Get a Linear API key for bobi to scan issues and post comments.

**Time:** ~2 minutes.

## 1. Create a Personal API Key

1. Go to https://linear.app/settings/api
2. Click **Create key**
3. Name it `bobi`
4. Copy the key (starts with `lin_api_`)

That's it. Personal API keys have access to all teams/projects your account can see.

## 2. Add the key to bobi

```bash
bobi init
# Paste the lin_api_ key when prompted for Linear API key
```

Or for named credentials:

```yaml
# ~/.config/bobi/credentials.yaml
default:
  linear_api_key: "lin_api_..."
```

## 3. Configure your project

The project key is used for event routing so workflows can match events
to the correct project.

## Finding your project key

The project key is the prefix on your issue IDs. If your issues look like `ENG-42`, `ENG-108`, then your project key is `ENG`.

You can also find it in Linear: **Settings** → **Teams** → your team → the **Identifier** field.

## 4. Label issues for automation

Create a label in Linear called `agent` (or whatever you set in `trigger_labels`). When you want dispatch to pick up an issue, add that label.

Dispatch only picks up issues in `Triage` or `Unstarted` states. Once it starts working, it moves the issue to `In Progress`.

## Multiple Linear teams

If you work across different Linear organizations, create separate API keys for each and store them as named credentials:

```yaml
# ~/.config/bobi/credentials.yaml
work:
  linear_api_key: "lin_api_work_org_key"

personal:
  linear_api_key: "lin_api_personal_org_key"
```

Then reference the appropriate workspace when configuring your projects.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No issues found | Check `trigger_labels` matches a real label in Linear |
| Wrong project | Verify the project key matches your issue ID prefix |
| `401 Unauthorized` | API key expired or revoked — regenerate at linear.app/settings/api |
| Issues not picked up | They must be in `Triage` or `Unstarted` state, not `In Progress` or `Done` |
