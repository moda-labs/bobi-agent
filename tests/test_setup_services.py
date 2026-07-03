"""Tests for the Connect connector catalog and pure card-status logic."""

import pytest

from bobi.setup import services
from bobi.setup.services import AuthMethod


class TestCanonicalServiceKey:
    @pytest.mark.parametrize("key,expected", [
        ("substack", "substack"),
        ("substack-mcp", "substack"),
        ("substack_mcp", "substack"),
        ("Substack-MCP", "substack"),
        ("mcp-substack", "substack"),
        ("substack-mcp-server", "substack"),
        ("github", "github"),
    ])
    def test_collapses_mcp_qualifiers(self, key, expected):
        assert services.canonical_service_key(key) == expected

    def test_bare_qualifier_falls_back(self):
        # Stripping must not erase a key that IS just the qualifier.
        assert services.canonical_service_key("mcp") == "mcp"


class TestResolve:
    def test_native_by_key(self):
        assert services.resolve("github").kind == "native"
        assert services.resolve("slack").credential_var == "SLACK_BOT_TOKEN"

    def test_concrete_venn_name_keeps_its_identity(self):
        # A concrete service keeps its own name/key (not collapsed to a bucket),
        # so two Gmail connections stay distinct and read as "Gmail" not "Email".
        gm = services.resolve("gmail")
        assert gm.kind == "venn" and gm.key == "gmail" and gm.name == "Gmail"
        sf = services.resolve("salesforce")
        assert sf.kind == "venn" and sf.key == "salesforce"
        assert sf.name == "Salesforce"

    def test_generic_bucket_term_resolves_to_the_bucket(self):
        # The literal generic term still maps to the broad bucket card.
        assert services.resolve("email").key == "email"
        assert services.resolve("crm").key == "crm"

    def test_titleizes_a_slug_name(self):
        assert services.resolve("google_calendar",
                                venn_catalog={"google_calendar"}).name == "Google Calendar"
        # a name Venn already cased is kept verbatim
        assert services.resolve("Work Gmail",
                                venn_catalog={"work gmail"}).name == "Work Gmail"

    def test_case_and_whitespace_insensitive(self):
        assert services.resolve("  GitHub ").key == "github"

    def test_in_venn_catalog_resolves_to_venn(self):
        # A name Venn actually supports (passed in the catalog) → venn connector.
        conn = services.resolve("zendesk", venn_catalog={"zendesk"})
        assert conn.kind == "venn"
        assert conn.key == "zendesk"
        assert conn.methods and conn.methods[0].action == "venn"

    def test_unknown_not_in_catalog_becomes_custom(self):
        # Neither native, a curated bucket, on Venn, nor a hosted MCP → custom:
        # bobi captures an API key and authors a tools guide for it.
        conn = services.resolve("posthog", venn_catalog=set())
        assert conn.kind == "custom"
        assert conn.key == "posthog"
        # captures a service-specific API key, no Venn action.
        sec = conn.methods[0].secrets[0]
        assert sec.var == "POSTHOG_API_KEY"
        assert conn.methods[0].action == ""
        assert conn.credential_var == "POSTHOG_API_KEY"

    def test_hosted_mcp_resolves_to_mcp(self):
        # In the MCP registry but not native/Venn → mcp: wired into mcp_servers.
        conn = services.resolve("stripe", venn_catalog=set())
        assert conn.kind == "mcp"
        assert conn.key == "stripe"
        m = conn.methods[0]
        assert m.action == "mcp"
        assert m.secrets[0].var == "STRIPE_API_KEY"   # static-key server

    def test_oauth_hosted_mcp_has_no_secret(self):
        conn = services.resolve("deepwiki", venn_catalog=set())
        assert conn.kind == "mcp"
        assert conn.methods[0].secrets == ()          # public/OAuth — no key

    def test_venn_wins_over_mcp_registry(self):
        # "one key, every service" comes first: if Venn covers a name, it's venn
        # even when the MCP registry also knows it.
        conn = services.resolve("stripe", venn_catalog={"stripe"})
        assert conn.kind == "venn"


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

    def test_static_key_mcp_connects_when_key_present(self):
        stripe = services.resolve("stripe", venn_catalog=set())
        assert services.card(stripe, present=set())["status"] == "missing"
        c = services.card(stripe, present={"STRIPE_API_KEY"})
        assert c["status"] == "connected"
        assert c["kind"] == "mcp"
        assert c["via"] == "hosted MCP"

    def test_public_mcp_is_satisfied_outright(self):
        # A public/OAuth hosted MCP has nothing to capture — it's wired in, so
        # it reads as connected rather than stranding the user on a CTA.
        deepwiki = services.resolve("deepwiki", venn_catalog=set())
        assert services.card(deepwiki, present=set())["status"] == "connected"

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
    def test_dedupes_identical_names(self, tmp_path):
        # The same connection named twice collapses to one card.
        cards = services.cards_for(
            [{"name": "gmail"}, {"name": "Gmail"}], tmp_path)
        assert [c["key"] for c in cards] == ["gmail"]
        assert cards[0]["name"] == "Gmail"

    def test_distinct_venn_names_are_distinct_rows(self, tmp_path):
        # Two Venn connections with different names each get their own row and
        # keep their own label (not merged into one "Email" bucket card).
        cat = {"gmail", "personal gmail"}
        cards = services.cards_for(
            [{"name": "gmail"}, {"name": "Personal Gmail"}], tmp_path, catalog=cat)
        assert [c["key"] for c in cards] == ["gmail", "personal gmail"]
        assert [c["name"] for c in cards] == ["Gmail", "Personal Gmail"]
        assert all(c["kind"] == "venn" for c in cards)

    def test_native_status_reads_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        from bobi.setup.actions import write_env
        write_env(tmp_path, {"SLACK_BOT_TOKEN": "xoxb-something"})
        cards = services.cards_for(["slack"], tmp_path)
        assert cards[0]["status"] == "connected"

    def test_pack_declared_vars_override_catalog_names(self, tmp_path):
        # A template/opened pack declares its own ${VAR} names (eng-team's
        # ${GH_TOKEN}); the capture card must speak them — the catalog var
        # is only the authoring default for packs setup writes itself.
        cards = services.cards_for(
            [{"name": "github", "credential_vars": {"token": "GH_TOKEN"}}],
            tmp_path)
        token_method = next(m for m in cards[0]["methods"]
                            if m["key"] == "token")
        assert [s["var"] for s in token_method["secrets"]] == ["GH_TOKEN"]

    def test_declared_vars_map_multi_secret_methods_by_key(self, tmp_path):
        cards = services.cards_for(
            [{"name": "slack",
              "credential_vars": {"bot_token": "MY_SLACK_BOT",
                                  "signing_secret": "MY_SLACK_SIGNING"}}],
            tmp_path)
        method = cards[0]["methods"][0]
        assert [s["var"] for s in method["secrets"]] == [
            "MY_SLACK_BOT", "MY_SLACK_SIGNING"]

    def test_declared_vars_flow_into_status(self, tmp_path, monkeypatch):
        # A credential saved under the DECLARED name flips the card to
        # connected; the catalog name alone does not satisfy it.
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from bobi.setup.actions import write_env
        spec = [{"name": "github", "credential_vars": {"token": "GH_TOKEN"}}]
        write_env(tmp_path, {"GITHUB_TOKEN": "ghp_x"})
        assert services.cards_for(spec, tmp_path)[0]["status"] != "connected"
        write_env(tmp_path, {"GH_TOKEN": "ghp_x"})
        assert services.cards_for(spec, tmp_path)[0]["status"] == "connected"

    def test_no_declaration_keeps_catalog_names(self, tmp_path):
        cards = services.cards_for([{"name": "github"}], tmp_path)
        token_method = next(m for m in cards[0]["methods"]
                            if m["key"] == "token")
        assert [s["var"] for s in token_method["secrets"]] == ["GITHUB_TOKEN"]

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
        from bobi.setup import venn_cli
        monkeypatch.setattr(venn_cli, "venn_binary", lambda: "/usr/bin/venn")
        monkeypatch.setattr(venn_cli, "list_service_names",
                            lambda key: {"from-cli"})
        assert services._live_service_names("k") == {"from-cli"}
        # CLI absent → REST fallback
        monkeypatch.setattr(venn_cli, "venn_binary", lambda: None)
        import bobi.venn as venn_mod
        monkeypatch.setattr(venn_mod, "list_available_services",
                            lambda key: {"from-rest"})
        assert services._live_service_names("k") == {"from-rest"}


