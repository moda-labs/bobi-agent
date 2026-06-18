"""Embedding sidecar — lightweight HTTP server that holds a fastembed/ONNX
model in memory and serves embedding requests.

Run as: python -m modastack.kb.sidecar --project-root <path>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"
_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


def _resolve_cache_dir() -> str | None:
    """Where fastembed should cache the ONNX model.

    fastembed honors ``FASTEMBED_CACHE_PATH`` but — unlike sentence-transformers
    /huggingface_hub — ignores ``HF_HOME``. The container image (C8) pre-seeds a
    model cache at build to avoid a first-embed download; honor either env so a
    pre-baked cache is actually used. Returns None to fall back to fastembed's
    default location.
    """
    explicit = os.environ.get("FASTEMBED_CACHE_PATH")
    if explicit:
        return explicit
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return str(Path(hf_home) / "fastembed")
    return None


def _make_handler(model):

    class EmbeddingHandler(BaseHTTPRequestHandler):

        def do_GET(self):
            if self.path == "/health":
                self._json_response(200, {
                    "status": "ok",
                    "model": MODEL_NAME,
                    "dim": EMBEDDING_DIM,
                    "pid": os.getpid(),
                })
            else:
                self._json_response(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/embed":
                self._handle_embed()
            else:
                self._json_response(404, {"error": "not found"})

        def _handle_embed(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, ValueError):
                self._json_response(400, {"error": "invalid JSON"})
                return

            texts = body.get("texts")
            if texts is None:
                self._json_response(400, {"error": "missing 'texts' field"})
                return

            if not isinstance(texts, list):
                self._json_response(400, {"error": "'texts' must be a list"})
                return

            if not texts:
                self._json_response(200, {
                    "embeddings": [],
                    "model": MODEL_NAME,
                    "dim": EMBEDDING_DIM,
                })
                return

            embeddings = [e.tolist() for e in model.embed(texts)]
            self._json_response(200, {
                "embeddings": embeddings,
                "model": MODEL_NAME,
                "dim": EMBEDDING_DIM,
            })

        def _json_response(self, status: int, data: dict):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return EmbeddingHandler


def main():
    parser = argparse.ArgumentParser(description="Embedding sidecar server")
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from modastack import paths
    state_dir = paths.state_dir(args.project_root)
    pid_file = state_dir / "embedding-sidecar.pid"
    port_file = state_dir / "embedding-sidecar.port"

    log.info("Loading model %s ...", MODEL_NAME)
    try:
        from fastembed import TextEmbedding
    except ImportError:
        log.error(
            "Knowledge base requires fastembed. "
            "Install with: pip install 'modastack[kb]'"
        )
        sys.exit(1)
    model = TextEmbedding(model_name=_FASTEMBED_MODEL,
                          cache_dir=_resolve_cache_dir())
    log.info("Model loaded (dim=%d)", EMBEDDING_DIM)

    handler_class = _make_handler(model)
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]

    pid_file.write_text(str(os.getpid()))
    port_file.write_text(str(port))

    def _shutdown(signum, frame):
        log.info("Received signal %d — shutting down", signum)
        pid_file.unlink(missing_ok=True)
        port_file.unlink(missing_ok=True)
        log.info("Sidecar stopped")
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Embedding sidecar listening on 127.0.0.1:%d (pid %d)", port, os.getpid())
    server.serve_forever()


if __name__ == "__main__":
    main()
