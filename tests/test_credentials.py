"""Tests for Credentials management."""

from modastack.config import Credentials, CREDENTIALS_PATH


class TestCredentials:

    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.CREDENTIALS_PATH", tmp_path / "nope.yaml")
        creds = Credentials.load()
        assert creds.entries == {}

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        creds_path = tmp_path / "credentials.yaml"
        monkeypatch.setattr("modastack.config.CREDENTIALS_PATH", creds_path)

        creds = Credentials()
        creds.add("myproject", linear_api_key="lin_test_123")
        creds.save()

        loaded = Credentials.load()
        assert loaded.entries["myproject"]["linear_api_key"] == "lin_test_123"

    def test_get_named_entry(self):
        creds = Credentials(entries={"prod": {"key": "abc"}, "default": {"key": "fallback"}})
        assert creds.get("prod") == {"key": "abc"}

    def test_get_falls_back_to_default(self):
        creds = Credentials(entries={"default": {"key": "fallback"}})
        assert creds.get("unknown") == {"key": "fallback"}

    def test_get_returns_empty_when_no_match(self):
        creds = Credentials(entries={"prod": {"key": "abc"}})
        assert creds.get("missing") == {}

    def test_add_creates_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.CREDENTIALS_PATH", tmp_path / "c.yaml")
        creds = Credentials()
        creds.add("new", api_key="k1", token="t1")
        assert creds.entries["new"]["api_key"] == "k1"
        assert creds.entries["new"]["token"] == "t1"

    def test_add_skips_empty_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.CREDENTIALS_PATH", tmp_path / "c.yaml")
        creds = Credentials()
        creds.add("proj", api_key="k1", empty_val="")
        assert "empty_val" not in creds.entries["proj"]

    def test_add_merges_into_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.CREDENTIALS_PATH", tmp_path / "c.yaml")
        creds = Credentials(entries={"proj": {"key1": "v1"}})
        creds.add("proj", key2="v2")
        assert creds.entries["proj"]["key1"] == "v1"
        assert creds.entries["proj"]["key2"] == "v2"

    def test_list_names(self):
        creds = Credentials(entries={"a": {}, "b": {}, "c": {}})
        assert sorted(creds.list_names()) == ["a", "b", "c"]

    def test_list_names_empty(self):
        creds = Credentials()
        assert creds.list_names() == []
