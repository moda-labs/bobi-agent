import os
from pathlib import Path

from scripts.smoke_diy_install import build_child_env, sha256


def test_child_environment_drops_credentials_and_checkout_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/checkout")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    env = build_child_env(tmp_path, tmp_path / "venv" / "bin")
    assert env["BOBI_HOME"].startswith(str(tmp_path))
    assert "PYTHONPATH" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert str(tmp_path / "venv" / "bin") in env["PATH"]
    assert "." not in env["PATH"].split(os.pathsep)


def test_sha256_is_stable(tmp_path):
    path = Path(tmp_path) / "wheel.whl"
    path.write_bytes(b"wheel")
    assert sha256(path) == sha256(path)
