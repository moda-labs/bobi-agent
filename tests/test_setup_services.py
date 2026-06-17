"""Tests for the Connect connector catalog and pure card-status logic."""

from modastack.setup import services
from modastack.setup.services import AuthMethod


class TestResolve:
    def test_native_by_key(self):
        assert services.resolve("github").kind == "native"
        assert services.resolve("slack").credential_var == "SLACK_BOT_TOKEN"

    def test_alias_resolves_to_canonical(self):
        # "gmail" is an alias of the coarse "email" venn bucket.
        assert services.resolve("gmail").key == "email"
        assert services.resolve("salesforce").key == "crm"

    def test_case_and_whitespace_insensitive(self):
        assert services.resolve("  GitHub ").key == "github"

    def test_in_venn_catalog_resolves_to_venn(self):
        # A name Venn actually supports (passed in the catalog) → venn connector.
        conn = services.resolve("zendesk", venn_catalog={"zendesk"})
        assert conn.kind == "venn"
        assert conn.key == "zendesk"
        assert conn.methods and conn.methods[0].action == "venn"

    def test_unknown_not_in_catalog_becomes_custom(self):
        # Neither native, a curated bucket, nor in Venn's catalog → custom:
        # modastack captures an API key and authors a tools guide for it.
        conn = services.resolve("posthog", venn_catalog=set())
        assert conn.kind == "custom"
        assert conn.key == "posthog"
        # captures a service-specific API key, no Venn action.
        sec = conn.methods[0].secrets[0]
        assert sec.var == "POSTHOG_API_KEY"
        assert conn.methods[0].action == ""
        assert conn.credential_var == "POSTHOG_API_KEY"


class TestConnectorModel:
    def test_credential_var_is_first_required_secret(self):
        assert services.CATALOG["github"].credential_var == "GITHUB_TOKEN"
        assert services.CATALOG["slack"].credential_var == "SLACK_BOT_TOKEN"
        assert services.CATALOG["linear"].credential_var == "LINEAR_API_KEY"

    def test_github_offers_token_and_app_methods(self):
        keys = {m.key for m in services.CATALOG["github"].methods}
        assert keys == {"token", "app"}

    def test_methods_carry_setup_steps(self):
        slack = services.CATALOG["slack"]
        token = next(m for m in slack.methods if m.key == "token")
        assert token.steps and any("api.slack.com" in s for s in token.steps)
        assert any(sec.var == "SLACK_BOT_TOKEN" for sec in token.secrets)

    def test_venn_connectors_share_the_venn_key(self):
        for key in ("email", "calendar", "crm"):
            conn = services.CATALOG[key]
            method = conn.methods[0]
            assert method.action == "venn"
            assert [s.var for s in method.secrets] == [services.VENN_KEY_VAR]


class TestCardStatus:
    def test_native_with_required_secret_present_is_connected(self):
        c = services.card(services.CATALOG["slack"],
                          present={"SLACK_BOT_TOKEN"})
        assert c["status"] == "connected"
        assert c["via"] == "token"

    def test_native_without_required_secret_is_missing(self):
        c = services.card(services.CATALOG["slack"], present=set())
        assert c["status"] == "missing"

    def test_github_missing_until_token_present(self):
        # Nothing captured → "connect": the App-install method can't be verified
        # locally and no token is set yet.
        c = services.card(services.CATALOG["github"], present=set())
        assert c["status"] == "missing"
        # Saving a token satisfies the token method → connected.
        c2 = services.card(services.CATALOG["github"], present={"GITHUB_TOKEN"})
        assert c2["status"] == "connected"

    def test_no_secret_method_is_never_auto_satisfied(self):
        m = AuthMethod(key="app", label="Install the App")  # no secrets
        assert services._method_satisfied(m, set(), venn_connected=None) is False

    def test_venn_connected_true(self):
        c = services.card(services.CATALOG["email"], venn_connected=True)
        assert c["status"] == "connected"
        assert c["via"] == "Venn OAuth"

    def test_venn_connected_false_is_missing(self):
        c = services.card(services.CATALOG["email"], venn_connected=False)
        assert c["status"] == "missing"

    def test_venn_unchecked_is_unknown(self):
        c = services.card(services.CATALOG["email"], venn_connected=None)
        assert c["status"] == "unknown"

    def test_card_is_serializable_shape(self):
        c = services.card(services.CATALOG["linear"], present={"LINEAR_API_KEY"})
        assert set(c) == {"key", "name", "kind", "summary", "scopes",
                          "methods", "via", "status"}
        assert isinstance(c["scopes"], list)
        m = c["methods"][0]
        assert set(m) == {"key", "label", "summary", "steps", "docs_url",
                          "action", "satisfied", "secrets"}
        s = m["secrets"][0]
        assert set(s) == {"var", "label", "placeholder", "help", "optional",
                          "present"}

    def test_secret_present_flag_reflects_present_set(self):
        c = services.card(services.CATALOG["linear"], present={"LINEAR_API_KEY"})
        secret = c["methods"][0]["secrets"][0]
        assert secret["present"] is True
        c2 = services.card(services.CATALOG["linear"], present=set())
        assert c2["methods"][0]["secrets"][0]["present"] is False


