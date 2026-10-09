"""TypeSafe System One Choice adapter; no provider session or transcript access."""

import json
import math
import os
from urllib.parse import urlsplit

import httpx

from bobi.metrics.policy import PolicyConfig, PolicyError, PolicyRequest, PolicyResult

class TypeSafePolicy:
    name = "typesafe-jev"

    def __init__(self, config: PolicyConfig, *, api_key: str | None = None) -> None:
        options = json.loads(config.options_json)
        if set(options) - {"endpoint", "instructions", "criteria"}:
            raise ValueError("unsupported TypeSafe policy options")
        self.endpoint = options.get("endpoint", "https://api.typesafe.ai/v1/systemone")
        if not isinstance(self.endpoint, str) or any(ord(char) <= 32 or ord(char) == 127 for char in self.endpoint):
            raise ValueError("invalid TypeSafe endpoint")
        endpoint = urlsplit(self.endpoint)
        if endpoint.port == 0:
            raise ValueError("invalid TypeSafe endpoint port")
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("TypeSafe endpoint requires HTTPS without credentials")
        self.instructions = options.get("instructions")
        self.criteria = options.get("criteria")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("TypeSafe requires routing instructions")
        if (not isinstance(self.criteria, dict)
                or set(self.criteria) != set(config.candidate_models)
                or any(not isinstance(value, str) or not value.strip() for value in self.criteria.values())):
            raise ValueError("TypeSafe requires criteria for every candidate")
        self.secret_env_names = (config.credential_env,)
        self.credential_env = config.credential_env
        self.api_key = api_key
        if config.version in {"jev-latest", "jev-preview"}:
            raise ValueError("TypeSafe requires a pinned model version")

    async def decide(self, request: PolicyRequest, *, timeout_s: float) -> PolicyResult:
        key = self.api_key if self.api_key is not None else os.environ.get(self.credential_env, "")
        if not key:
            raise PolicyError("policy_unauthenticated", authentication=True)
        if timeout_s <= 0:
            raise PolicyError("policy_timeout")
        if set(request.candidate_models) != set(self.criteria):
            raise PolicyError("policy_invalid_response")
        try:
            outgoing = {
                "model": request.pinned_version, "state": dict(request.features),
                "questions": {"route": {"type": "choice",
                    "instructions": self.instructions, "criteria": self.criteria}},
            }
            payload = json.dumps(outgoing, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > 131072:
                raise PolicyError("policy_invalid_response")
            async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
                async with client.stream("POST", self.endpoint, headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }, content=payload) as response:
                    if response.status_code in {401, 403}:
                        raise PolicyError("policy_unauthenticated", authentication=True)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise PolicyError("policy_unavailable")
                    if response.status_code != 200:
                        raise PolicyError("policy_invalid_response")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > 65536:
                            raise PolicyError("policy_invalid_response")
                        content.extend(chunk)
            raw = json.loads(content)
            json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
            answer = raw["answers"]["route"]
            probabilities = answer["probabilities"]
            confidence = answer["confidence"]
            if (answer["type"] != "choice" or answer["choice"] not in request.candidate_models
                    or not isinstance(raw["model"], str)
                    or not isinstance(probabilities, dict)
                    or set(probabilities) != set(request.candidate_models)
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) or not 0 <= value <= 1
                           for value in probabilities.values())
                    or not math.isclose(sum(probabilities.values()), 1, abs_tol=1e-6)
                    or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise PolicyError("policy_invalid_response")
            usage = raw["usage"]
            if any(type(usage[name]) is not int or usage[name] < 0 for name in ("input_tokens", "output_tokens")):
                raise PolicyError("policy_invalid_response")
            return PolicyResult(answer["choice"], confidence, None, raw["model"], None, None,
                                probabilities, raw, outgoing)
        except httpx.TimeoutException:
            raise PolicyError("policy_timeout") from None
        except httpx.HTTPError:
            raise PolicyError("policy_unavailable") from None
        except (ValueError, KeyError, TypeError, RecursionError):
            raise PolicyError("policy_invalid_response") from None
