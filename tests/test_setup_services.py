"""Tests for the Connect connector catalog and pure card-status logic."""

from modastack.setup import services
from modastack.setup.services import Connector


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

    def test_unknown_becomes_generic_venn(self):
        conn = services.resolve("some-obscure-saas")
        assert conn.kind == "venn"
        assert conn.key == "some-obscure-saas"


class TestCardStatus:
    def test_native_with_credential_present_is_connected(self):
        c = services.card(services.CATALOG["slack"], has_credential=True)
        assert c["status"] == "connected"
        assert c["via"] == "token"

    def test_native_without_credential_is_missing(self):
        c = services.card(services.CATALOG["slack"], has_credential=False)
        assert c["status"] == "missing"

    def test_native_with_no_credential_var_is_connected(self):
        # github needs nothing to collect (gh CLI / git remote).
        conn = Connector(key="github", name="GitHub", kind="native",
                         summary="", credential_var="")
        assert services.card(conn, has_credential=False)["status"] == "connected"

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
        c = services.card(services.CATALOG["linear"], has_credential=True)
        assert set(c) == {"key", "name", "kind", "summary", "scopes",
                          "credential_var", "instructions", "via", "status"}
        assert isinstance(c["scopes"], list)


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
