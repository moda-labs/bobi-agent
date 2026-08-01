#!/usr/bin/env python3
"""Render a CI-only wrangler config from the shipped ``wrangler.jsonc``.

Phase 2 of plans/2026-08-01-ci-coverage.md needs a REAL ``wrangler deploy`` to
prove what ``wrangler dev`` cannot: the KV binding, the ``v1``
``new_sqlite_classes`` Durable Object migration, and account-side
provisioning. That deploy must land on a dedicated ``ci-smoke`` Worker with
its own KV namespace — never the environment moda-agents runs production on,
because sharing production's KV would let smoke traffic write live event state.

Deriving the CI config from the shipped one (rather than checking in a second
hand-maintained copy) is deliberate: the compatibility date, the DO binding
and the migration tag stay identical to what production deploys, so the smoke
proves the real recipe rather than a fork of it that quietly drifts.

Isolation guards, all enforced here so a misconfigured run fails BEFORE it can
touch anything (see the plan's Phase 2 — these assert on properties this
public repo owns, because production's KV id lives in a private repo and must
never be learned here):

* the rendered ``name`` must differ from the shipped default (``bobi-events``);
* the KV id must be supplied by CI configuration, must not be the literal
  shipped in the source file, and must not be the ``REPLACE_WITH_...``
  placeholder — omitting it would make ``wrangler deploy`` silently
  auto-provision a brand-new empty namespace, the documented trap that would
  cut a bus over to empty storage.

Usage:

    python scripts/render_worker_ci_config.py \
        --name bobi-events-ci-smoke \
        --kv-namespace-id "$CI_SMOKE_KV_ID" \
        --release-version "$GITHUB_REF_NAME" \
        --release-sha "$GITHUB_SHA" \
        --out event-server/worker/wrangler.ci.jsonc
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_CONFIG = REPO_ROOT / "event-server" / "worker" / "wrangler.jsonc"

#: The name the shipped config carries. Production runs under it, so the CI
#: Worker must never be deployed under it.
PRODUCTION_WORKER_NAME = "bobi-events"

#: The deliberate placeholder in the shipped config. Reaching a deploy means
#: nothing materialized the real id.
KV_PLACEHOLDER = "REPLACE_WITH_YOUR_KV_NAMESPACE_ID"


class ConfigError(Exception):
    """The CI Worker config cannot be rendered safely."""


def strip_jsonc_comments(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments, leaving string literals intact.

    A regex would corrupt any ``//`` inside a string (a URL in a comment, a
    path in a value), so this scans character by character and tracks whether
    it is inside a string or an escape.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_jsonc(path: Path) -> dict:
    return json.loads(strip_jsonc_comments(path.read_text()))


def _events_kv_id(config: dict) -> str:
    """The EVENTS binding's namespace id, from either the source or a render."""
    for binding in config.get("kv_namespaces", []):
        if binding.get("binding") == "EVENTS":
            return str(binding.get("id", ""))
    raise ConfigError("wrangler config has no EVENTS kv_namespaces binding")


def render(
    source: dict,
    *,
    name: str,
    kv_namespace_id: str,
    release_version: str,
    release_sha: str,
) -> dict:
    """Return the CI Worker config, or raise ConfigError if it is unsafe."""
    if not name.strip():
        raise ConfigError("--name is required")
    if name == PRODUCTION_WORKER_NAME:
        raise ConfigError(
            f"refusing to render a CI config named {PRODUCTION_WORKER_NAME!r} — "
            "that is the shipped production Worker name; the CI smoke Worker "
            "must be a dedicated deployment"
        )

    kv_namespace_id = kv_namespace_id.strip()
    if not kv_namespace_id:
        raise ConfigError(
            "--kv-namespace-id is required and was empty — without it wrangler "
            "would auto-provision a fresh empty KV namespace on deploy"
        )
    if kv_namespace_id == KV_PLACEHOLDER:
        raise ConfigError(
            f"--kv-namespace-id is still the {KV_PLACEHOLDER} placeholder; "
            "supply the CI namespace id from CI configuration"
        )
    if kv_namespace_id == _events_kv_id(source):
        raise ConfigError(
            "--kv-namespace-id matches the id hardcoded in the shipped "
            "wrangler.jsonc; the CI Worker needs its own namespace, supplied "
            "by CI configuration"
        )

    config = json.loads(json.dumps(source))  # deep copy
    config["name"] = name
    # A deterministic URL to smoke against; the shipped config leaves this to
    # the operator's route configuration.
    config["workers_dev"] = True
    config.setdefault("vars", {})
    config["vars"]["BOBI_RELEASE_VERSION"] = release_version
    config["vars"]["BOBI_RELEASE_SHA"] = release_sha
    for binding in config.get("kv_namespaces", []):
        if binding.get("binding") == "EVENTS":
            binding["id"] = kv_namespace_id

    rendered = json.dumps(config)
    if KV_PLACEHOLDER in rendered:
        raise ConfigError(
            f"{KV_PLACEHOLDER} survived into the rendered config — refusing to deploy"
        )
    if f'"name": "{PRODUCTION_WORKER_NAME}"' in rendered:
        raise ConfigError(
            f"rendered config still names {PRODUCTION_WORKER_NAME!r} — refusing to deploy"
        )
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="CI Worker name")
    parser.add_argument(
        "--kv-namespace-id",
        required=True,
        help="id of the CI-only EVENTS KV namespace (never production's)",
    )
    parser.add_argument("--release-version", default="ci-smoke")
    parser.add_argument("--release-sha", default="unknown")
    parser.add_argument(
        "--source",
        type=Path,
        default=SOURCE_CONFIG,
        help="the shipped wrangler.jsonc to derive from",
    )
    parser.add_argument("--out", type=Path, required=True, help="where to write it")
    args = parser.parse_args(argv)

    try:
        config = render(
            load_jsonc(args.source),
            name=args.name,
            kv_namespace_id=args.kv_namespace_id,
            release_version=args.release_version,
            release_sha=args.release_sha,
        )
    except ConfigError as exc:
        print(f"::error::CI Worker config is unsafe to deploy: {exc}", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(config, indent=2) + "\n")
    kv_id = _events_kv_id(config)
    print(
        f"rendered {args.out}: name={config['name']} "
        f"EVENTS={kv_id[:8]}… (isolated from {PRODUCTION_WORKER_NAME})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
