# Spec — #397: Image generation → baked CLI (retire the `kind: image` MCP shim)

- **Issue:** [#397](https://github.com/moda-labs/modastack/issues/397)
- **Status:** Draft — awaiting Zach's approval on this PR. **No implementation lands until approved.**
- **Complexity:** Medium (spec-first).
- **Build-vs-adopt:** **Locked = adopt.** Bake the official OpenAI image CLI; do not write our own generation logic. (Issue comment, @underminedsk, 2026-06-21.)

This spec is a superset of the issue body and its three scoping comments (the
re-scope to image-only and the locked "adopt, don't build" decision).

---

## Problem & Solution

### Problem

`modastack/mcp/image_server.py` is a built-in stdio MCP server exposing one tool,
`generate_image`, injected into agent sessions by `mcp/inject.py` whenever an
`agent.yaml` connection of `kind: image` exists. It is the **worst of both
worlds**:

- It returns `{url, b64_json}` wrapped in a **text** content block
  (`json.dumps(...)` → `{"type": "text"}`), not a native MCP image block — so it
  gets **zero** native-image benefit from being an MCP server.
- The `b64_json` branch dumps a multi-hundred-KB base64 string into the model's
  **text** context (expensive; the model can't view base64-as-text as an image).
- The `url` branch hands back an OpenAI URL that **expires**.

It is also the **last live consumer** of the connection-kind + built-in-MCP
injection layer for the `image` kind. Retiring it is the precondition that
unblocks the full teardown of that middle layer (see **Sequencing**).

### Solution

Replace the MCP server with a **baked CLI capability**, consistent with the
CLI-first Axis-2 direction in `docs/design/MULTI_MODEL.md`:

- Bake the official **OpenAI image CLI** (the `openai` Python package's
  `images generate` command), version-pinned.
- Credential comes from the environment (`OPENAI_API_KEY`) at runtime — **never
  baked** into the image.
- Ship a **policy** tools guide (when to use it, the temp-file contract, that
  it's a capability call not delegation). **Syntax comes from `--help`**, per the
  function-vs-policy doctrine — flags are not hardcoded in the guide.
- Define the **agent temp-file contract**: generate → write the image to a temp
  file (`/tmp/*.png`) → print the path → the agent `Read`s the path for true
  native-image rendering, and the file is what's needed downstream (Slack post,
  PR attach).
- Remove `modastack/mcp/image_server.py` and the `kind: image` branch in
  `mcp/inject.py`.

**Breaking change accepted** (per the locked comment): the `generate_image` MCP
tool and the `kind: image` connection go away. A possibly-unseen internal
modalabs consumer that relies on it today migrates to the CLI. Intended.

---

## Sequencing

This ticket is **one slice** of a three-ticket teardown. Staying in lane matters:
over-reaching here creates merge churn with the siblings.

| Ticket | Scope | Relationship |
|--------|-------|--------------|
| **#285** (MDS-47) | Codex-as-CLI (`codex exec` + `tools/codex.md`) — replaces the `kind: codex` MCP shim's *capability*. | **Sibling.** In flight in parallel. Does not touch image code. |
| **#397** (this) | Image-gen → CLI. Remove `image_server.py` + the `kind: image` branch in `inject.py`. | **First in sequence.** |
| **#403** | Dismantle the rest: `inject.py` file, `codex_server.py`, `ConnectionEntry`, `Config.connection()`/`connections_by_kind()`, the `connections:` block, the `subagent.py` call sites, remaining tests. | **Depends on this + #285.** Final removal. |

**Explicitly deferred to #403 (do NOT touch here):**

- The `inject.py` **file itself** — it still has a live `kind: codex` branch after
  this ticket, so it stays. We remove only the `kind: image` branch.
- `modastack/mcp/codex_server.py`.
- `ConnectionEntry`, `Config.connection()`, `Config.connections_by_kind()`,
  and the `connections:` block in `agent.yaml`.
- The `inject_builtin_mcp_servers()` call sites in `subagent.py:369,504`.

Why defer: those are shared by the codex branch and only become dead once #285
lands codex-as-CLI. Removing them here would break the codex path. #397 leaves
the shim *smaller but functional*; #403 deletes the carcass.

---

## Scope

### In scope

1. **Bake the OpenAI image CLI** — version-pinned, into the image (see Decision 1
   for *where*). `OPENAI_API_KEY` from env at runtime; not baked.
2. **Tools guide** — `agents/eng-team/tools/image.md`: policy + the temp-file
   contract + the documented save-to-file one-liner. No hardcoded flag reference.
3. **Base-prompt pointer** — a short "Generate images" subsection in
   `modastack/prompts/base.md`, sibling to the existing "Call other models"
   (`aichat`) block, so every agent knows the capability exists and the
   write-file→`Read` convention.
4. **Remove** `modastack/mcp/image_server.py`.
5. **Remove** the `kind: image` branch in `mcp/inject.py` (lines 46–55) — leave
   the `kind: codex` branch intact.
6. **Tests** — delete `tests/test_image_server.py`; trim any `kind: image`
   injection assertions from `tests/test_connections.py` (keep `ConnectionEntry`
   parsing tests — that type survives until #403).

### Out of scope

- **Google Imagen.** The current server has a `_google_generate` path; the first
  cut is **OpenAI-only** (the default provider today). Document adding Google via
  its own CLI as an optional follow-on if a team needs it — do not bake a second
  provider speculatively.
- **A tight visual regenerate loop** (generate → eyeball inline → tweak). With a
  CLI this becomes generate → `Read` → eyeball — one extra call. Acceptable, not
  the common case. (If it ever becomes important, that is the *one* scenario that
  would justify a rewritten MCP server returning **native** image blocks — not
  the current text-returning one. Not in this ticket.)
- **Everything in the #403 deferral list above.**
- **A bespoke wrapper script / server** of any kind. The locked decision is
  "adopt … not a bespoke server."

---

## Technical Approach

### 1. The CLI and its actual file behavior (verified)

The `openai` package ships the image command as:

```
openai images generate --model gpt-image-1 --prompt "…"
```

**Verified against OpenAI's official CLI docs (2026-06):** the CLI has **no
native `--output`** for images yet. Saving to a file is the documented one-liner —
extract `b64_json` and decode it:

```bash
openai images generate \
  --model gpt-image-1 \
  --prompt "translucent green cube on a neutral background" \
  --format yaml \
  --transform 'data.0.b64_json' | base64 --decode > /tmp/hero.png
```

This is the **official documented save pattern**, not a workaround we invented —
which is exactly the "document the one-liner to save it" path the locked decision
named for the case where the CLI doesn't write a file natively. `base64` is GNU
coreutils (already present). The model/size/quality flags are **not** pinned in
the guide — they come from `openai images generate --help` (function-vs-policy
doctrine). Default model stays `gpt-image-1` (today's `image_server.py` default).

### 2. Baking (pinned, reproducible — #380)

- Pin the version with a build `ARG` (e.g. `OPENAI_CLI_VERSION`) and install the
  `openai` Python package at that exact version. Bump deliberately via
  `pip index versions openai` (one-line diff), per the #380 reproducibility rule
  — same rule `aichat`/`codex`/`bun` follow.
- **Arch note:** `openai` is a **pure-Python wheel** — there is *no* arch
  variance. The `dpkg --print-architecture` case statement `aichat` uses (it
  fetches an arch-specific musl binary) does **not** apply. "Arch-aware" from the
  issue is satisfied trivially: one pinned `pip install` works on amd64 and
  arm64 alike. Calling this out so the reviewer knows we are *consciously* not
  replicating the aichat case statement, not forgetting it.
- Credential: `OPENAI_API_KEY` is read from the environment at run time (already
  how `codex` and the deploy flow pass it). **Never** written into a layer.

### 3. Agent temp-file contract

The contract the guide and base prompt encode:

- **Write to `/tmp/`** with a descriptive, collision-resistant name, e.g.
  `/tmp/<slug>-<short-id>.png`. (Per-agent containers; `/tmp` is private.)
- **Print exactly the absolute path** on success (the last line), so the agent
  can `Read` it directly.
- **The agent `Read`s the path** to get native-image rendering, then uses the
  same file downstream (`slack-upload-file`, PR attachment) — no base64 ever
  enters the model's text context.
- **Errors fail loudly** to stderr with a non-zero exit (missing
  `OPENAI_API_KEY`, API error, empty result) — never a silent empty file.
- **Cleanup:** `/tmp` is ephemeral per container; the agent deletes intermediates
  it no longer needs. No daemon, no registry.

### 4. Removals (this ticket only)

- Delete `modastack/mcp/image_server.py` (304 lines).
- In `mcp/inject.py`, delete the `if _has_kind("image"):` block
  (`modastack-image` injection, lines 46–55). Keep the `kind: codex` block, the
  helpers, and the function signature. Update the module docstring to say only
  codex is injected.
- `tests/test_image_server.py` → delete.
- `tests/test_connections.py` → remove any assertion that a `kind: image`
  connection injects `modastack-image`; keep `ConnectionEntry` parse tests.

After this ticket: `inject.py` still compiles and still injects `modastack-codex`;
nothing references `image_server`. `grep -rn 'image_server\|generate_image\|kind.*image'`
returns only docs/spec mentions.

---

## Decision Points (RESOLVED in review — Zach, 2026-06-22)

> The "adopt OpenAI image CLI" call is already locked. These are the open
> implementation choices the locked comment left under-specified.
>
> **Resolved (Zach, PR #407 review, 2026-06-22): ship this PR as designed.**
> - **D1 = Option B** — bake via the eng-team `build:` spec (provider-specific
>   tool, consistent with how `codex` is baked); NOT base-baked (Option A).
> - **D2 = Option A** — documented save-to-file one-liner; no bespoke wrapper.
> - **D3 = both** — `agents/eng-team/tools/image.md` guide **plus** the
>   `base.md` pointer.
> - The **tool-library convention** (D1 Option C) is split out to its own
>   follow-on: **#416** — a define-once catalog migrating aichat, codex, the
>   OpenAI image CLI, venn, and gstack. Option B here is exactly what a library
>   entry expands into, so #416 lands with zero rework against this slice.

**D1 — Where to bake: base `Dockerfile` vs eng-team `build:` spec.**
The locked comment says "bake into the base `Dockerfile` alongside aichat/codex."
But that premise is factually off for `codex`: **`aichat` is base-baked**
(`Dockerfile:169`, provider-generic musl binary), while **`codex` is *team*-baked**
via the eng-team `build:` spec (`agent.yaml:71`, `npm: @openai/codex@…`) — *not*
in the base Dockerfile. The OpenAI image CLI is **OpenAI-specific**, like codex,
not provider-generic like aichat.
- **Option A — base `Dockerfile`** (literal reading of the locked comment):
  universal, every team gets image-gen for free. Cost: an OpenAI-specific tool in
  an otherwise provider-generic base image.
- **Option B — eng-team `build:` spec** (recommended): consistent with how the
  other OpenAI-specific tool (`codex`) is actually baked today; keeps the base
  image provider-generic; only teams that opt in carry it.
- **Option C — pre-configured tool library (Zach, 2026-06-22).** A curated
  catalog where each entry bundles the runtime binary (e.g. `openai`, `codex`)
  *and* its tools guide markdown, and a team opts in with a one-line reference in
  `agent.yaml` (e.g. `tool_library: [openai, codex]`) instead of hand-editing
  three coordinated places.

> **My take on C (the convention question Zach asked):** it is **better than
> today's convention, and worth doing — but as its own ticket, not inside #397.**
>
> *Why better.* Adopting an OpenAI-specific tool today touches **three** places
> that must stay in lockstep: `requires:` (check/fix), `build:` (the pinned
> install), and `tools/<name>.md` (the guide). They already drift-by-design — the
> codex version pin is **hand-duplicated** in `agent.yaml`: `requires.fix` says
> `@openai/codex@0.141.0` and `build.npm` says `@openai/codex@0.141.0`. A library
> entry collapses that to one pinned definition + one guide, opted into by name —
> strictly less surface to keep in sync, and reusable across teams (eng-team,
> dogfood, market-research today each re-derive their own bake).
>
> *Why it does **not** reintroduce what #397/#403 are tearing down.* The
> `kind: image` MCP shim is a **runtime** indirection — it wraps a capability in
> an injected server. A tool library is a **build/config-time** convenience that
> *expands to the primitives we already have* (a `build:` bake + a `tools/*.md`
> guide); the agent still calls the bare CLI directly. It removes hand-coordination,
> it doesn't add a runtime layer. So it is on the right side of the line we drew.
>
> *Why not in #397.* This ticket is explicitly one slice of a three-ticket
> teardown ("stay in lane"); designing a catalog format + a resolver that expands
> a library reference into `requires`+`build`+guide at render time is a net-new
> framework feature with its own blast radius, and bolting it onto a teardown
> slice would create merge churn with #285/#403. It deserves its own spec.
>
> *Crucially, picking B now does not paint us into a corner.* B is exactly what a
> future library entry would expand into — when the library lands, the
> openai-image bake + `tools/image.md` lift into a catalog entry mechanically,
> with no rework. A (base Dockerfile) is the option to avoid, because base-baking
> an OpenAI-specific tool is itself an inconsistency the library would later have
> to unwind.
- **Recommendation: ship #397 on B now; capture the tool library as its own
  issue/spec.** Happy to file that ticket (problem = three-places-by-hand +
  duplicated pins; proposal = named catalog of binary+guide, opt-in via
  `agent.yaml`) and link it here on your nod — that's the "best of both worlds":
  #397 lands small and consistent, your convention improvement gets designed
  properly instead of rushed.

**D2 — Save-to-file: documented one-liner vs thin wrapper.**
The CLI has no native file output (verified above). The locked decision says
"document the one-liner … not a bespoke server."
- **Option A — guide one-liner** (recommended, matches the locked decision): the
  `--transform 'data.0.b64_json' | base64 --decode > /tmp/x.png` pattern lives in
  the tools guide. Zero new code to maintain.
- **Option B — a tiny baked wrapper command** that writes the file and prints the
  path. Nicer ergonomics, but it is net-new code we own — closer to the
  "bespoke" the locked decision rejected.
- **Recommendation: A.**

**D3 — Guide surface.** Proposed: a new `agents/eng-team/tools/image.md` (full
policy) **plus** a 4–6 line pointer in `modastack/prompts/base.md` next to the
`aichat` block. Confirm both, or guide-only.

---

## Verification Plan

- **Unit:** `pytest tests/ --ignore=tests/integration/` green after the test
  edits. `tests/test_image_server.py` gone; `tests/test_connections.py` passes
  with the image-injection assertion removed; `tests/test_codex_server.py` and
  the codex injection path **unchanged and still green** (proves we didn't touch
  the codex branch).
- **Dead-reference grep:** `grep -rn 'image_server\|generate_image' modastack/`
  returns nothing; `grep -rn 'modastack-image' modastack/` returns nothing.
- **`inject.py` still works:** a focused test (or the existing codex test)
  confirms a `kind: codex` connection still injects `modastack-codex` and a
  `kind: image` connection now injects **nothing**.
- **Build smoke (CI image build):** the pinned `openai` install succeeds; `openai
  images generate --help` exits 0 in the built image (mirrors `aichat --version`
  at `Dockerfile:178`). No `OPENAI_API_KEY` needed for `--help`.
- **Manual capability check (with a key):** run the documented one-liner, confirm
  `/tmp/*.png` is a valid PNG and the path is printed; `Read` it renders.

## Implementation Plan

1. **Bake** the pinned `openai` CLI in the location chosen in **D1**; add the
   pin-bump comment (mirror the aichat/#380 style).
2. **Write** `agents/eng-team/tools/image.md` (policy + temp-file contract +
   documented save one-liner; no hardcoded flags) and the `base.md` pointer
   (pending **D3**).
3. **Remove** `image_server.py` and the `kind: image` branch in `inject.py`;
   update the `inject.py` docstring.
4. **Tests:** delete `test_image_server.py`; trim `test_connections.py`; add the
   "image injects nothing now" assertion.
5. `pytest tests/ --ignore=tests/integration/`; dead-reference grep; build smoke.
6. `/review`, then open the implementation PR against `main` linking #397.

## Rollback

Pure deletion + additive bake. Rollback = revert the PR: `image_server.py` and
the `kind: image` branch return, the baked CLI layer drops. No migrations, no
state, no data. The only externally-visible change is the removed
`generate_image` MCP tool (breaking change, accepted).
