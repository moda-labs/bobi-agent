# Skill Packs

agent-dispatch auto-discovers installed skills and incorporates them into agent prompts. No configuration needed — if skills are installed, they're used.

## How discovery works

On each dispatch cycle, the engine scans these locations for skill packs:

```
~/.claude/skills/*/
~/.codex/skills/*/
~/.cursor/skills/*/
```

A directory is a skill pack if it contains either:
1. A `skill-pack.yaml` manifest (explicit)
2. Subdirectories with `SKILL.md` files (convention-based, e.g., gstack)

## Built-in relevance mapping

Dispatch matches skills to tasks based on Linear labels:

| Label contains | Skills suggested |
|---|---|
| `bug` | investigate, review |
| `feature` | office-hours, plan-eng-review, ship, review |
| `refactor` | review, ship |
| `security` | cso |
| `docs` | document-release |
| `performance` | benchmark |
| `design` | design-review, plan-design-review |
| `qa` | qa, browse |
| `deploy` | land-and-deploy, canary |

No matching labels → defaults to `review` + `ship`.

## Creating your own skill pack

Create a directory with a `skill-pack.yaml`:

```yaml
# ~/.claude/skills/my-team-skills/skill-pack.yaml
name: "my-team-skills"
skills:
  - name: "db-migrate"
    description: "Run and verify database migrations safely"
    trigger: "/db-migrate"
  - name: "api-test"
    description: "Generate API integration tests from OpenAPI spec"
    trigger: "/api-test"
  - name: "changelog"
    description: "Generate changelog from commits since last tag"
    trigger: "/changelog"
```

Or just follow the gstack convention: one subdirectory per skill, each with a `SKILL.md`:

```
~/.claude/skills/my-team-skills/
├── db-migrate/
│   └── SKILL.md
├── api-test/
│   └── SKILL.md
└── changelog/
    └── SKILL.md
```

## How skills get injected into prompts

For **trivial** tasks: no skills injected (too simple to need them).

For **medium** tasks: relevant skills listed as available, agent decides whether to use them.

For **heavy** tasks: relevant skills listed with explicit guidance to use them when appropriate.

## Adding relevance rules

Edit `dispatch/skills.py` → `get_relevant_skills()` to add mappings from your labels to your skill names. Or override per-repo in `.dispatch.yaml`:

```yaml
agent:
  skills: ["db-migrate", "api-test"]  # always available for this repo
```

Explicit skills from `.dispatch.yaml` are a fallback — if auto-discovery finds matching skills, those take priority (they have descriptions and richer context).

## Examples

### gstack installed (auto-detected)

```
~/.claude/skills/gstack/
├── review/SKILL.md
├── ship/SKILL.md
├── investigate/SKILL.md
├── qa/SKILL.md
└── ...
```

Dispatch sees gstack, discovers 20+ skills, and injects the relevant ones per-task.

### Custom team skills + gstack

```
~/.claude/skills/gstack/          # auto-detected
~/.claude/skills/acme-tools/      # your team's custom skills
├── skill-pack.yaml
├── deploy-canary/SKILL.md
└── run-e2e/SKILL.md
```

Both packs discovered. A task labeled `deploy` gets `land-and-deploy` (gstack) + `deploy-canary` (acme-tools).
