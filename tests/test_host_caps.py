"""Tests for the generalized `host:` capability model (#428 Stage 3).

Covers the HostCap model (parse / read / check / fix), collection across a
dependency set, the compose emission of `host:` into the frozen agent.yaml, and
that gstack's browser sysctl is now one instance of the same model.
"""

from __future__ import annotations

from pathlib import Path

from bobi import host_caps
from bobi.host_caps import HostCap, describe_for_deploy, parse_host_caps
from bobi.tool_library import Dependency


# --- HostCap model ----------------------------------------------------------


def test_sysctl_cap_spec_paths_and_fix():
    cap = HostCap.sysctl("kernel.apparmor_restrict_unprivileged_userns", "0",
                         owner="gstack")
    assert cap.spec == "kernel.apparmor_restrict_unprivileged_userns=0"
    assert cap.proc_path == Path(
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    assert cap.conf_path == Path(
        "/etc/sysctl.d/99-bobi-kernel-apparmor_restrict_unprivileged_userns.conf")
    assert cap.fix_command() == (
        "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0")


def test_cap_check_satisfied(tmp_path, monkeypatch):
    knob = tmp_path / "knob"
    knob.write_text("0\n")
    cap = HostCap.sysctl("net.example.knob", "0")
    monkeypatch.setattr(HostCap, "proc_path", property(lambda self: knob))
    assert cap.satisfied() is True
    res = cap.check()
    assert res.ok and "satisfied" in res.detail


def test_cap_check_violated_reports_fix(tmp_path, monkeypatch):
    knob = tmp_path / "knob"
    knob.write_text("1\n")
    cap = HostCap.sysctl("net.example.knob", "0", owner="gstack")
    monkeypatch.setattr(HostCap, "proc_path", property(lambda self: knob))
    assert cap.satisfied() is False
    res = cap.check()
    assert not res.ok
    assert "required 0" in res.detail
    assert "sudo sysctl -w net.example.knob=0" in res.hint
    assert "cannot set this" in res.hint  # in-container agent can't grant it


def test_cap_absent_knob_is_not_applicable(tmp_path, monkeypatch):
    cap = HostCap.sysctl("net.example.missing", "0")
    monkeypatch.setattr(HostCap, "proc_path",
                        property(lambda self: tmp_path / "does-not-exist"))
    assert cap.satisfied() is None
    assert cap.check().ok  # not present here → capability not required, passes


# --- parse / collect --------------------------------------------------------


def test_parse_host_caps_valid_and_malformed(caplog):
    caps = parse_host_caps([
        {"sysctl": "kernel.x=0"},
        {"sysctl": "no_equals_sign"},      # skipped (no key=value)
        {"device": "/dev/fuse"},           # skipped (unknown kind)
        "not-a-mapping",                   # skipped
    ], owner="d")
    assert len(caps) == 1
    assert caps[0].key == "kernel.x" and caps[0].value == "0" and caps[0].owner == "d"


def test_host_caps_for_deps_dedupes():
    d1 = Dependency(name="a", success="s", host=[{"sysctl": "kernel.x=0"}])
    d2 = Dependency(name="b", success="s", host=[{"sysctl": "kernel.x=0"},
                                                 {"sysctl": "kernel.y=1"}])
    caps = host_caps.host_caps_for_deps([d1, d2])
    assert {c.spec for c in caps} == {"kernel.x=0", "kernel.y=1"}


def test_describe_for_deploy():
    assert describe_for_deploy([]) == ""
    caps = [HostCap.sysctl("kernel.x", "0", owner="gstack")]
    text = describe_for_deploy(caps)
    assert "kernel.x=0" in text and "gstack" in text
    assert "sudo sysctl -w kernel.x=0" in text


# --- gstack's browser sysctl is one instance of the model -------------------


def test_browser_userns_is_a_hostcap():
    from bobi import browser
    assert browser.USERNS_CAP.key == browser.USERNS_SYSCTL
    assert browser.USERNS_CAP.value == "0"
    assert browser.FIX_COMMAND == browser.USERNS_CAP.fix_command()


# --- compose emission: host survives into the frozen agent.yaml -------------


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_host_field_emitted_through_compose(tmp_path):
    from bobi.compose import compose, resolve_chain
    from bobi.config import Config

    _write(tmp_path / "agents" / "gt" / "agent.yaml",
           "agent: gt\ntool_library:\n"
           "  - name: gstack\n"
           "    guide: g\n"
           "    success: s\n"
           "    host:\n"
           "      - sysctl: kernel.apparmor_restrict_unprivileged_userns=0\n")
    chain = resolve_chain(tmp_path / "agents" / "gt", tmp_path)
    dest = tmp_path / "composed"
    compose(chain, dest)
    cfg = Config._parse(dest / "agent.yaml")
    # host: is carried into the frozen config (runtime wiring for deploy/doctor)…
    assert cfg.host == [{"sysctl": "kernel.apparmor_restrict_unprivileged_userns=0"}]
    # …and tool_library: is consumed (the #428 acceptance bar).
    assert "tool_library" not in (dest / "agent.yaml").read_text()
    caps = parse_host_caps(cfg.host)
    assert caps[0].spec == "kernel.apparmor_restrict_unprivileged_userns=0"


def test_inline_host_wins_over_dependency_same_key(tmp_path):
    # A team's inline top-level host: for a knob wins over a dependency's entry for
    # the SAME sysctl key (leaf-wins), so doctor never gets two conflicting checks
    # for one knob.
    from bobi.compose import compose, resolve_chain
    from bobi.config import Config

    _write(tmp_path / "agents" / "gt" / "agent.yaml",
           "agent: gt\n"
           "host:\n  - sysctl: kernel.x=1\n"          # inline team value
           "tool_library:\n"
           "  - name: dep\n    guide: g\n    success: s\n"
           "    host:\n      - sysctl: kernel.x=0\n")  # dep's conflicting value
    chain = resolve_chain(tmp_path / "agents" / "gt", tmp_path)
    dest = tmp_path / "composed"
    compose(chain, dest)
    cfg = Config._parse(dest / "agent.yaml")
    specs = [e["sysctl"] for e in cfg.host]
    assert specs == ["kernel.x=1"]  # one entry, the inline team value wins


def test_host_caps_for_team_resolves_from_chain(tmp_path):
    _write(tmp_path / "agents" / "gt" / "agent.yaml",
           "agent: gt\ntool_library:\n"
           "  - name: gstack\n    guide: g\n    success: s\n"
           "    host:\n      - sysctl: kernel.x=0\n")
    caps = host_caps.host_caps_for_team(tmp_path / "agents" / "gt", tmp_path)
    assert [c.spec for c in caps] == ["kernel.x=0"]
