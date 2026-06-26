# Open-Core / SaaS Repository & Deployment Strategy

**Status:** Design / decision record
**Audience:** bobi maintainers
**Question it answers:** As bobi moves toward a hosted (SaaS) offering, what
code stays in the open-source repo and what moves into a private repo? Where does
deployment/provisioning code live — and how do other open-core companies draw
that line?

---

## TL;DR

- The split is **not** "deployment code (open) vs. dashboard/UI (closed)."
- The line every successful open-core company actually draws is **single-tenant
  runtime (open) vs. multi-tenant control plane (closed)**.
- Deployment code splits *across* that line: "deploy one instance" is open;
  "provision, bill, and orchestrate many instances" is closed.
- **Recommendation for bobi:** keep all single-instance deploy/provision
  code open (framework, container image, Fly provision script, `install <url>`);
  put the fleet orchestrator, billing/metering, and hosted dashboard in a private
  repo; have the private control plane **call** the open provisioning primitives
  rather than reimplement them; and relicense the core to **BSL/FSL early** so
  licensing — not secrecy — protects the SaaS business.

---

## The mental model: single-tenant runtime vs. multi-tenant control plane

The intuitive split ("deployment = open, the fancy UI = closed") draws the line in
the wrong place. The durable line is:

| Open (data plane / one customer runs it for themselves) | Closed (control plane / you run it for many customers) |
| --- | --- |
| The runtime / framework | Multi-tenant control plane |
| The container image | Billing, metering, usage attribution |
| Single-node deploy (Compose / one Helm chart / `provision-instance.sh`) | Fleet provisioning & orchestration (provision/upgrade/tear-down *many*) |
| Self-host-one-instance docs | Hosted dashboard backend, multi-tenant auth |
| | SSO / RBAC / audit log, SLAs |

"Deploy **one** instance" belongs in the open repo. "Provision and bill **10,000**
instances" belongs in the private repo. They share primitives; they are not the
same code.

---

## The three structural patterns in the wild

### 1. Single repo, `ee/` directory — GitLab, PostHog
All code lives in one repository; proprietary code is quarantined to a top-level
`ee/` folder under a different license. The OSS build is produced by literally
deleting/ignoring that directory — GitLab uses a `FOSS_ONLY` env var; PostHog
gates `ee/` behind a license check. One codebase, two license zones.

- **Pros:** best DX, lowest version drift, one place to change things.
- **Cons:** proprietary code is *visible* (source-available, not hidden).

### 2. Separate repos — Grafana, Mattermost
OSS core is its own repo; enterprise is a separate private repo (a superset/fork
or a plugin layer) that pulls the core in. Deployment artifacts (e.g. Helm charts)
often live in a *third* shared repo that can deploy either edition via values
flags.

- **Pros:** cleaner secrecy boundary.
- **Cons:** more integration/version-drift overhead between core and enterprise.

### 3. No open-core — license the whole thing — Sentry
Sentry ships **zero feature difference** between self-hosted and SaaS. Instead of
hiding code, they license it (FSL/BSL) so you can run it but can't resell it as a
competing SaaS. Their entire SaaS value is "we operate it for you," not "we have
secret features." Increasingly the modern move (HashiCorp → BSL, Elastic →
SSPL/AGPL) when the moat is **operations**, not **features**.

---

## What this means for bobi

bobi is a Python CLI framework on PyPI, single-tenant agent instances, now
containerized onto Fly. Its natural shape is a **control-plane / data-plane
split** — also the most common modern open-source-SaaS architecture
(split-plane).

### Keep in the open-source repo (data plane / single instance)
- The framework itself (already published to PyPI).
- The containerized runtime image (the C8 work).
- **Single-instance provisioning** — `scripts/provision-instance.sh`, the Fly
  machine config, `bobi install <url>`. This is valuable as a credibility
  and self-host signal; hiding it buys nothing because it only provisions *one*
  bubble.
