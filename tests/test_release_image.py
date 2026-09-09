"""Exercise image gate failures before writes and exact-byte promotion."""

import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location(
    "release_image", Path(__file__).resolve().parents[1] / "scripts/release_image.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class Registry:
    def __init__(self):
        self.manifests = {}
        self.writes = []
        self.fail_at = None

    def add(self, value, media="application/vnd.oci.image.manifest.v1+json"):
        raw = json.dumps(value).encode()
        digest = gate.digest_of(raw)
        self.manifests[digest] = raw, media
        return digest

    def manifest(self, ref):
        return self.manifests.get(ref)

    def digest(self, ref):
        found = self.manifest(ref)
        return gate.digest_of(found[0]) if found else None

    def put(self, tag, manifest):
        if len(self.writes) == self.fail_at:
            raise gate.GateError("interrupted write")
        self.writes.append(tag)
        self.manifests[tag] = manifest


@pytest.fixture
def candidate():
    registry = Registry()
    platforms = {f"linux/{arch}": registry.add({"schemaVersion": 2, "arch": arch})
                 for arch in ("amd64", "arm64")}
    digest = registry.add({"schemaVersion": 2, "manifests": [
        {"platform": {"os": "linux", "architecture": arch}, "digest": platforms[f"linux/{arch}"]}
        for arch in ("amd64", "arm64")]}, "application/vnd.oci.image.index.v1+json")
    value = {"schema_version": 1, "candidate_id": "10-1", "producer_run_id": 10,
             "producer_run_attempt": 1, "version": "1.2.3", "claude_version": "2.3.4",
             "source_sha": "a" * 40, "build_sha": "b" * 40, "image": gate.IMAGE,
             "index_digest": digest, "platform_digests": platforms,
             "expected_previous_digest": None}
    return value, registry


@pytest.fixture
def event():
    return {"action": gate.EVENT, "sender": {"id": 123, "type": "Bot"},
            "repository": {"full_name": gate.PUBLIC}, "client_payload": {
                "schema_version": 1, "producer_run_id": 10, "producer_run_attempt": 1,
                "candidate_artifact_id": 12, "canary_run_id": 20,
                "canary_run_attempt": 1, "proof_artifact_id": 22}}


@pytest.mark.parametrize("key,value", [
    ("version", "1.2.3\nlatest"), ("version", "$(touch /tmp/no)"),
    ("claude_version", "stable"), ("source_sha", "abcdef"),
    ("image", "example.com/foreign"), ("index_digest", "sha256:abc"),
    ("producer_run_id", True), ("producer_run_attempt", 0),
    ("candidate_id", "10-2"), ("expected_previous_digest", "missing"),
    ("platform_digests", {}), ("schema_version", 2), ("schema_version", True),
])
def test_invalid_candidate_rejected_without_writes(candidate, key, value):
    data, registry = candidate
    data[key] = value
    with pytest.raises(gate.GateError):
        gate.promote(registry, data, lambda: "v1.2.3")
    assert registry.writes == []


@pytest.mark.parametrize("sender", [{"id": 123, "type": "User"}, {"id": 999, "type": "Bot"}, {}])
def test_sender_is_not_release_authority(event, sender):
    event["sender"] = sender
    assert gate.validate_event(event) == event["client_payload"]


@pytest.mark.parametrize("key,value", [("canary_run_id", "20"), ("canary_run_attempt", False),
                                      ("schema_version", 2), ("extra", "untrusted")])
def test_bad_payload_rejected(event, key, value):
    event["client_payload"][key] = value
    with pytest.raises(gate.GateError):
        gate.validate_event(event)


def test_valid_event(event):
    assert gate.validate_event(event) == event["client_payload"]


@pytest.mark.parametrize("latest", ["v1.2.3", "v2.0.0"])
def test_promotion_copies_identical_manifest_bytes(candidate, latest):
    data, registry = candidate
    gate.promote(registry, data, lambda: latest)
    assert registry.manifest("1.2.3") == registry.manifest(data["index_digest"])
    for platform, digest in data["platform_digests"].items():
        assert registry.manifest("1.2.3-" + platform.split("/")[1]) == registry.manifest(digest)
    assert ("latest" in registry.writes) == (latest == "v1.2.3")


def test_stale_callback_does_not_replace_newer_image(candidate):
    data, registry = candidate
    other = registry.add({"another": "candidate"})
    registry.manifests["1.2.3"] = registry.manifest(other)
    with pytest.raises(gate.GateError, match="stale"):
        gate.promote(registry, data, lambda: "v1.2.3")
    assert registry.writes == []


def test_corrective_republish_requires_expected_previous_digest(candidate):
    data, registry = candidate
    previous = registry.add({"old": "version"})
    registry.manifests["1.2.3"] = registry.manifest(previous)
    data["expected_previous_digest"] = previous
    gate.promote(registry, data, lambda: "v1.2.3")
    assert registry.digest("1.2.3") == data["index_digest"]


@pytest.mark.parametrize("fail_at", [0, 1, 2, 3])
def test_retry_recovers_partial_promotion_without_rebuilding(candidate, fail_at):
    data, registry = candidate
    registry.fail_at = fail_at
    with pytest.raises(gate.GateError):
        gate.promote(registry, data, lambda: "v1.2.3")
    registry.fail_at = None
    gate.promote(registry, data, lambda: "v1.2.3")
    gate.promote(registry, data, lambda: "v1.2.3")
    assert registry.digest("latest") == data["index_digest"]


def test_missing_child_or_wrong_index_rejected_before_writes(candidate):
    data, registry = candidate
    del registry.manifests[data["platform_digests"]["linux/arm64"]]
    with pytest.raises(gate.GateError, match="missing"):
        gate.promote(registry, data, lambda: "v1.2.3")
    assert not registry.writes


@pytest.fixture
def evidence(candidate, event):
    data, _ = candidate
    payload = event["client_payload"]
    proof = {"schema_version": 1, "candidate_id": "10-1", "candidate_artifact_id": 12,
             "index_digest": data["index_digest"], "version": "1.2.3", "canary_run_id": 20,
             "canary_run_attempt": 1, "canary_sha": "c" * 40, "result": "CANARY-OK", "smoked": True}

    class Evidence:
        def run(self, repo, run_id, attempt, workflow):
            return {"id": run_id, "head_sha": ("b" if repo == gate.PUBLIC else "c") * 40}

        def artifact(self, repo, artifact_id, run, name, filename):
            return data if repo == gate.PUBLIC else proof

    return payload, proof, Evidence()


def test_valid_completed_canary_binds_candidate(evidence, candidate):
    payload, _, api = evidence
    assert gate.verify_proof(payload, api, api) == candidate[0]


@pytest.mark.parametrize("key,value", [("smoked", False), ("smoked", "true"),
                                      ("result", "SKIPPED"), ("candidate_id", "10-2"),
                                      ("index_digest", "sha256:" + "f" * 64),
                                      ("version", "9.9.9"), ("canary_run_attempt", 2)])
def test_unexecuted_or_mismatched_proof_rejected(evidence, key, value):
    payload, proof, api = evidence
    proof[key] = value
    with pytest.raises(gate.GateError):
        gate.verify_proof(payload, api, api)


def run_record():
    return {"id": 10, "run_attempt": 1, "repository": {"full_name": gate.PUBLIC},
            "head_repository": {"full_name": gate.PUBLIC}, "head_branch": "main",
            "path": ".github/workflows/release-image.yml", "event": "workflow_dispatch",
            "status": "completed", "conclusion": "success", "head_sha": "b" * 40}


@pytest.mark.parametrize("key,value", [("head_branch", "fork"), ("run_attempt", 2),
                                      ("path", ".github/workflows/unrelated.yml"),
                                      ("conclusion", "skipped"), ("conclusion", "cancelled"),
                                      ("status", "in_progress"), ("event", "pull_request"),
                                      ("head_repository", {"full_name": "fork/repo"})])
def test_github_run_provenance_is_independently_checked(monkeypatch, key, value):
    api = gate.GitHub("test-token")
    response = run_record()
    response[key] = value
    monkeypatch.setattr(api, "get", lambda path: response)
    with pytest.raises(gate.GateError):
        api.run(gate.PUBLIC, 10, 1, "release-image.yml")


def test_reusable_candidate_binds_the_trusted_callee_sha(monkeypatch):
    api = gate.GitHub("test-token")
    response = run_record()
    response["path"] = ".github/workflows/caller.yml"
    response["referenced_workflows"] = [{
        "path": f"{gate.PUBLIC}/.github/workflows/release-image.yml@main", "sha": "b" * 40}]
    monkeypatch.setattr(api, "get", lambda path: response)
    assert api.run(gate.PUBLIC, 10, 1, "release-image.yml")["id"] == 10
    response["referenced_workflows"][0]["sha"] = "a" * 40
    with pytest.raises(gate.GateError):
        api.run(gate.PUBLIC, 10, 1, "release-image.yml")


@pytest.mark.parametrize("filename", ["../candidate.json", "another.json"])
def test_artifact_cannot_supply_another_path(monkeypatch, filename):
    api = gate.GitHub("test-token")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(filename, "{}")
    meta = {"name": "image-candidate-10-1", "expired": False, "size_in_bytes": 200,
            "workflow_run": {"id": 10, "head_sha": "b" * 40}}
    monkeypatch.setattr(api, "get", lambda path, binary=False: archive.getvalue() if binary else meta)
    with pytest.raises(gate.GateError):
        api.artifact(gate.PUBLIC, 12, run_record(), "image-candidate-10-1", "candidate.json")


def test_registry_adapter_put_preserves_raw_body_and_content_type(monkeypatch):
    registry = object.__new__(gate.Registry)
    registry.token = "test-token"
    raw = b'{"schemaVersion":2,"manifests":[]}'
    media = "application/vnd.oci.image.index.v1+json"
    requests = []

    class Response:
        status = 201
        headers = {"Content-Type": media}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return raw

    def open_request(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(gate.urllib.request, "urlopen", open_request)
    registry.put("1.2.3", (raw, media))
    assert requests[0].method == "PUT"
    assert requests[0].data == raw
    assert requests[0].get_header("Content-type") == media
    assert len(requests) == 2  # Mandatory digest readback after writing.


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_registry_error_is_not_tag_absence(monkeypatch, code):
    registry = object.__new__(gate.Registry)
    registry.token = "test-token"

    def denied(request, timeout):
        raise gate.urllib.error.HTTPError(request.full_url, code, "denied", {}, None)

    monkeypatch.setattr(gate.urllib.request, "urlopen", denied)
    with pytest.raises(gate.GateError, match="read failed"):
        registry.digest("1.2.3")
