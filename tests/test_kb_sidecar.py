"""Unit tests for the embedding sidecar HTTP handler.

Tests the handler in-process — no subprocess launched.  The real
sentence-transformers model is replaced with a lightweight stub so
tests don't depend on HuggingFace Hub downloads (which are rate-limited
and can exceed the CI timeout).
"""

import json
from http.server import HTTPServer
from threading import Thread

import pytest

np = pytest.importorskip("numpy", reason="KB extra not installed")

from modastack.kb.sidecar import _make_handler, MODEL_NAME, EMBEDDING_DIM


class _StubModel:
    """Drop-in replacement for SentenceTransformer that returns deterministic
    embeddings without downloading anything."""

    def encode(self, texts, *, show_progress_bar=False):
        # Return a (len(texts), EMBEDDING_DIM) float32 ndarray — same shape
        # as the real model — seeded for reproducibility.
        rng = np.random.default_rng(42)
        return rng.standard_normal((len(texts), EMBEDDING_DIM)).astype(np.float32)


@pytest.fixture(scope="module")
def model():
    """Provide a lightweight stub model — no HuggingFace download needed."""
    return _StubModel()


@pytest.fixture(scope="module")
def server(model):
    """Start an in-process HTTP server on a random port."""
    handler_class = _make_handler(model)
    srv = HTTPServer(("127.0.0.1", 0), handler_class)
    port = srv.server_address[1]
    t = Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


def _get(port, path):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def _post(port, path, data):
    import urllib.request
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestHealth:
    def test_returns_ok(self, server):
        status, data = _get(server, "/health")
        assert status == 200
        assert data["status"] == "ok"

    def test_includes_model_info(self, server):
        _, data = _get(server, "/health")
        assert data["model"] == MODEL_NAME
        assert data["dim"] == EMBEDDING_DIM

    def test_includes_pid(self, server):
        _, data = _get(server, "/health")
        assert isinstance(data["pid"], int)


class TestEmbed:
    def test_single_text(self, server):
        status, data = _post(server, "/embed", {"texts": ["hello world"]})
        assert status == 200
        assert len(data["embeddings"]) == 1
        assert len(data["embeddings"][0]) == EMBEDDING_DIM

    def test_batch(self, server):
        texts = ["hello", "goodbye", "test"]
        status, data = _post(server, "/embed", {"texts": texts})
        assert status == 200
        assert len(data["embeddings"]) == 3
        for emb in data["embeddings"]:
            assert len(emb) == EMBEDDING_DIM

    def test_empty_list(self, server):
        status, data = _post(server, "/embed", {"texts": []})
        assert status == 200
        assert data["embeddings"] == []

    def test_embeddings_are_floats(self, server):
        status, data = _post(server, "/embed", {"texts": ["test"]})
        assert all(isinstance(v, float) for v in data["embeddings"][0])


class TestErrors:
    def test_unknown_route_get(self, server):
        import urllib.request
        import urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{server}/nope")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 404

    def test_unknown_route_post(self, server):
        status, data = _post(server, "/nope", {})
        assert status == 404

    def test_missing_texts_field(self, server):
        status, data = _post(server, "/embed", {"wrong": "field"})
        assert status == 400
        assert "texts" in data["error"]

    def test_texts_not_list(self, server):
        status, data = _post(server, "/embed", {"texts": "not a list"})
        assert status == 400
