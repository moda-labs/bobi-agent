"""Built-in MCP server for gateway (LLM proxy) connections.

Provides a ``gateway_chat`` tool that sends chat-completion requests to
any OpenAI-compatible endpoint (OpenAI, OpenRouter, Together, Google
Gemini via its OpenAI-compat layer, etc.).

The server resolves credentials and base URLs via :mod:`modastack.model`
so that connections can rely on env-var auto-discovery and provider
defaults.

Runs as a stdio MCP server, injected into agent sessions via the
harness registry in :mod:`modastack.mcp.inject`.
"""

from __future__ import annotations

import json
import logging
import sys

import httpx

from modastack import http as pooled

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chat completion via OpenAI-compatible endpoint
# ---------------------------------------------------------------------------

def _post_json(url: str, body: dict, headers: dict,
               provider_label: str, timeout: float = 120.0) -> dict:
    """POST JSON and return parsed response.  Never raises."""
    try:
        resp = pooled.post(
            url,
            json=body,
            headers={**headers, "Content-Type": "application/json"},
            timeout=timeout,
        )
        return resp.json()
    except httpx.HTTPStatusError as e:
        err_body = e.response.text[:500]
        return {"error": f"{provider_label} API error {e.response.status_code}: {err_body}"}
    except (httpx.HTTPError, OSError, TimeoutError) as e:
        return {"error": f"Request failed: {e}"}


def _chat_completion(base_url: str, api_key: str, model: str,
                     messages: list[dict], provider: str,
                     extra_headers: dict | None = None) -> dict:
    """Send a chat completion request to an OpenAI-compatible endpoint."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)

    body: dict = {"model": model, "messages": messages}
    data = _post_json(url, body, headers, provider_label=provider)

    if "error" in data and isinstance(data["error"], str):
        return data
    # OpenAI-style error objects
    if "error" in data and isinstance(data["error"], dict):
        return {"error": f"{provider}: {data['error'].get('message', str(data['error']))}"}

    choices = data.get("choices", [])
    if not choices:
        return {"error": "No choices returned"}

    msg = choices[0].get("message", {})
    usage = data.get("usage", {})
    return {
        "content": msg.get("content", ""),
        "role": msg.get("role", "assistant"),
        "model": data.get("model", model),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Connection routing
# ---------------------------------------------------------------------------

def gateway_chat(connection_name: str, messages: list[dict],
                 model_override: str,
                 connections: list[dict]) -> dict:
    """Route a chat request through the named gateway connection.

    Uses :mod:`modastack.model` for credential and model resolution.
    """
    from modastack.config import ConnectionEntry
    from modastack.model import resolve

    conn_dict = None
    for c in connections:
        if c["name"] == connection_name:
            conn_dict = c
            break

    if conn_dict is None:
        gw_conns = [c for c in connections if c.get("kind") == "gateway"]
        if len(gw_conns) == 1:
            conn_dict = gw_conns[0]
        elif not connection_name and gw_conns:
            conn_dict = gw_conns[0]
        else:
            return {"error": f"Connection '{connection_name}' not found. "
                    f"Available: {[c['name'] for c in connections]}"}

    if conn_dict.get("kind") != "gateway":
        return {"error": f"Connection '{conn_dict['name']}' is kind="
                f"{conn_dict.get('kind')}, not 'gateway'"}

    # Reconstruct a ConnectionEntry for the resolver.
    extra = {k: v for k, v in conn_dict.items()
             if k not in ("name", "kind", "provider", "api_key", "model")}
    entry = ConnectionEntry(
        name=conn_dict["name"],
        kind=conn_dict["kind"],
        provider=conn_dict.get("provider", ""),
        api_key=conn_dict.get("api_key", ""),
        model=conn_dict.get("model", ""),
        extra=extra,
    )

    spec = resolve(entry, requested_model=model_override)

    if not spec.api_key:
        return {"error": f"No api_key configured for connection "
                f"'{conn_dict['name']}' and no {spec.provider.upper()}_API_KEY "
                f"env var found"}

    if not spec.base_url:
        return {"error": f"No base_url for provider '{spec.provider}'"}

    extra_headers: dict[str, str] = {}
    if spec.extra.get("http_referer"):
        extra_headers["HTTP-Referer"] = spec.extra["http_referer"]
    if spec.extra.get("x_title"):
        extra_headers["X-Title"] = spec.extra["x_title"]

    return _chat_completion(
        base_url=spec.base_url,
        api_key=spec.api_key,
        model=spec.model,
        messages=messages,
        provider=spec.provider,
        extra_headers=extra_headers or None,
    )


# ---------------------------------------------------------------------------
# Stdio MCP server (JSON-RPC over stdin/stdout)
# ---------------------------------------------------------------------------

_TOOL_SCHEMA = {
    "name": "gateway_chat",
    "description": (
        "Send a chat completion request through a configured gateway "
        "connection. Routes to any OpenAI-compatible endpoint (OpenAI, "
        "OpenRouter, Together, etc.) based on the connection's provider."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "description": (
                    "Name of the gateway connection from agent.yaml. "
                    "Leave empty to use the default gateway connection."
                ),
            },
            "messages": {
                "type": "array",
                "description": (
                    "Chat messages in OpenAI format: "
                    '[{"role": "user", "content": "Hello"}]'
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["role", "content"],
                },
            },
            "model": {
                "type": "string",
                "description": (
                    "Model override. If empty, uses the connection's "
                    "configured model or the provider's default."
                ),
            },
        },
        "required": ["messages"],
    },
}


def _handle_jsonrpc(request: dict, connections: list[dict]) -> dict | None:
    """Handle a single JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "modastack-gateway",
                    "version": "1.0.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [_TOOL_SCHEMA]},
        }

    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name != "gateway_chat":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text",
                                 "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        result = gateway_chat(
            connection_name=arguments.get("connection", ""),
            messages=arguments.get("messages", []),
            model_override=arguments.get("model", ""),
            connections=connections,
        )

        is_error = "error" in result
        text = json.dumps(result, indent=2)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        }

    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def run_stdio_server(connections_json: str) -> None:
    """Run the MCP gateway server over stdio (JSON-RPC)."""
    connections = json.loads(connections_json)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = _handle_jsonrpc(request, connections)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m modastack.mcp.gateway_server '<connections_json>'",
              file=sys.stderr)
        sys.exit(1)
    run_stdio_server(sys.argv[1])