- A reference deploy (Compose / single Fly app) so anyone can self-host one
  instance.

### Keep in a private repo (control plane / your cloud)
- The **fleet orchestrator** — provisions/tears-down/upgrades *many* instances,
  assigns subdomains, manages quotas. A *superset* of the provision script, not a
  copy.
- **Billing, metering, usage attribution.** (`bobi costs` is the local view;
  the aggregated cross-tenant version is the SaaS asset.)
- The **hosted dashboard UI + backend**, multi-tenant auth, SSO/RBAC/audit.
- Anything touching other customers' data or your infra credentials.

### The discipline that keeps it clean
The control plane should **call the same provisioning primitives the OSS repo
exposes, not reimplement them.** The private fleet manager shells out to / imports
the open `provision-instance.sh` logic. This keeps "deploy one" and "deploy many"
from drifting, and means dogfooding the OSS path also exercises the SaaS path.

---

## Recommendation

Adopt **pattern 2 + 3 combined, deferring open-core (pattern 1)**:

1. **Don't build an `ee/` split yet.** There are no proprietary *features* worth
   hiding today — the moat is "we host and operate the agent fleet for you" (the
   Sentry posture).
2. **Put deployment in the open repo; fleet/billing/dashboard in a private repo.**
   Split on control-plane vs. data-plane, per the table above.
3. **Relicense the core from permissive to BSL/FSL early**, before there's SaaS
   revenue to protect, so nobody can spin up "bobi-cloud" from the open
   provisioning scripts. Highest-leverage protective move; cheap early, painful
   late. It also lets *all* deployment code stay fully open without fear.
4. **Revisit a real `ee/` open-core split only once** there are specific
   enterprise features (SSO, audit logs, RBAC) that genuinely differ from the OSS
   runtime.

Net effect: the `bobi install <url>` / Fly provision story stays fully open
(great for trust and self-hosters), while the genuinely SaaS-only assets
(multi-tenant provisioning, billing, dashboard) stay private, and licensing — not
secrecy — protects against a competitor reselling the work.

---

## Open question / next step

Map the concrete repo boundary: which current files
(`scripts/provision-instance.sh`, the container bootstrap, the install-from-URL
path) stay in this repo, and which interfaces the private control plane calls
into.

---

## Sources

- GitLab — [single codebase for CE and EE](https://about.gitlab.com/blog/a-single-codebase-for-gitlab-community-and-enterprise-edition/),
  [EE feature guidelines (`ee/` dir, `FOSS_ONLY`)](https://docs.gitlab.com/development/ee_features/)
- PostHog — [self-host disclaimer / MIT core + proprietary EE](https://posthog.com/docs/self-host/open-source/disclaimer)
- Sentry — [self-hosted vs SaaS (no open-core, FSL)](https://develop.sentry.dev/self-hosted/),
  [licensing](https://open.sentry.io/licensing)
- Open core & licensing — [Open Source vs Open Core](https://oneuptime.com/blog/post/2026-02-14-open-source-vs-open-core/view),
  [HashiCorp BSL switch (Open Core Ventures)](https://www.opencoreventures.com/blog/hashicorp-switching-to-bsl-shows-a-need-for-open-charter-companies),
  [Moving away from open source: licensing trends (Goodwin)](https://www.goodwinlaw.com/en/insights/publications/2024/09/insights-practices-moving-away-from-open-source-trends-in-licensing)
- Control/data plane split — [split-plane SaaS trends (BCG)](https://medium.com/bcgontech/latest-trends-in-saas-deployment-models-moving-towards-multi-tenancy-and-split-plane-7110650becdc),
  [why decoupling control & data planes is the future of SaaS (The New Stack)](https://thenewstack.io/why-decoupling-control-and-data-planes-is-the-future-of-saas/)
- Deployment artifacts — [Mattermost Helm charts](https://github.com/mattermost/mattermost-helm),
  [Grafana Helm charts](https://grafana.com/docs/helm-charts/)
