# Deployment interface — `modastack deploy` (design)

Status: **implemented (2026-06-19), PR #365.** The deployment *engine* is now the
portable CLI primitive `modastack deploy` / `destroy` (`modastack/deploy.py`); the
GitHub Actions are thin clients. Both delivery modes (ssh-push for a local
`team:`, HTTPS-fetch for a published `team-url:`) ship. See `docs/DEPLOYMENT.md`
§2.5 for how it works today. Composes with C8 (image), C22 (the Fly mechanics),
and C24 (team-flavored images). (Original design preserved below.)

## Context / why

C22 made GitOps work, but the reconcile logic lives **inside the workflow YAML**, so
the fleet-level automation only works in moda's repo with moda's Action. That
violates the operator-agnostic principle (design §9.1) one level up: a third-party
developer or enterprise can't self-service-deploy without our Actions, and a future
SaaS control plane can't drive it either.

The fix is a layering, applied recursively:

```
Layer 3 — orchestration (per operator):  GitHub Action │ Terraform │ SaaS control plane │ a for-loop
                                             └─ loops / diffs / decides, calls ↓
Layer 2 — the primitive (in modastack):  modastack deploy <name> / destroy <name>   ← ONE instance
                                             └─ uses ↓
Layer 1 — mechanics:                      fly apps create/deploy · fleet.sh · install · ssh-push
```

**The primitive deploys/destroys ONE instance, idempotently. Anything that loops or
diffs across instances is orchestration and lives on top** — never a `-f fleet.yaml`
mode baked into the command. moda drives it from an Action; an enterprise from
Terraform; a SaaS plane calls `deploy <tenant>` on signup. Same engine, many triggers.

## Two directories (the decoupling)

| `agents/<team>/` — **packages** | `deployments/<name>.yaml` — **deployments** |
|---|---|
| what the team **is and needs** (portable, distributable) | **where/how an operator runs an instance** |
| `services:`, `requires:`, `build:` (C24), `${VAR}` refs = *which* secrets | name, team source, region, size, auth, event server, fleet, *where* secrets come from |

Deploy config **cannot** live in the package: one team → many deployments
(`acme-eng`, `beta-eng`, `staging-eng` all use `eng-team`), and the package must not
carry an operator's app name / region / secrets. Adding to `agents/` deploys
nothing; adding a `deployments/*.yaml` deploys. That *is* the
"decouple-package-appearance-from-deployment" requirement, realized as two dirs.

## The primitives

```
modastack deploy <name>      # provision/update ONE instance (idempotent)
modastack destroy <name>     # tear down ONE instance — Fly app + volume (typed-confirm)
```

### `modastack deploy <name>` semantics
1. **Resolve config** (precedence): CLI flags › `deployments/<name>.yaml` ›
   `deployments/defaults.yaml` › built-in defaults. The primitive merges these
   itself, so it works standalone (no orchestration pre-merge). A bare `<name>` with
   no file = local package `agents/<name>` + defaults + ssh-push.
