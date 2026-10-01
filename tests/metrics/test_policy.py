import asyncio

import pytest

from bobi.metrics.policy import PolicyRequest, StaticPolicy


def test_static_policy_selects_only_an_allowed_model():
    request = PolicyRequest("jev-features-v1", {}, ("control", "cheap"), "control", "v1")
    result = asyncio.run(StaticPolicy("cheap").decide(request, timeout_s=1))
    assert result.model == "cheap"
    assert result.model_version == "v1"
    assert result.confidence == 1.0
    with pytest.raises(ValueError, match="candidate"):
        asyncio.run(StaticPolicy("unknown").decide(request, timeout_s=1))
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(StaticPolicy("cheap").decide(request, timeout_s=0))
    with pytest.raises(ValueError, match="requires a model"):
        StaticPolicy("")
