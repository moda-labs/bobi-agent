"""Tests for Chromium sandbox detection, fix, and /browse health checks."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from modastack import browser
from modastack.browser import CheckResult
from modastack.cli import main


# --- sysctl reading -------------------------------------------------------


def test_read_userns_restriction_parses_value(tmp_path):
    knob = tmp_path / "apparmor_restrict_unprivileged_userns"
    knob.write_text("1\n")
    with patch.object(browser, "USERNS_SYSCTL_PATH", knob):
        assert browser.read_userns_restriction() == 1
        assert browser.userns_restricted() is True


def test_read_userns_restriction_unrestricted(tmp_path):
    knob = tmp_path / "knob"
    knob.write_text("0\n")
    with patch.object(browser, "USERNS_SYSCTL_PATH", knob):
        assert browser.read_userns_restriction() == 0
        assert browser.userns_restricted() is False


def test_read_userns_restriction_absent(tmp_path):
    with patch.object(browser, "USERNS_SYSCTL_PATH", tmp_path / "missing"):
        assert browser.read_userns_restriction() is None
        assert browser.userns_restricted() is False


# --- sandbox error detection ----------------------------------------------


def test_looks_like_sandbox_error_positive():
    assert browser.looks_like_sandbox_error("[...] No usable sandbox! Update")
    assert browser.looks_like_sandbox_error("Failed to move to new namespace")


def test_looks_like_sandbox_error_ignores_unrelated_noise():
    # The benign DBus/UPower warning Chromium prints must not trip detection.
    assert not browser.looks_like_sandbox_error(
        "Failed to call method: org.freedesktop.DBus ... UPower"
    )


# --- chromium discovery ---------------------------------------------------


def test_find_chromium_binary_prefers_newest(tmp_path):
    for rev in ("chromium-1208", "chromium-1223"):
        d = tmp_path / rev / "chrome-linux64"
        d.mkdir(parents=True)
        (d / "chrome").write_text("")
    with patch.object(browser, "PLAYWRIGHT_CACHE", tmp_path):
        found = browser.find_chromium_binary()
        assert found is not None
        assert "chromium-1223" in str(found)


def test_find_chromium_binary_none_when_missing(tmp_path):
    with patch.object(browser, "PLAYWRIGHT_CACHE", tmp_path / "nope"):
        assert browser.find_chromium_binary() is None


# --- chromium launch check ------------------------------------------------


def test_check_chromium_launch_detects_sandbox_error(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    proc = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="No usable sandbox!")
    with patch.object(browser, "find_chromium_binary", return_value=chrome), \
         patch("subprocess.run", return_value=proc):
        result = browser.check_chromium_launch()
    assert not result.ok
    assert result.sandbox_error is True
    assert result.hint


def test_check_chromium_launch_success(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    proc = subprocess.CompletedProcess([], returncode=0, stdout="<html></html>", stderr="")
    with patch.object(browser, "find_chromium_binary", return_value=chrome), \
         patch("subprocess.run", return_value=proc):
        result = browser.check_chromium_launch()
    assert result.ok
    assert not result.sandbox_error


def test_check_chromium_launch_missing_binary():
    with patch.object(browser, "find_chromium_binary", return_value=None):
        result = browser.check_chromium_launch()
    assert not result.ok
    assert not result.sandbox_error
    assert "playwright" in result.hint.lower()


# --- userns check ---------------------------------------------------------


def test_check_userns_sandbox_restricted(tmp_path):
    knob = tmp_path / "knob"
    knob.write_text("1")
    with patch.object(browser, "USERNS_SYSCTL_PATH", knob):
        result = browser.check_userns_sandbox()
    assert not result.ok
    assert result.sandbox_error


def test_check_userns_sandbox_ok(tmp_path):
    knob = tmp_path / "knob"
    knob.write_text("0")
    with patch.object(browser, "USERNS_SYSCTL_PATH", knob):
        result = browser.check_userns_sandbox()
    assert result.ok


# --- apply fix ------------------------------------------------------------


def test_apply_sandbox_fix_runtime_failure():
    fail = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="not permitted")
    with patch("subprocess.run", return_value=fail):
        ok, msg = browser.apply_sandbox_fix()
    assert ok is False
    assert "not permitted" in msg


def test_apply_sandbox_fix_runtime_and_persist():
    success = subprocess.CompletedProcess([], returncode=0, stdout="", stderr="")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return success

    with patch("subprocess.run", side_effect=fake_run):
        ok, msg = browser.apply_sandbox_fix(persist=True)
    assert ok is True
    # Applies at runtime, then persists via tee.
    assert any("sysctl" in c for c in calls)
    assert any("tee" in c for c in calls)
    assert "99-chromium-sandbox.conf" in msg


# --- system deps ----------------------------------------------------------


def test_check_system_deps_missing_lib(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    ldd = subprocess.CompletedProcess(
        [], returncode=0,
        stdout="\tlibnss3.so => not found\n\tlibc.so.6 => /lib/libc.so.6\n",
        stderr="",
    )
    with patch.object(browser, "find_chromium_binary", return_value=chrome), \
         patch("subprocess.run", return_value=ldd):
        result = browser.check_system_deps()
    assert not result.ok
    assert "libnss3.so" in result.detail


def test_check_system_deps_all_present(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    ldd = subprocess.CompletedProcess(
        [], returncode=0, stdout="\tlibc.so.6 => /lib/libc.so.6\n", stderr="",
    )
    with patch.object(browser, "find_chromium_binary", return_value=chrome), \
         patch("subprocess.run", return_value=ldd):
        result = browser.check_system_deps()
    assert result.ok


# --- doctor command -------------------------------------------------------


def test_doctor_all_ok():
    results = [CheckResult("a", ok=True, detail="x"), CheckResult("b", ok=True, detail="y")]
    with patch("modastack.doctor.run_doctor", return_value=results):
        result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "All checks passed" in result.output


def test_doctor_reports_failure_and_exits_nonzero():
    results = [
        CheckResult("Chromium launches", ok=False,
                    detail="blocked", hint="run the fix", sandbox_error=True),
    ]
    with patch("modastack.doctor.run_doctor", return_value=results), \
         patch("modastack.browser.run_doctor", return_value=[]), \
         patch("modastack.browser.is_linux", return_value=True):
        result = CliRunner().invoke(main, ["doctor", "--browser"])
    assert result.exit_code == 1
    assert "✗" in result.output
    assert "run the fix" in result.output
    assert "--fix" in result.output



def test_doctor_fix_applies_when_confirmed():
    results = [CheckResult("Chromium launches", ok=False, detail="blocked",
                           sandbox_error=True)]
    with patch("modastack.doctor.run_doctor", return_value=results), \
         patch("modastack.browser.run_doctor", return_value=[]), \
         patch("modastack.browser.is_linux", return_value=True), \
         patch("modastack.browser.apply_sandbox_fix", return_value=(True, "Applied.")) as apply_fix, \
         patch("modastack.browser.check_chromium_launch",
               return_value=CheckResult("Chromium launches", ok=True)):
        result = CliRunner().invoke(main, ["doctor", "--browser", "--fix"], input="y\n")
    apply_fix.assert_called_once()
    assert "Verified" in result.output
