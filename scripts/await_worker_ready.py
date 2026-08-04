#!/usr/bin/env python3
"""Gate the deploy smoke on the deployment that carries THIS run's credentials.

`wrangler deploy` and `wrangler secret bulk` publish two different Worker
versions, and Cloudflare's rollover between them is not atomic. Both versions
report the SAME `BOBI_RELEASE_SHA`: it is a plain `var` baked into the rendered
config, identical either side of the secret write. So a gate that polls
`/health` for the release sha cannot tell the two apart. It clears while the
edge is still answering from the DEPLOY version, which carries the PREVIOUS
run's `FLEET_OPERATOR_TOKEN` and `INTERNAL_DO_SECRET` (secrets survive a
deploy, so the freshly deployed script inherits the last run's).

That is not a hypothesis. Run 30895709421 (scheduled, 2026-08-04) cleared the
sha gate on 5 consecutive matches with 0 resets, and was then smoked against a
version holding the previous run's credentials:

* `/mcp` answered 401 to an operator token minted 35 seconds earlier;
* the publish -> subscribe round-trip answered 500, the Durable Object
  refusing an internal fetch signed with a mismatched `INTERNAL_DO_SECRET`;
* `test_deployed_worker_health` passed, because the sha is the same on both
  versions, and the unauthenticated `/mcp` probe passed, because both versions
  reject a missing token. Exactly the two tests that cannot see the difference.

So readiness is not "this commit is live". It is "the version carrying this
run's credentials is live, and consistently". Three conditions, all required,
on `--needed` CONSECUTIVE probes:

1. `/health` reports the expected release sha, so a stale or foreign deploy
   never becomes ready. This is the original gate, kept exactly as strict.
2. An operator-authenticated route answers 200 to THIS run's minted token. The
   pre-secret version answers 401, so this is the condition that discriminates
   the two versions the sha cannot.
3. `worker.version_id` does not change across the streak, so the edge is
   serving ONE version rather than alternating between two mid-rollover.

Any failed condition resets the streak. On timeout this exits non-zero and
prints what it last saw; it never degrades to "close enough".

    BOBI_SMOKE_FLEET_OPERATOR_TOKEN=... python scripts/await_worker_ready.py \
        --url https://bobi-events-ci-smoke.example.workers.dev \
        --expect-sha "$GITHUB_SHA"

The token comes from the environment, never argv: argv is world-readable in
`ps` on the runner, and the workflow masks the token in the log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

#: Cloudflare's managed bot rules answer 403 to the default `Python-urllib/x.y`
#: User-Agent at the edge, so every request here sends an explicit one. Verified
#: 2026-08-01 against this same deployed Worker; the smoke module sends the
#: identical string for the identical reason. Without it this gate would never
#: clear, and the failure would look like a broken deploy rather than a 403.
SMOKE_USER_AGENT = "bobi-ci-smoke/1"

#: The minted operator token, read from the environment rather than argv.
OPERATOR_TOKEN_ENV = "BOBI_SMOKE_FLEET_OPERATOR_TOKEN"

HEALTH_PATH = "/health"

#: The credential probe. `/fleet/status` is a read-only GET behind the SAME
#: `requireOperator(..., env.FLEET_OPERATOR_TOKEN)` check that guards `/mcp`, so
#: a 200 here proves the credential the MCP smoke needs is live on the version
#: that answered. Cheaper than speaking JSON-RPC at `/mcp`, and side-effect free
#: on an empty fleet (`buildFleetStatus` lists KV and returns `{instances: []}`).
CREDENTIAL_PATH = "/fleet/status"


class NotReady(Exception):
    """The deployment never became consistently ready."""


@dataclass(frozen=True)
class Observation:
    """One probe of the deployment.

    `version_id` is carried even when the probe is not ready, so the caller can
    report WHICH version answered when a streak collapses.
    """

    ready: bool
    reason: str
    version_id: str | None = None


@dataclass(frozen=True)
class Ready:
    """A cleared gate, with the evidence that cleared it."""

    version_id: str | None
    probes: int
    resets: int


def _get(url: str, timeout: float, token: str | None = None) -> tuple[int, bytes]:
    """GET *url*, returning (status, body). HTTP errors are statuses, not raises."""
    headers = {"User-Agent": SMOKE_USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # A 401 from the credential probe is the signal this gate exists to
        # see, not an error to propagate.
        return exc.code, exc.read()


def probe(base_url: str, expected_sha: str, token: str, timeout: float = 10) -> Observation:
    """One full readiness probe: health + sha, then the credential."""
    try:
        status, body = _get(f"{base_url}{HEALTH_PATH}", timeout)
    except (urllib.error.URLError, OSError) as exc:
        return Observation(False, f"{HEALTH_PATH} did not answer: {exc}")

    if status != 200:
        return Observation(False, f"{HEALTH_PATH} -> HTTP {status}")

    try:
        health = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        return Observation(False, f"{HEALTH_PATH} returned unparseable JSON: {exc}")

    if not isinstance(health, dict):
        return Observation(False, f"{HEALTH_PATH} returned {type(health).__name__}, not an object")

    release = health.get("release")
    served_sha = release.get("sha") if isinstance(release, dict) else None
    worker = health.get("worker")
    version_id = worker.get("version_id") if isinstance(worker, dict) else None

    if served_sha != expected_sha:
        return Observation(
            False,
            f"{HEALTH_PATH} is serving release sha {served_sha!r}, expected {expected_sha!r}",
            version_id,
        )

    try:
        status, _ = _get(f"{base_url}{CREDENTIAL_PATH}", timeout, token=token)
    except (urllib.error.URLError, OSError) as exc:
        return Observation(False, f"{CREDENTIAL_PATH} did not answer: {exc}", version_id)

    if status == 401:
        return Observation(
            False,
            f"{CREDENTIAL_PATH} -> 401: the version answering does not carry this "
            "run's operator token, so it predates the secret write",
            version_id,
        )
    if status == 503:
        return Observation(
            False,
            f"{CREDENTIAL_PATH} -> 503: FLEET_OPERATOR_TOKEN is unset on the "
            "version answering",
            version_id,
        )
    if status != 200:
        return Observation(False, f"{CREDENTIAL_PATH} -> HTTP {status}", version_id)

    return Observation(True, "", version_id)


def await_ready(
    probe_once: Callable[[], Observation],
    needed: int,
    interval: float,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> Ready:
    """Poll until *needed* consecutive probes agree, or raise NotReady.

    A streak survives only while every probe is ready AND reports the same
    `version_id`. A ready probe on a DIFFERENT version restarts the count at 1
    rather than 0: that observation is itself a valid start for the new version,
    but it cannot extend a streak built on the old one. If the edge keeps
    alternating, the count keeps collapsing and the gate keeps waiting.
    """
    if needed < 1:
        raise ValueError("needed must be at least 1")

    deadline = monotonic() + timeout
    streak = 0
    streak_version: str | None = None
    probes = 0
    resets = 0
    last_reason = "no probe completed"

    while True:
        observation = probe_once()
        probes += 1

        if observation.ready:
            # `version_id` is None when the Worker reports no version metadata.
            # Treat that as "cannot discriminate" rather than "different", so
            # the gate degrades to conditions 1 and 2 instead of never clearing.
            same_version = (
                streak_version is None
                or observation.version_id is None
                or observation.version_id == streak_version
            )
            if streak and not same_version:
                resets += 1
                log(
                    f"rollover still in flight: version {observation.version_id} "
                    f"answered after {streak} match(es) on {streak_version}; "
                    "restarting the count"
                )
                # Drop the old version too, not just the count. Keeping it would
                # measure every later probe against a version that is no longer
                # serving, so the streak could never re-converge and the gate
                # would time out on a deployment that had in fact settled.
                streak = 0
                streak_version = None
            streak += 1
            if streak_version is None:
                streak_version = observation.version_id
            if streak >= needed:
                return Ready(streak_version, probes, resets)
        else:
            if streak:
                resets += 1
                log(
                    f"readiness lost after {streak} match(es): {observation.reason}; "
                    "restarting the count"
                )
            streak = 0
            streak_version = None
            last_reason = observation.reason

        if monotonic() + interval >= deadline:
            raise NotReady(
                f"the deployment did not satisfy the readiness gate on {needed} "
                f"consecutive probes within {timeout:g}s "
                f"({probes} probes, {resets} reset(s), final streak {streak}/{needed}). "
                f"Last blocking observation: {last_reason}"
            )
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for the deployed ci-smoke Worker")
    parser.add_argument("--url", required=True, help="the deployed Worker's base URL")
    parser.add_argument("--expect-sha", required=True, help="the release sha under test")
    parser.add_argument("--needed", type=int, default=5, help="consecutive matches required")
    parser.add_argument("--interval", type=float, default=3, help="seconds between probes")
    parser.add_argument("--timeout", type=float, default=180, help="seconds before giving up")
    args = parser.parse_args(argv)

    token = os.environ.get(OPERATOR_TOKEN_ENV)
    if not token:
        # Fail loudly rather than degrading to a sha-only gate: a sha-only gate
        # is precisely the bug this script was written to fix.
        print(
            f"::error::{OPERATOR_TOKEN_ENV} is unset - the readiness gate cannot "
            "prove the deployed version carries this run's credentials, which is "
            "the whole point of the gate",
            file=sys.stderr,
        )
        return 1

    base_url = args.url.rstrip("/")
    try:
        ready = await_ready(
            lambda: probe(base_url, args.expect_sha, token),
            needed=args.needed,
            interval=args.interval,
            timeout=args.timeout,
        )
    except NotReady as exc:
        print(f"::error::{base_url}: {exc}", file=sys.stderr)
        return 1

    print(
        f"{base_url} is serving release sha {args.expect_sha} with this run's "
        f"credentials on {args.needed} consecutive probes "
        f"(version {ready.version_id}, {ready.probes} probes, {ready.resets} reset(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
