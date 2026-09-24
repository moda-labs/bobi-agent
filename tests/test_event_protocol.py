"""Client-side event protocol compatibility negotiation."""

import json
from unittest.mock import patch

import httpx
import pytest

from bobi import http as pooled
from bobi.events.protocol import (
    EVENT_PROTOCOL,
    IncompatibleEventProtocol,
    InvalidEventProtocol,
    validate_server_response,
)
from bobi.events.server import register


def test_register_sends_protocol_range_and_accepts_legacy_response():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"deployment_id": "dep-1", "api_key": "moda_key"},
            request=request,
        )

    with patch.object(
        pooled,
        "_client",
        httpx.Client(transport=httpx.MockTransport(handler)),
    ):
        assert register("https://events.invalid", "sess", ["inbox/sess"]) == (
            "dep-1",
            "moda_key",
        )

    assert json.loads(requests[0].content)["protocol"] == EVENT_PROTOCOL


def test_register_accepts_overlapping_server_range():
    response = {
        "deployment_id": "dep-1",
        "api_key": "moda_key",
        "protocol": {"minimum": 1, "current": 2, "ignored": True},
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(201, json=response, request=request)
    )
    with patch.object(pooled, "_client", httpx.Client(transport=transport)):
        assert register("https://events.invalid", "sess", ["inbox/sess"]) == (
            "dep-1",
            "moda_key",
        )


@pytest.mark.parametrize(
    "protocol",
    [
        None,
        "1",
        {},
        {"minimum": 1},
        {"minimum": True, "current": 1},
        {"minimum": 2, "current": 1},
        {"minimum": 1, "current": 1 << 53},
    ],
)
def test_success_response_rejects_malformed_protocol(protocol):
    payload = {"protocol": protocol}
    with pytest.raises(InvalidEventProtocol, match="invalid event protocol"):
        validate_server_response(payload)


def test_success_response_rejects_non_overlapping_protocol():
    with pytest.raises(IncompatibleEventProtocol, match=r"client 1\.\.1.*server 2\.\.2"):
        validate_server_response({"protocol": {"minimum": 2, "current": 2}})


def test_register_surfaces_426_as_typed_incompatibility():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            426,
            json={
                "error": "incompatible_protocol",
                "detail": "event protocol ranges do not overlap",
                "protocol": {"minimum": 2, "current": 2},
            },
            request=request,
        )

    with patch.object(
        pooled,
        "_client",
        httpx.Client(transport=httpx.MockTransport(handler)),
    ):
        with pytest.raises(IncompatibleEventProtocol, match="do not overlap"):
            register("https://events.invalid", "sess", ["inbox/sess"])
