import asyncio
from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from pathlib import Path

import httpx
import pytest

from bobi.metrics.policies.typesafe import TypeSafePolicy
from bobi.metrics.policy import PolicyConfig, PolicyError, PolicyRequest
from bobi.metrics.policy import CircuitBreaker, call_policy


def config():
    return PolicyConfig("typesafe-jev", "jev-1.13.0", "codex", "shadow",
        ("control", "cheap"), ("session_start",), ("engineer",), "none",
        "TEST_TYPESAFE_KEY", json.dumps({"instructions": "Choose a model",
        "criteria": {"control": "Complex task", "cheap": "Simple task"}}))

def body():
    return {"model": "jev-1.13.0", "answers": {"route": {"type": "choice",
        "choice": "cheap", "confidence": 0.94,
        "probabilities": {"cheap": 0.97, "control": 0.03}}},
        "usage": {"input_tokens": 100, "output_tokens": 20}}

def run(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setenv("TEST_TYPESAFE_KEY", "test-only-secret")
    monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    request = PolicyRequest("jev-features-v1", {"prompt_bytes": 10},
                            ("control", "cheap"), "control", "jev-1.13.0")
    return asyncio.run(TypeSafePolicy(config()).decide(request, timeout_s=1))

def test_real_wire_contract_is_one_bounded_call(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raw = json.loads(request.content)
        assert raw["model"] == "jev-1.13.0"
        assert raw["state"] == {"prompt_bytes": 10}
        assert raw["questions"]["route"]["type"] == "choice"
        assert request.headers["authorization"] == "Bearer test-only-secret"
        return httpx.Response(200, json=body())
    result = run(monkeypatch, handler)
    assert result.model == "cheap" and result.model_version == "jev-1.13.0"
    assert result.cost_usd is None and result.reason_text is None
    assert len(calls) == 1

@pytest.mark.parametrize("status,reason,auth", [
    (401, "policy_unavailable", True), (403, "policy_unavailable", True),
    (429, "policy_unavailable", False), (529, "policy_unavailable", False),
    (422, "policy_invalid_response", False), (302, "policy_invalid_response", False),
])
def test_http_errors_are_redacted_and_never_retried(monkeypatch, status, reason, auth):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private vendor response", headers={"location": "https://other.example"})
    with pytest.raises(PolicyError) as caught:
        run(monkeypatch, handler)
    assert caught.value.reason == reason and caught.value.authentication == auth
    assert "private" not in str(caught.value) and len(calls) == 1

@pytest.mark.parametrize("mutation", ["unknown", "nan", "sum", "usage", "oversized"])
def test_malformed_vendor_response_fails_closed(monkeypatch, mutation):
    raw = body()
    if mutation == "unknown":
        raw["answers"]["route"]["choice"] = "not-a-candidate"
    elif mutation == "nan":
        raw["answers"]["route"]["confidence"] = float("nan")
    elif mutation == "sum":
        raw["answers"]["route"]["probabilities"]["control"] = 0.5
    elif mutation == "usage":
        raw["usage"]["input_tokens"] = True
    else:
        raw["extra"] = "x" * 65536
    with pytest.raises(PolicyError, match="policy_invalid_response"):
        run(monkeypatch, lambda request: httpx.Response(200, content=json.dumps(raw)))

def test_adapter_validates_endpoint_pin_and_criteria():
    for options in (
        {"endpoint": "http://example.com", "instructions": "route", "criteria": {"control": "a", "cheap": "b"}},
        {"instructions": "route", "criteria": {"cheap": "b"}},
        {"instructions": "route", "criteria": {"control": "a", "cheap": "b"}, "unknown": 1},
    ):
        with pytest.raises(ValueError):
            TypeSafePolicy(replace(config(), options_json=json.dumps(options)))
    with pytest.raises(ValueError, match="pinned"):
        TypeSafePolicy(replace(config(), version="jev-latest"))


@pytest.mark.parametrize("features", [{"task": "界" * 50000}, {"confidence": float("nan")}])
def test_request_bounds_reject_before_network(monkeypatch, features):
    monkeypatch.setenv("TEST_TYPESAFE_KEY", "test-only-secret")
    def no_client(**kwargs):
        raise AssertionError("invalid request must not create an HTTP client")
    monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient", no_client)
    request = PolicyRequest("jev-features-v1", features,
                            ("control", "cheap"), "control", "jev-1.13.0")
    with pytest.raises(PolicyError, match="policy_invalid_response"):
        asyncio.run(TypeSafePolicy(config()).decide(request, timeout_s=1))


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectTimeout, "policy_timeout"),
    (httpx.ReadTimeout, "policy_timeout"),
    (httpx.ConnectError, "policy_unavailable"),
    (httpx.RemoteProtocolError, "policy_unavailable"),
])
def test_transport_errors_are_sanitized_and_not_retried(monkeypatch, error, reason):
    calls = []
    def handler(request):
        calls.append(request)
        raise error("API_KEY=private-vendor-error", request=request)
    with pytest.raises(PolicyError) as caught:
        run(monkeypatch, handler)
    assert caught.value.reason == reason
    assert "private" not in str(caught.value)
    assert len(calls) == 1


def test_oversized_stream_is_closed_without_consuming_remaining_chunks(monkeypatch):
    state = {"chunks": 0, "closed": False}
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3):
                state["chunks"] += 1
                yield b"x" * 40000
        async def aclose(self):
            state["closed"] = True
    with pytest.raises(PolicyError, match="policy_invalid_response"):
        run(monkeypatch, lambda request: httpx.Response(200, stream=OversizedStream()))
    assert state == {"chunks": 2, "closed": True}


@pytest.mark.parametrize("fault,reason", [("5xx", "policy_unavailable"),
    ("invalid_model", "policy_invalid_response"), ("version", "policy_version_drift"),
    ("timeout", "policy_timeout")])
def test_adapter_guard_with_loopback_fault_server(monkeypatch, fault, reason):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if fault == "timeout":
                time.sleep(1)
                return
            raw = body()
            if fault == "invalid_model":
                raw["answers"]["route"]["choice"] = "unknown"
            if fault == "version":
                raw["model"] = "changed-version"
            self.send_response(503 if fault == "5xx" else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(raw).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_TYPESAFE_KEY", "test-only-secret")
    policy = TypeSafePolicy(config())
    policy.endpoint = f"http://127.0.0.1:{server.server_port}/"
    request = PolicyRequest("jev-features-v1", {"prompt_bytes": 10},
                            ("control", "cheap"), "control", "jev-1.13.0")
    try:
        started = time.monotonic()
        result, failure = asyncio.run(call_policy(policy, request,
            replace(config(), deadline_ms=200), breaker=CircuitBreaker()))
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert failure == reason
    assert len(calls) == 1
    if fault == "timeout":
        assert result is None and elapsed < 0.8
