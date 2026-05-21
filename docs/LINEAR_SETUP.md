# Linear Setup

Get a Linear API key for modastack to scan issues and post comments.

**Time:** ~2 minutes.

## 1. Create a Personal API Key

1. Go to https://linear.app/settings/api
2. Click **Create key**
3. Name it `modastack`
4. Copy the key (starts with `lin_api_`)

That's it. Personal API keys have access to all teams/projects your account can see.

## 2. Add the key to modastack

```bash
modastack init
# Paste the lin_api_ key when prompted for Linear API key
```

Or for named credentials:

```yaml
# ~/.modastack/credentials.yaml
default:
  linear_api_key: "lin_api_..."
```

## 3. Configure your repo

In your repo's `.modastack.yaml`, set the Linear project key:

```yaml
linear:
  project: "PROJ"              # The short key (visible in issue IDs like PROJ-42)
  trigger_labels: ["agent"]    # Issues with this label get dispatched
```

## Finding your project key

The project key is the prefix on your issue IDs. If your issues look like `ENG-42`, `ENG-108`, then your project key is `ENG`.

You can also find it in Linear: **Settings** → **Teams** → your team → the **Identifier** field.

## 4. Label issues for automation

Create a label in Linear called `agent` (or whatever you set in `trigger_labels`). When you want dispatch to pick up an issue, add that label.

Dispatch only picks up issues in `Triage` or `Unstarted` states. Once it starts working, it moves the issue to `In Progress`.

## Multiple Linear teams

If you work across different Linear organizations, create separate API keys for each and store them as named credentials:

```yaml
# ~/.modastack/credentials.yaml
work:
  linear_api_key: "lin_api_work_org_key"

personal:
  linear_api_key: "lin_api_personal_org_key"
```

Each repo references its credential set:

```yaml
# work-repo/.modastack.yaml
credentials: "work"
linear:
  project: "ENG"

# side-project/.modastack.yaml
credentials: "personal"
linear:
  project: "SIDE"
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No issues found | Check `trigger_labels` matches a real label in Linear |
| Wrong project | Verify the project key matches your issue ID prefix |
| `401 Unauthorized` | API key expired or revoked — regenerate at linear.app/settings/api |
| Issues not picked up | They must be in `Triage` or `Unstarted` state, not `In Progress` or `Done` |
