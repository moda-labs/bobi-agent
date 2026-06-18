"""Built-in MCP server for one-shot Codex execution via configured connections.

Provides a single tool `codex_exec(connection, prompt)` that shells out
to `codex exec "<prompt>"` and returns the output text.

Runs as a stdio MCP server, injected into agent sessions via the
existing mcp_servers plumbing in subagent.py when a connection with
kind=codex is declared in agent.yaml.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default timeout for codex exec (seconds)
# ---------------------------------------------------------------------------
CODEX_EXEC_TIMEOUT = 300


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

def _run_codex_exec(prompt: str, model: str = "",
                    timeout: float = CODEX_EXEC_TIMEOUT) -> dict:
    """Run ``codex exec`` as a subprocess, passing the prompt via stdin.

    When ``model`` is set, it is passed through as ``codex exec -m <model>``;
    otherwise the Codex CLI's own default model is used. Returns
    ``{"output": "..."}`` on success or ``{"error": "..."}`` on failure.
    Never raises — all errors are returned as dicts.
    """
    cmd = ["codex", "exec"]
    if model:
        cmd += ["-m", model]
    cmd.append("-")
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = result.stderr.strip()[:500] or f"exit code {result.returncode}"
            return {"error": f"codex exec failed: {detail}"}
        return {"output": result.stdout}
    except subprocess.TimeoutExpired:
        return {"error": f"codex exec timed out after {timeout}s"}
    except (OSError, FileNotFoundError) as e:
        return {"error": f"Failed to run codex: {e}"}


# ---------------------------------------------------------------------------
# Connection routing
# ---------------------------------------------------------------------------

def codex_exec(connection_name: str, prompt: str,
               connections: list[dict]) -> dict:
    """Route a codex exec request through the named connection.

    Args:
        connection_name: Name of the connection from agent.yaml
        prompt: The prompt to send to codex exec
        connections: List of connection dicts from config
    """
    conn = None
    for c in connections:
        if c["name"] == connection_name:
            conn = c
            break

    if conn is None:
        # If only one codex connection exists, use it
        codex_conns = [c for c in connections if c.get("kind") == "codex"]
        if len(codex_conns) == 1:
            conn = codex_conns[0]
        elif not connection_name and codex_conns:
            conn = codex_conns[0]
        else:
            return {"error": f"Connection '{connection_name}' not found. "
                    f"Available: {[c['name'] for c in connections]}"}

    if conn.get("kind") != "codex":
        return {"error": f"Connection '{conn['name']}' is kind={conn.get('kind')}, "
                f"not 'codex'"}

    return _run_codex_exec(prompt, model=conn.get("model", ""))


# ---------------------------------------------------------------------------
# Stdio MCP server (JSON-RPC over stdin/stdout)
# ---------------------------------------------------------------------------

_TOOL_SCHEMA = {
    "name": "codex_exec",
    "description": (
        "Run a one-shot Codex execution. Sends the prompt to `codex exec` "
        "and returns the output. Useful for adversarial code reviews, "
        "second-opinion analysis, or any task that benefits from a "
        "separate model's perspective."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "description": (
                    "Name of the codex connection from agent.yaml. "
                    "Leave empty to use the default codex connection."
                ),
            },
            "prompt": {
                "type": "string",
                "description": "The prompt to send to Codex for execution.",
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
                    "name": "modastack-codex",
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

        if tool_name != "codex_exec":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text",
                                 "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        result = codex_exec(
            connection_name=arguments.get("connection", ""),
            prompt=arguments.get("prompt", ""),
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
    """Run the MCP codex server over stdio (JSON-RPC).

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
        print("Usage: python -m modastack.mcp.codex_server '<connections_json>'",
              file=sys.stderr)
        sys.exit(1)
    run_stdio_server(sys.argv[1])