2. **Resolve the team + delivery** (the two "both" primitives):
   - `team: <name>` → a **local** package (`_resolve_agent_pack`) → **ssh-push**
     delivery (build the tarball, provision a blank instance, push the team in over
     `fly ssh`, start). The "I just built it, ship it" dev path — no hosting.
   - `team-url: <url>` → a **published** tarball → **HTTPS-fetch** at first boot
     (today's dark-instance path). The enterprise/SaaS path. Exactly one of the two.
3. **Resolve secrets**: `secrets.env` (a named source, e.g. a GitHub Environment) or
   `secrets.env-file` (a local path), or flags/process env. Validate the package's
   required vars (`find_required_env_vars`) against the source; **fail loudly** on a
   gap ("missing OPENAI_API_KEY") rather than boot a broken instance.
4. **Compute identity**: app = `<fleet>-<name>`; stamp `MODASTACK_FLEET` +
   `MODASTACK_INSTANCE=<name>` in `[env]` (enumerable; the SaaS tenant key).
5. **Apply**: the current provisioner core (app/volume/secrets/config/deploy),
   idempotent — create or redeploy. If the team has a C24 image, deploy that image;
   else the generic base.

### `modastack destroy <name>` semantics
Resolve `<name>` → `<fleet>-<name>` → `destroy-instance.sh` (Fly app + volume).
Keep the **typed-confirmation** safety (volume = only copy of state); `--yes` for
automation. The orchestration teardown-detector only *surfaces* removed instances
and calls this — never silent.

## `deployments/<name>.yaml`

Name = the filename (`deployments/acme-eng.yaml` → `acme-eng`), not a field.

Minimal (dev):
```yaml
# deployments/my-team.yaml  — or no file + `modastack deploy my-team`
team: my-team        # local package → ssh-push; everything else defaults
```

Full (enterprise/SaaS):
```yaml
# deployments/acme-eng.yaml
team-url: https://registry.acme.com/eng-team-1.2.0.tar.gz   # registry → HTTPS-fetch
fleet: acme                       # → MODASTACK_FLEET + app prefix
event_server: https://ev.acme.workers.dev
region: sjc
memory: 8gb
cpus: 2
auth: subscription
secrets:
  env: acme-eng                   # named secret source (GitHub Environment, …)
  # env-file: ./acme-eng.env      # …or a local file (self-service)
```

`deployments/defaults.yaml` carries shared operator **values** (fleet, event_server,
region) to avoid repetition — it is **not** a list of what to deploy (the list is the
set of `deployments/*.yaml` files). This keeps the "no fleet-file the command parses"
slice intact while staying DRY.

## Orchestration (on top — not in the primitive)

The reconcile niceties are composition:
- "only act on changed `deployments/*.yaml`" — diff in the Action.
- "Fly app exists but no `deployments/` file" → surface for **human-gated**
  `modastack destroy <name>` (never auto-destroy).
- the loop itself — `for name in changed; do modastack deploy "$name"; done`.

A thin `modastack deploy --all` (loop + diff over `deployments/`) is **deferred** —
build the primitive first; the Action doesn't need it.

### GitHub Actions become thin clients
```yaml
# gitops-teams.yml, essentially:
- run: |
    for name in $(changed deployments/*.yaml); do
      materialize secrets for "$name"        # GitHub Environment → env-file
      modastack deploy "$name"
    done
    # report Fly apps (fleet.sh) with no deployments/ file → human destroy
```
`gitops-release.yml` similarly loops `modastack deploy` (image rebuild) over the
fleet. Nothing GitHub-specific remains in the engine.

## What's reused vs new

**Reused:** `provision-instance.sh` / `destroy-instance.sh` core (absorbed or called
by the CLI), `scripts/fleet.sh` (enumeration), `_resolve_agent_pack` (name → package),
`find_required_env_vars` (secret validation), `registry.fetch_from_url` / `install`
(delivery), the `MODASTACK_*` env-routing, build-team-tarballs.sh.

**New:** `modastack deploy` / `destroy` Click subcommands; `deployments/*.yaml` +
`defaults.yaml` parsing + precedence; the **ssh-push delivery path** (which needs a
small entrypoint change: boot-with-no-team → wait, accept a pushed team, start — a
C9-adjacent change); `MODASTACK_INSTANCE` stamp; thin workflows.

## Relationship to C22 / C24

- **C22 (#365):** this *refactors* it — the workflow business logic moves into the
  CLI; the Fly mechanics and `fleet.sh` stay. Net: the same behavior, but portable.
- **C24 (#368):** composes — `deploy` picks the team's image (generic base, or the
  team-flavored image when a `build:` spec exists). No conflict.

## Open / deferred

- `modastack deploy --all` convenience loop — deferred (#1 decision).
- Registry choice for published tarballs (GHCR vs Fly) — ties to C24.
- Third-party build/deploy trust (sandboxing arbitrary teams) — note for SaaS, not MVP.
- ssh-push entrypoint "wait for team" state — sequence with C9 (#339).
