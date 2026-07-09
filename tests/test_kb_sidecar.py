"""Unit tests for the embedding sidecar HTTP handler.

Tests the handler in-process — no subprocess launched.  The real
fastembed model is replaced with a lightweight stub so tests don't
depend on HuggingFace Hub downloads (which are rate-limited and can
exceed the CI timeout).
"""

import json
from http.server import HTTPServer
from threading import Thread

import pytest

# numpy ships transitively with the optional [kb] extra (fastembed). Skip the
# whole module cleanly on a `.[dev]`-only install rather than erroring at
# collection, so `pytest tests/` works without the kb extra.
np = pytest.importorskip("numpy", reason="kb extra not installed (pip install '.[kb]')")

from bobi.kb.sidecar import _make_handler, MODEL_NAME, EMBEDDING_DIM


class _StubModel:
    """Drop-in replacement for fastembed TextEmbedding that returns
    deterministic embeddings without downloading anything."""

    def embed(self, texts):
        # Yield (EMBEDDING_DIM,) float32 ndarrays — same shape as the real
        # fastembed model — seeded for reproducibility.
        rng = np.random.default_rng(42)
        for _ in texts:
            yield rng.standard_normal(EMBEDDING_DIM).astype(np.float32)


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


class TestEmbeddingCompatibility:
    """Verify the sidecar produces embeddings compatible with the KB schema."""

    def test_embedding_dim_matches_store(self, server):
        """Embeddings must be EMBEDDING_DIM (384) floats — the sqlite-vec
        table is created with this dimension and rejects mismatches."""
        from bobi.kb.store import EMBEDDING_DIM as STORE_DIM
        status, data = _post(server, "/embed", {"texts": ["compatibility check"]})
        assert status == 200
        assert len(data["embeddings"][0]) == STORE_DIM

    def test_embedding_model_matches_store(self, server):
        """Health endpoint model name must match the constant stored in KB
        metadata so hybrid search uses the same model for query and doc."""
        from bobi.kb.store import EMBEDDING_MODEL as STORE_MODEL
        _, data = _get(server, "/health")
        assert data["model"] == STORE_MODEL

    def test_embeddings_are_deterministic(self, server):
        """Same input → same output, so re-indexing a doc produces identical
        vectors (important for dedup and search consistency)."""
        _, d1 = _post(server, "/embed", {"texts": ["determinism check"]})
        _, d2 = _post(server, "/embed", {"texts": ["determinism check"]})
        assert d1["embeddings"] == d2["embeddings"]

    def test_different_texts_produce_different_vectors(self, server):
        """Sanity check that the model is actually encoding, not returning
        a constant vector."""
        _, data = _post(server, "/embed", {"texts": ["cats", "quantum physics"]})
        assert data["embeddings"][0] != data["embeddings"][1]


class TestResolveCacheDir:
    """_resolve_cache_dir bridges fastembed's cache to env the C8 image controls.

    fastembed honors FASTEMBED_CACHE_PATH but ignores HF_HOME, so the container
    points first-use downloads at an explicit durable cache path.
    """

    def test_prefers_fastembed_cache_path(self, monkeypatch):
        from bobi.kb.sidecar import _resolve_cache_dir
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", "/img/fe-cache")
        monkeypatch.setenv("HF_HOME", "/img/hf")
        assert _resolve_cache_dir() == "/img/fe-cache"

    def test_falls_back_to_hf_home(self, monkeypatch):
        from bobi.kb.sidecar import _resolve_cache_dir
        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
        monkeypatch.setenv("HF_HOME", "/img/hf")
        assert _resolve_cache_dir() == "/img/hf/fastembed"

    def test_none_when_unset(self, monkeypatch):
        from bobi.kb.sidecar import _resolve_cache_dir
        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
        monkeypatch.delenv("HF_HOME", raising=False)
        assert _resolve_cache_dir() is None
