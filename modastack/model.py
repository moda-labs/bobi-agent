"""ModelSpec resolver with precedence-based model selection.

Resolves which model to use for a connection by applying a precedence
chain: explicit request > connection config > provider default.

Also provides per-provider default env-var naming conventions so that
gateway connections can auto-discover credentials from the environment
without requiring explicit ``api_key`` entries in agent.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modastack.config import ConnectionEntry


# ---------------------------------------------------------------------------
# Provider defaults
# ---------------------------------------------------------------------------

# Default models per provider when none is specified in the connection.
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "google": "gemini-2.5-pro",
    "gemini": "gemini-2.5-pro",
    "anthropic": "claude-sonnet-4-20250514",
    "openrouter": "anthropic/claude-sonnet-4",
    "together": "meta-llama/Llama-3-70b-chat-hf",
}

# Default base URLs per provider for gateway connections.
PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
}

# Canonical env-var name per provider for API keys.  When a connection
# omits ``api_key``, the resolver checks these env vars automatically.
PROVIDER_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
}


# ---------------------------------------------------------------------------
# ModelSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSpec:
    """Resolved model specification for a connection.

    Fields are populated by :func:`resolve` after applying the full
    precedence chain.
    """

    provider: str
    model: str
    api_key: str
    base_url: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def resolve(
    connection: ConnectionEntry,
    *,
    requested_model: str = "",
) -> ModelSpec:
    """Resolve a fully-populated :class:`ModelSpec` from a connection.

    Precedence (highest first):

    1. *requested_model* — an explicit override from the caller.
    2. ``connection.model`` — declared in agent.yaml.
    3. :data:`PROVIDER_DEFAULT_MODELS` — built-in fallback.

    For ``api_key`` the chain is:

    1. ``connection.api_key`` — declared (possibly via ``${ENV_VAR}``).
    2. :data:`PROVIDER_ENV_VARS` — auto-discovered from environment.

    For ``base_url`` the chain is:

    1. ``connection.extra["base_url"]`` — declared.
    2. :data:`PROVIDER_BASE_URLS` — built-in fallback.
    """
    provider = connection.provider.lower()

    model = (
        requested_model
        or connection.model
        or PROVIDER_DEFAULT_MODELS.get(provider, "")
    )

    api_key = connection.api_key
    if not api_key:
        env_var = PROVIDER_ENV_VARS.get(provider, "")
        if env_var:
            api_key = os.environ.get(env_var, "")

    base_url = (
        connection.extra.get("base_url", "")
        or PROVIDER_BASE_URLS.get(provider, "")
    )

    # Pass through any extra fields that aren't base_url.
    extra = {k: v for k, v in connection.extra.items() if k != "base_url"}

    return ModelSpec(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Per-provider connection naming
# ---------------------------------------------------------------------------

def default_connection_name(provider: str, kind: str) -> str:
    """Generate a canonical connection name from provider and kind.

    Examples::

        >>> default_connection_name("openai", "gateway")
        'openai-gateway'
        >>> default_connection_name("google", "image")
        'google-image'
    """
    return f"{provider.lower()}-{kind.lower()}"


def env_var_for_provider(provider: str) -> str:
    """Return the canonical env-var name for a provider's API key.

    Returns an empty string if the provider is not in the known map.
    """
    return PROVIDER_ENV_VARS.get(provider.lower(), "")