class TestCatalogCards:
    def test_includes_natives_and_venn(self):
        keys = {c["key"] for c in services.catalog_cards()}
        assert {"github", "slack", "linear", "email", "crm"} <= keys


class TestCardsForSpec:
    def test_dedupes_aliased_services(self, tmp_path):
        # "gmail" and "email" both resolve to the email connector → one card.
        cards = services.cards_for(
            [{"name": "gmail"}, {"name": "email"}], tmp_path)
        assert [c["key"] for c in cards] == ["email"]

    def test_native_status_reads_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        from modastack.setup.actions import write_env
        write_env(tmp_path, {"SLACK_BOT_TOKEN": "xoxb-something"})
        cards = services.cards_for(["slack"], tmp_path)
        assert cards[0]["status"] == "connected"

    def test_venn_status_uses_connected_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VENN_API_KEY", "k")
        connected = {"salesforce"}   # crm's alias
        cards = services.cards_for(["crm"], tmp_path, connected=connected)
        assert cards[0]["status"] == "connected"
        cards = services.cards_for(["crm"], tmp_path, connected=set())
        assert cards[0]["status"] == "missing"

    def test_venn_status_unknown_without_connected_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VENN_API_KEY", "k")
        cards = services.cards_for(["crm"], tmp_path, connected=None)
        assert cards[0]["status"] == "unknown"

    def test_accepts_plain_strings_and_dicts(self, tmp_path):
        cards = services.cards_for(["github", {"name": "linear"}], tmp_path)
        assert {c["key"] for c in cards} == {"github", "linear"}

    def test_catalog_param_classifies_venn_vs_custom(self, tmp_path):
        # With posthog in the passed catalog it's venn-backed; without, custom.
        v = services.cards_for([{"name": "posthog"}], tmp_path,
                               catalog={"posthog"})
        assert v[0]["kind"] == "venn"
        c = services.cards_for([{"name": "posthog"}], tmp_path)  # static seed
        assert c[0]["kind"] == "custom"


class TestLiveVennCatalog:
    def test_no_key_returns_static_seed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        assert services.live_venn_catalog(tmp_path) == services.VENN_CATALOG

    def test_unions_live_names_when_key_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VENN_API_KEY", "k")
        # the live source (CLI→REST) is stubbed; no network in the test
        monkeypatch.setattr(services, "_live_service_names",
                            lambda key: {"posthog", "stripe"})
        cat = services.live_venn_catalog(tmp_path)
        assert {"posthog", "stripe"} <= cat
        assert services.VENN_CATALOG <= cat

    def test_live_source_prefers_cli_then_rest(self, tmp_path, monkeypatch):
        from modastack.setup import venn_cli
        monkeypatch.setattr(venn_cli, "venn_binary", lambda: "/usr/bin/venn")
        monkeypatch.setattr(venn_cli, "list_service_names",
                            lambda key: {"from-cli"})
        assert services._live_service_names("k") == {"from-cli"}
        # CLI absent → REST fallback
        monkeypatch.setattr(venn_cli, "venn_binary", lambda: None)
        import modastack.venn as venn_mod
        monkeypatch.setattr(venn_mod, "list_available_services",
                            lambda key: {"from-rest"})
        assert services._live_service_names("k") == {"from-rest"}
