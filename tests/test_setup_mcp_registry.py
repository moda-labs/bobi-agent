"""Tests for the hosted-MCP registry — the Connect cascade's third rung."""

from modastack.setup import mcp_registry


class TestLookup:
    def test_known_server_by_key(self):
        spec = mcp_registry.lookup("stripe")
        assert spec is not None
        assert spec.key == "stripe"
        assert spec.url.startswith("https://")

    def test_case_and_whitespace_insensitive(self):
        assert mcp_registry.lookup("  Stripe ").key == "stripe"

    def test_alias_resolves_to_canonical(self):
        # "hf" / "hugging face" both alias the huggingface server.
        assert mcp_registry.lookup("hf").key == "huggingface"
        assert mcp_registry.lookup("Hugging Face").key == "huggingface"

    def test_unknown_returns_none(self):
        assert mcp_registry.lookup("totally-made-up-service") is None
        assert mcp_registry.lookup("") is None

    def test_no_venn_bucket_names_in_registry(self):
        # Services Venn's curated buckets already cover resolve to Venn FIRST,
        # so the registry must not shadow them (notion, slack, jira, …).
        from modastack.venn import SERVICE_ALIASES
        venn_names = {n.lower() for names in SERVICE_ALIASES.values()
                      for n in names} | set(SERVICE_ALIASES)
        assert not (set(mcp_registry.REGISTRY) & venn_names)


class TestServerConfig:
    def test_static_key_server_emits_auth_header(self):
        spec = mcp_registry.lookup("stripe")
        cfg = spec.server_config()
        assert cfg["type"] == "http"
        assert cfg["url"] == spec.url
        # the key is referenced as ${VAR}, never a literal value
        assert cfg["headers"] == {"Authorization": "Bearer ${STRIPE_API_KEY}"}

    def test_oauth_or_public_server_has_no_headers(self):
        # DeepWiki is a public hosted MCP — no static key, so no headers block.
        spec = mcp_registry.lookup("deepwiki")
        assert spec.secret_var == ""
        cfg = spec.server_config()
        assert "headers" not in cfg
        assert cfg == {"type": "http", "url": spec.url}

    def test_no_config_carries_a_literal_secret(self):
        for spec in mcp_registry.REGISTRY.values():
            blob = str(spec.server_config())
            # only ${VAR} refs allowed in headers, never a raw token
            if spec.secret_var:
                assert "${" + spec.secret_var + "}" in blob
