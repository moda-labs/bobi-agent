"""Built-in MCP server for image generation via configured connections.

Provides a single tool `generate_image(connection, prompt, size)` that
routes to the appropriate provider (OpenAI, Google) based on the
connection config in agent.yaml.

Runs as a stdio MCP server, injected into agent sessions via the
existing mcp_servers plumbing in subagent.py.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider base URLs — override via environment if needed
# ---------------------------------------------------------------------------
OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
GOOGLE_IMAGEN_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}"
    ":generateImages"
)

# Default models when the connection config doesn't specify one
OPENAI_DEFAULT_MODEL = "gpt-image-1"
GOOGLE_DEFAULT_MODEL = "imagen-3.0-generate-002"


# ---------------------------------------------------------------------------
# Shared HTTP helper
# ---------------------------------------------------------------------------

def _post_json(url: str, body: dict, headers: dict,
               provider_label: str, timeout: int = 120) -> dict:
    """POST JSON to *url* and return the parsed response body.

    Returns ``{"error": "..."}`` on any HTTP or network failure so
    callers never need their own try/except around requests.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        return {"error": f"{provider_label} API error {e.code}: {err_body}"}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {"error": f"Request failed: {e}"}


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _openai_generate(api_key: str, model: str, prompt: str, size: str) -> dict:
    """Generate an image using OpenAI's images/generations endpoint."""
    data = _post_json(
        url=OPENAI_IMAGES_URL,
        body={
            "model": model or OPENAI_DEFAULT_MODEL,
            "prompt": prompt,
            "n": 1,
            "size": size or "1024x1024",
        },
        headers={"Authorization": f"Bearer {api_key}"},
        provider_label="OpenAI",
    )
    if "error" in data and isinstance(data["error"], str):
        return data
    images = data.get("data", [])
    if not images:
        return {"error": "No images returned"}
    result = images[0]
    return {
        "url": result.get("url", ""),
        "revised_prompt": result.get("revised_prompt", ""),
        "b64_json": result.get("b64_json", ""),
    }


def _google_generate(api_key: str, model: str, prompt: str, size: str) -> dict:
    """Generate an image using Google's Imagen API."""
    model = model or GOOGLE_DEFAULT_MODEL
    url = GOOGLE_IMAGEN_URL_TEMPLATE.format(model=model) + f"?key={api_key}"
    data = _post_json(
        url=url,
        body={"prompt": prompt, "config": {"numberOfImages": 1}},
        headers={},
        provider_label="Google",
    )
    if "error" in data and isinstance(data["error"], str):
        return data
    images = data.get("generatedImages", [])
    if not images:
        return {"error": "No images returned"}
    img = images[0].get("image", {})
    return {
        "b64_json": img.get("imageBytes", ""),
        "mime_type": img.get("mimeType", "image/png"),
    }


_PROVIDER_HANDLERS = {
    "openai": _openai_generate,
    "google": _google_generate,
    "gemini": _google_generate,
}


def generate_image(connection_name: str, prompt: str, size: str,
                   connections: list[dict]) -> dict:
    """Route an image generation request to the right provider.

    Args:
        connection_name: Name of the connection from agent.yaml
        prompt: Image generation prompt
        size: Image size (e.g. "1024x1024")
        connections: List of connection dicts from config
    """
    conn = None
    for c in connections:
        if c["name"] == connection_name:
            conn = c
            break

    if conn is None:
        # If only one image connection exists, use it
        image_conns = [c for c in connections if c.get("kind") == "image"]
        if len(image_conns) == 1:
            conn = image_conns[0]
        elif not connection_name and image_conns:
            conn = image_conns[0]
        else:
            return {"error": f"Connection '{connection_name}' not found. "
                    f"Available: {[c['name'] for c in connections]}"}

    if conn.get("kind") != "image":
        return {"error": f"Connection '{conn['name']}' is kind={conn.get('kind')}, "
                f"not 'image'"}

    provider = conn.get("provider", "").lower()
    handler = _PROVIDER_HANDLERS.get(provider)
    if not handler:
        return {"error": f"Unsupported image provider: '{provider}'. "
                f"Supported: {list(_PROVIDER_HANDLERS.keys())}"}

    api_key = conn.get("api_key", "")
    if not api_key:
        return {"error": f"No api_key configured for connection '{conn['name']}'"}

    model = conn.get("model", "")
    return handler(api_key, model, prompt, size)


# ---------------------------------------------------------------------------
# Stdio MCP server (JSON-RPC over stdin/stdout)
# ---------------------------------------------------------------------------

_TOOL_SCHEMA = {
    "name": "generate_image",
    "description": (
        "Generate an image using a configured model connection. "
        "Routes to OpenAI (DALL-E / GPT-Image) or Google (Imagen) "
        "based on the connection's provider."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "description": (
                    "Name of the image connection from agent.yaml. "
                    "Leave empty to use the default image connection."
                ),
            },
            "prompt": {
                "type": "string",
                "description": "The image generation prompt.",
            },
            "size": {
                "type": "string",
                "description": "Image size (e.g. '1024x1024', '1792x1024'). Default: 1024x1024.",
                "default": "1024x1024",
            },
        },
        "required": ["prompt"],
    },
}


def _handle_jsonrpc(request: dict, connections: list[dict]) -> dict:
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
                    "name": "modastack-image",
                    "version": "1.0.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None  # notification, no response

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

        if tool_name != "generate_image":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text",
                                 "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        result = generate_image(
            connection_name=arguments.get("connection", ""),
            prompt=arguments.get("prompt", ""),
            size=arguments.get("size", "1024x1024"),
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

    # Unknown method
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def run_stdio_server(connections_json: str) -> None:
    """Run the MCP image server over stdio (JSON-RPC).

    Args:
        connections_json: JSON string of connection configs from agent.yaml
    """
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
        print("Usage: python -m modastack.mcp.image_server '<connections_json>'",
              file=sys.stderr)
        sys.exit(1)
    run_stdio_server(sys.argv[1])
