#!/usr/bin/env python3
"""Build candidate records and promote only a fleet-proven GHCR digest (#930)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

PUBLIC = "moda-labs/bobi-agent"
PRIVATE = "moda-labs/moda-agents"
IMAGE = "ghcr.io/moda-labs/bobi"
EVENT = "bobi-image-proven-v1"
PLATFORMS = {"linux/amd64", "linux/arm64"}
DIGEST = r"sha256:[0-9a-f]{64}"
MAX_RECORD = 65536


class GateError(Exception):
    """Missing or inconsistent evidence; no publication is authorized."""


def require(condition, message):
    if not condition:
        raise GateError(message)


def text(value, pattern, name):
    require(isinstance(value, str) and re.fullmatch(pattern, value), f"invalid {name}")
    return value


def positive(value, name):
    require(type(value) is int and value > 0, f"invalid {name}")
    return value


def record(raw):
    require(len(raw) <= MAX_RECORD, "record too large")
    value = json.loads(raw)
    require(isinstance(value, dict), "record must be an object")
    return value


def validate_candidate(value):
    require(type(value.get("schema_version")) is int and value["schema_version"] == 1,
            "unsupported candidate schema")
    text(value.get("version"), r"[0-9][0-9A-Za-z._-]{0,100}", "version")
    text(value.get("claude_version"), r"[0-9]+\.[0-9]+\.[0-9]+", "Claude pin")
    for key in ("source_sha", "build_sha"):
        text(value.get(key), r"[0-9a-f]{40}", key)
    for key in ("producer_run_id", "producer_run_attempt"):
        positive(value.get(key), key)
    identity = f"{value['producer_run_id']}-{value['producer_run_attempt']}"
    require(value.get("candidate_id") == identity, "candidate identity mismatch")
    require(value.get("image") == IMAGE, "wrong candidate repository")
    text(value.get("index_digest"), DIGEST, "index digest")
    platforms = value.get("platform_digests")
    require(isinstance(platforms, dict) and set(platforms) == PLATFORMS,
            "candidate must contain both native platforms")
    for digest in platforms.values():
        text(digest, DIGEST, "platform digest")
    require("expected_previous_digest" in value, "missing previous digest")
    if value["expected_previous_digest"] is not None:
        text(value["expected_previous_digest"], DIGEST, "previous digest")
    return value


def validate_event(event):
    require(event.get("action") == EVENT, "wrong dispatch event")
    require(event.get("repository", {}).get("full_name") == PUBLIC, "wrong target repo")
    payload = event.get("client_payload")
    require(isinstance(payload, dict), "missing callback payload")
    keys = {"schema_version", "producer_run_id", "producer_run_attempt",
            "candidate_artifact_id", "canary_run_id", "canary_run_attempt",
            "proof_artifact_id"}
    require(set(payload) == keys and type(payload["schema_version"]) is int
            and payload["schema_version"] == 1,
            "invalid callback schema")
    for key in keys - {"schema_version"}:
        positive(payload[key], key)
    return payload


class GitHub:
    """Read cross-repository Actions evidence with the configured PAT."""

    def __init__(self, token):
        require(bool(token), "missing GitHub read token")
        self.token = token

    def get(self, path, *, binary=False):
        result = subprocess.run(["gh", "api", path], capture_output=True,
                                env={**os.environ, "GH_TOKEN": self.token}, timeout=60)
        require(result.returncode == 0, "GitHub evidence request failed")
        return result.stdout if binary else json.loads(result.stdout)

    def run(self, repo, run_id, attempt, workflow):
        latest = self.get(f"repos/{repo}/actions/runs/{run_id}")
        require(latest.get("run_attempt") == attempt, "superseded workflow attempt")
        run = self.get(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
        workflow_matches = run.get("path") == f".github/workflows/{workflow}"
        if repo == PUBLIC:
            # Reusable workflows share their caller's run ID and path. Accept
            # only the expected callee at the same protected-main commit.
            workflow_matches = workflow_matches or any(
                item.get("path") in {
                    f"{PUBLIC}/.github/workflows/{workflow}@main",
                    f"{PUBLIC}/.github/workflows/{workflow}@refs/heads/main"}
                and item.get("sha") == run.get("head_sha")
                for item in run.get("referenced_workflows", []))
        events = {"workflow_dispatch", "push", "workflow_call"} if repo == PUBLIC else {"repository_dispatch"}
        require(run.get("repository", {}).get("full_name") == repo
                and run.get("head_repository", {}).get("full_name") == repo
                and run.get("head_branch") == "main"
                and workflow_matches and run.get("event") in events
                and run.get("status") == "completed"
                and run.get("conclusion") == "success", "unproven workflow run")
        return run

    def artifact(self, repo, artifact_id, run, name, filename):
        meta = self.get(f"repos/{repo}/actions/artifacts/{artifact_id}")
        require(meta.get("name") == name and meta.get("expired") is False
                and 0 < meta.get("size_in_bytes", 0) <= MAX_RECORD
                and meta.get("workflow_run", {}).get("id") == run["id"]
                and meta["workflow_run"].get("head_sha") == run["head_sha"],
                "artifact provenance mismatch or expired artifact")
        raw = self.get(f"repos/{repo}/actions/artifacts/{artifact_id}/zip", binary=True)
        require(len(raw) <= MAX_RECORD, "artifact archive too large")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            require(archive.namelist() == [filename], "unexpected artifact members")
            require(archive.getinfo(filename).file_size <= MAX_RECORD,
                    "artifact record too large")
            return record(archive.read(filename))


class Registry:
    """Copy raw manifests in the single approved package, preserving their digest."""

    ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json"))

    def __init__(self, *, write=False):
        headers = {}
        if write:
            token = os.environ.get("GHCR_TOKEN", "")
            require(bool(token), "missing package token")
            auth = base64.b64encode(f"{os.environ['GITHUB_ACTOR']}:{token}".encode()).decode()
            headers["Authorization"] = f"Basic {auth}"
        scope = "repository:moda-labs/bobi:pull" + (",push" if write else "")
        url = "https://ghcr.io/token?" + urllib.parse.urlencode({"service": "ghcr.io", "scope": scope})
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            self.token = json.load(response)["token"]

    def manifest(self, ref):
        text(ref, rf"(?:{DIGEST}|[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}})", "registry reference")
        req = urllib.request.Request(f"https://ghcr.io/v2/moda-labs/bobi/manifests/{ref}",
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": self.ACCEPT})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, "manifest too large")
                return raw, response.headers["Content-Type"]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise GateError("registry read failed") from exc

    def digest(self, ref):
        manifest = self.manifest(ref)
        return digest_of(manifest[0]) if manifest else None

    def put(self, tag, manifest):
        raw, media = manifest
        req = urllib.request.Request(f"https://ghcr.io/v2/moda-labs/bobi/manifests/{tag}",
                                     data=raw, method="PUT", headers={
                                         "Authorization": f"Bearer {self.token}",
                                         "Content-Type": media})
        with urllib.request.urlopen(req, timeout=30) as response:
            require(response.status == 201, "manifest write failed")
        require(self.digest(tag) == digest_of(raw), f"digest readback failed for {tag}")


def digest_of(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def inspect_candidate(registry, candidate):
    manifests = {}
    for digest in [candidate["index_digest"], *candidate["platform_digests"].values()]:
        manifest = registry.manifest(digest)
        require(manifest is not None and digest_of(manifest[0]) == digest,
                "candidate manifest missing or changed")
        manifests[digest] = manifest
    index = json.loads(manifests[candidate["index_digest"]][0])
    entries = index.get("manifests", [])
    require(len(entries) == 2, "candidate index must have two platforms")
    actual = {f"{entry.get('platform', {}).get('os')}/{entry.get('platform', {}).get('architecture')}":
              entry.get("digest") for entry in entries}
    require(actual == candidate["platform_digests"], "candidate index platform mismatch")
    return manifests


def verify_proof(payload, public, private):
    producer = public.run(PUBLIC, payload["producer_run_id"], payload["producer_run_attempt"],
                          "release-image.yml")
    identity = f"{payload['producer_run_id']}-{payload['producer_run_attempt']}"
    candidate = validate_candidate(public.artifact(
        PUBLIC, payload["candidate_artifact_id"], producer,
        f"image-candidate-{identity}", "candidate.json"))
    require(candidate["candidate_id"] == identity and candidate["build_sha"] == producer["head_sha"],
            "producer binding mismatch")
    canary = private.run(PRIVATE, payload["canary_run_id"], payload["canary_run_attempt"],
                         "image-canary.yml")
    proof = private.artifact(PRIVATE, payload["proof_artifact_id"], canary,
                             f"image-canary-proof-{canary['id']}-{payload['canary_run_attempt']}",
                             "proof.json")
    required = {"schema_version": 1, "candidate_id": identity,
                "candidate_artifact_id": payload["candidate_artifact_id"],
                "index_digest": candidate["index_digest"], "version": candidate["version"],
                "canary_run_id": canary["id"], "canary_run_attempt": payload["canary_run_attempt"],
                "canary_sha": canary["head_sha"], "result": "CANARY-OK"}
    require(all(type(proof.get(k)) is type(v) and proof.get(k) == v for k, v in required.items())
            and proof.get("smoked") is True, "canary proof mismatch or smoke did not run")
    return candidate


def promote(registry, candidate, latest_release):
    validate_candidate(candidate)
    manifests = inspect_candidate(registry, candidate)
    current = registry.digest(candidate["version"])
    require(current in (candidate["expected_previous_digest"], candidate["index_digest"]),
            "final version changed since candidate creation; refusing stale promotion")
    targets = {f"{candidate['version']}-{platform.split('/')[1]}": digest
               for platform, digest in candidate["platform_digests"].items()}
    targets[candidate["version"]] = candidate["index_digest"]
    for tag, digest in targets.items():
        registry.put(tag, manifests[digest])
    # Re-read release eligibility at the latest write, after the version is verified.
    if latest_release() == f"v{candidate['version']}":
        registry.put("latest", manifests[candidate["index_digest"]])


def write_outputs(values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in values.items():
            require("\n" not in str(value), "invalid workflow output")
            output.write(f"{key}={value}\n")


def prepare():
    require(os.environ.get("GITHUB_REPOSITORY") == PUBLIC
            and os.environ.get("GITHUB_REF") == "refs/heads/main", "candidate requires public main")
    version = text(os.environ.get("VERSION"), r"[0-9][0-9A-Za-z._-]{0,100}", "version")
    claude = text(os.environ.get("CLAUDE_VERSION"), r"[0-9]+\.[0-9]+\.[0-9]+", "Claude pin")
    public = GitHub(os.environ.get("GH_TOKEN"))
    ref = public.get(f"repos/{PUBLIC}/git/ref/tags/v{version}")["object"]
    for _ in range(5):
        if ref["type"] != "tag":
            break
        ref = public.get(f"repos/{PUBLIC}/git/tags/{ref['sha']}")["object"]
    require(ref["type"] == "commit", "release tag does not resolve to a commit")
    sha = text(ref["sha"], r"[0-9a-f]{40}", "release SHA")
    require(os.environ.get("SOURCE_SHA", "") in ("", sha), "release SHA mismatch")
    previous = Registry().digest(version)
    write_outputs({"version": version, "claude-version": claude, "source-sha": sha,
                   "previous-digest": previous or "absent"})


def collect(directory):
    run_id, attempt = int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])
    identity = f"{run_id}-{attempt}"
    platforms = {}
    for arch in ("amd64", "arm64"):
        platforms[f"linux/{arch}"] = text((directory / f"{arch}.txt").read_text().strip(), DIGEST, "digest")
    registry = Registry()
    candidate = {"schema_version": 1, "candidate_id": identity,
                 "producer_run_id": run_id, "producer_run_attempt": attempt,
                 "version": os.environ["VERSION"], "claude_version": os.environ["CLAUDE_VERSION"],
                 "source_sha": os.environ["SOURCE_SHA"], "build_sha": os.environ["GITHUB_SHA"],
                 "image": IMAGE, "platform_digests": platforms,
                 "expected_previous_digest": None if os.environ["PREVIOUS_DIGEST"] == "absent"
                 else os.environ["PREVIOUS_DIGEST"],
                 "index_digest": registry.digest(f"candidate-{identity}")}
    validate_candidate(candidate)
    inspect_candidate(registry, candidate)
    Path("candidate.json").write_text(json.dumps(candidate, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "collect", "check-event", "promote"])
    parser.add_argument("--directory", type=Path, default=Path("digests"))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
        elif args.command == "collect":
            collect(args.directory)
        else:
            event = record(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
            payload = validate_event(event)
            if args.command == "promote":
                public = GitHub(os.environ.get("GH_TOKEN"))
                private = GitHub(os.environ.get("CROSS_REPO_PAT"))
                candidate = verify_proof(payload, public, private)
                latest = lambda: public.get(f"repos/{PUBLIC}/releases/latest")["tag_name"]
                latest()  # Fail before writes if release eligibility cannot be read.
                promote(Registry(write=True), candidate, latest)
                print(f"Promoted {candidate['version']} at {candidate['index_digest']}")
        return 0
    except GateError as exc:
        print(f"Image gate failed: {exc}", file=sys.stderr)
        return 1
    except (KeyError, ValueError, TypeError, OSError, zipfile.BadZipFile,
            subprocess.SubprocessError):
        # Private API content and credentials must never enter public Actions logs.
        print("Image gate failed: invalid or unavailable release/canary evidence; final publication incomplete.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