class TestUserMcpCardStatus:
    """The Connect card for a user MCP reflects config + the last test verdict."""

    def _card(self, tmp_path, **cfg):
        cfg.setdefault("type", "stdio")
        cfg.setdefault("command", "uv")
        cfg.setdefault("label", "substack-mcp")
        return services.user_mcp_card("substack_mcp", cfg, tmp_path)

    def test_added_until_tested(self, tmp_path):
        c = self._card(tmp_path)
        assert c["status"] == "added"

    def test_successful_tool_call_marks_connected(self, tmp_path):
        c = self._card(tmp_path, last_test={"ok": True, "live_ok": True,
                                            "called": "substack_get_notes_feed"})
        assert c["status"] == "connected" and "substack_get_notes_feed" in c["note"]

    def test_failed_tool_call_marks_error(self, tmp_path):
        c = self._card(tmp_path, last_test={"ok": True, "live_ok": False,
                                            "error": "no cookie"})
        assert c["status"] == "error"

    def test_server_wont_start_marks_error(self, tmp_path):
        c = self._card(tmp_path, last_test={"ok": False, "error": "boom"})
        assert c["status"] == "error"

    def test_missing_env_stays_needs_auth_despite_stale_pass(self, tmp_path):
        # A connection still missing required config must not read "connected"
        # just because an earlier test passed.
        c = self._card(tmp_path, env_vars=["SUBSTACK_COOKIE"],
                       last_test={"ok": True, "live_ok": True, "called": "x"})
        assert c["status"] == "needs_auth"
