"""Self-learning ``script_cache`` monitor runner (#327).

A monitor whose only config is a natural-language ``prompt``. On the first run
the agent runtime discovers the right tool calls, executes the check, and emits
a deterministic script; subsequent runs execute the cached script via a sandboxed
subprocess at ~$0; on failure the runner falls back to the agent runtime to fix
and re-cache (self-healing).

This caches a script an LLM *wrote*, so a cron now runs machine-generated code
unattended with the manager's secret env. The security model is the load-bearing
part of this module — see ``docs/design/SCRIPT_CACHE.md`` §3. Defense in depth:

  1. Generation constraints (prompt-level) — ``_build_generation_description``.
  2. Static validation gate (the control) — ``validate_script``.
  3. Runtime sandbox (belt and suspenders) — ``run_sandboxed`` + the pre-run
     TOCTOU re-verify in ``_run_active``.
  4. Approval + post-hoc notification — ``_pin`` / ``_notify`` (§3.4 gate
     decision: PROCEED-BUT-NOTIFY — scripts auto-run, but every first run of a
     generated script emits a *real* post-hoc notification; the post-hoc
     observability is the safety trade for removing the pre-approval gate).

Trusted state (content ``sha256`` + the validated capability envelope + the
observability counters) lives in a per-monitor sidecar JSON next to the script
(``<name>.state.json``) rather than in ``monitor_state.json``: the scheduler
rewrites ``monitor_state.json`` wholesale from an in-memory dict every tick, so a
check runner writing the same file would be clobbered. A dedicated sidecar avoids
that race and keeps the trust record co-located with the script it protects.

Example monitor YAML::

    - name: unread-emails
      check: script_cache
      prompt: "Check my email for unread messages"
      id_field: id
      interval: 5m
      event: monitor/email.received

Config (per-monitor ``extra`` keys, overriding install-level ``script_cache:``
in agent.yaml):
    prompt              (required) natural-language description of the check
    id_field           dedup key field (default "id")
    approval           "auto" (default, per §3.4 gate) | "review" | "off"
    allow_http         allow raw curl GETs (default False)
    http_hosts         host allowlist when allow_http is on
    max_age            refresh a pinned script older than this (default: unset)
    on_persistent_failure  "degrade" (default) | "pause"
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modastack.monitors.schema import Condition
# Reuse the parse helpers verbatim — no behavior change to tool_poll.
from modastack.monitors.tool_checks import (
    TOOL_TIMEOUT,
    _items_to_conditions,
    _parse_items,
)

log = logging.getLogger(__name__)

# Per-tick agent budget (reused from #294) and cross-tick circuit breaker.
CHECK_MAX_TURNS = 8
SCRIPT_REGEN_MAX = 3          # consecutive failed regens before degrade+alert
_BACKOFF_CAP = 6 * 3600       # degraded-path backoff ceiling (~6h)
_RLIMIT_FSIZE = 10 * 1024 * 1024   # 10 MB — bounded, not zero (gh/venn write caches)
_RLIMIT_AS = 1024 * 1024 * 1024    # 1 GB address space
_RLIMIT_CPU = 30                   # CPU-seconds
_RLIMIT_NPROC = 64                 # fork-bomb bound

DEFAULT_APPROVAL = "auto"          # §3.4 gate decision (Zach): PROCEED-BUT-NOTIFY

# [impl] Only bash is accepted. python3 was dropped from the spec's two-language
# set: a `python3 -c '...'` call from bash is an arbitrary-code vector the binary
# scan can't see, and a sound python sandbox needs far more than an AST denylist
# (alias imports, Path.write_text, os.open, socket, importlib all bypass one).
# bash (flat simple commands) + jq covers the read-only monitor need with a single
# language we can validate soundly. Hardened because pre-approval was removed (§3.4)
# and no adversarial review was available.
VALID_SHEBANGS = {
    "#!/usr/bin/env bash": "bash",
}

# Every external command in command position must be on this allowlist. Raw
# curl/wget are deliberately absent — raw HTTP to an arbitrary host is the
# easiest exfil channel, so it requires explicit per-install opt-in.
#
# [impl] `sed` is deliberately NOT on the default allowlist even though the spec
# lists it: GNU sed's `e` modifier (`s/.../.../e`) shells out to /bin/sh — a full
# binary-allowlist bypass — and its `w` command writes files; a token scan can't
# reliably tell those from a benign substitution. jq/grep/cut/tr cover the
# read-only text-shaping need without an exec/write vector. Hardened out because
# the pre-approval gate was removed (§3.4) and no adversarial review was available.
# python3 is deliberately absent: a bash `python3 -c '<arbitrary code>'` (or
# `echo code | python3`) is a full arbitrary-code escape the per-command binary
# scan can't catch, so the interpreter is not reachable from a validated script.
SCRIPT_BINARY_ALLOWLIST = frozenset({
    "venn", "gh", "jq", "cat", "echo", "printf",
    "head", "tail", "sort", "uniq", "grep", "cut", "tr", "date",
})

# Shell builtins permitted in command position (no external process spawned).
_BUILTIN_ALLOWLIST = frozenset({"set", "true", "false", ":"})

# Binaries that mutate the filesystem / escalate / manage packages / mutate VCS.
# Reaching any of these (even though they're off the allowlist, this gives a
# precise rejection reason) is a hard reject.
_DENY_BINARIES = frozenset({
    "rm", "mv", "cp", "mkdir", "rmdir", "chmod", "chown", "ln", "install",
    "dd", "truncate", "tee", "shred", "mkfifo", "mknod",
    "sudo", "su", "doas", "eval", "exec", "source",
    "pip", "pip3", "npm", "npx", "apt", "apt-get", "brew", "uv", "cargo",
    "yum", "dnf", "gem", "go",
    "bash", "sh", "zsh", "dash", "ksh", "env", "xargs", "find",
    "curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "rsync", "telnet",
})


@dataclass
class CapabilityEnvelope:
    """The set of capabilities a validated script uses — pinned at approval time
    and compared on self-heal so a mechanical repair auto-promotes but a
    capability *change* (new binary / Venn tool / host) re-enters review."""

    binaries: set = field(default_factory=set)
    venn_tools: set = field(default_factory=set)   # "service:tool" pairs
    hosts: set = field(default_factory=set)         # curl GET hosts

    def to_json(self) -> dict:
        return {
            "binaries": sorted(self.binaries),
            "venn_tools": sorted(self.venn_tools),
            "hosts": sorted(self.hosts),
        }

    @classmethod
    def from_json(cls, raw: dict | None) -> "CapabilityEnvelope":
        raw = raw or {}
        return cls(
            binaries=set(raw.get("binaries", [])),
            venn_tools=set(raw.get("venn_tools", [])),
            hosts=set(raw.get("hosts", [])),
        )

    def covers(self, other: "CapabilityEnvelope") -> bool:
        """True when ``other`` introduces no new capability beyond this one."""
        return (
            other.binaries <= self.binaries
            and other.venn_tools <= self.venn_tools
            and other.hosts <= self.hosts
        )


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    interpreter: str = ""
    envelope: CapabilityEnvelope = field(default_factory=CapabilityEnvelope)


# ---------------------------------------------------------------------------
# Static validation gate (§3.2) — the control
# ---------------------------------------------------------------------------

# Operators that split a line into simple commands.
_SEPARATORS = {";", "|", "&&", "||", "\n"}


def _scan_unquoted(line: str) -> tuple[list[str], list[str]]:
    """Walk ``line`` tracking quote state; return (segments, forbidden_ops).

    Splits on unquoted command separators (``;`` ``|`` ``&&`` ``||``) into
    "simple command" segments, and records any forbidden shell operator that
    appears *outside* quotes: command substitution (`` ` `` / ``$(``), arithmetic
    or process substitution (``<(`` / ``>(``), backgrounding (a lone ``&``), and
    file-writing redirections (``>`` / ``>>`` whose target is not ``/dev/null``
    or an fd-dup). Quoting is honored so a literal ``grep '>'`` argument is not
    mistaken for a redirection — the whole reason a naive ``shlex`` token scan is
    unsafe here.
    """
    segments: list[str] = []
    forbidden: list[str] = []
    cur: list[str] = []
    i, n = 0, len(line)
    sq = dq = False
    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ""
        if sq:
            cur.append(ch)
            if ch == "'":
                sq = False
            i += 1
            continue
        if dq:
            # Bash performs command substitution INSIDE double quotes, so the
            # operator scan must run here too — only single quotes are a fully
            # literal context. (`echo "$(curl evil)"` would otherwise pass.)
            if ch == "\\" and nxt:
                cur.append(ch + nxt)
                i += 2
                continue
            if ch == "`":
                forbidden.append("command substitution (`) inside double quotes")
                i += 1
                continue
            if ch == "$" and nxt == "(":
                forbidden.append("command substitution $( inside double quotes")
                i += 2
                continue
            cur.append(ch)
            if ch == '"':
                dq = False
            i += 1
            continue
        # not in a quote
        if ch == "'":
            sq = True
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            dq = True
            cur.append(ch)
            i += 1
            continue
        if ch == "\\":
            cur.append(ch + nxt)
            i += 2
            continue
        if ch == "`":
            forbidden.append("command substitution (`)")
            i += 1
            continue
        if ch == "$" and nxt == "(":
            forbidden.append("command/arithmetic substitution $(")
            i += 2
            continue
        if ch in "<>" and nxt == "(":
            forbidden.append("process substitution <( / >(")
            i += 2
            continue
        # redirections
        if ch == ">":
            op = ">>" if nxt == ">" else ">"
            j = i + len(op)
            # `>&N` / `2>&1` is an fd-dup, not a file write — allowed.
            if j < n and line[j] == "&":
                i = j + 1
                continue
            # capture the redirect target token
            while j < n and line[j] == " ":
                j += 1
            k = j
            while k < n and line[k] not in " \t;|&<>":
                k += 1
            target = line[j:k]
            if target != "/dev/null":
                forbidden.append(f"output redirection ({op} {target or '?'})")
            i = k
            continue
        if ch == "<":
            # input redirection / here-string / here-doc — disallow (here-strings
            # and here-docs are exactly the constructs we refuse to reason about)
            forbidden.append("input redirection / here-doc (<)")
            i += 1
            continue
        if ch == "&":
            if nxt == "&":
                # logical AND — a separator
                if cur:
                    segments.append("".join(cur))
                    cur = []
                i += 2
                continue
            # lone & — backgrounding
            forbidden.append("backgrounding (&)")
            i += 1
            continue
        if ch == "|":
            if nxt == "|":
                if cur:
                    segments.append("".join(cur))
                    cur = []
                i += 2
                continue
            # pipe — separator
            if cur:
                segments.append("".join(cur))
                cur = []
            i += 1
            continue
        if ch == ";":
            if cur:
                segments.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if sq or dq:
        forbidden.append("unbalanced quote")
    if cur:
        segments.append("".join(cur))
    return segments, forbidden


def _binary_of(segment: str) -> tuple[str | None, list[str]]:
    """Return (binary, args) for a simple command, stripping leading
    ``VAR=value`` env-assignment prefixes. (None, []) for an empty segment or one
    that can't be tokenized."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None, []
    idx = 0
    while idx < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return None, []
    return tokens[idx], tokens[idx + 1:]


def _validate_curl(args: list[str], allow_http: bool, http_hosts) -> tuple[str, str]:
    """Validate a curl command. Returns (reason, host) — reason='' means ok."""
    if not allow_http:
        return "raw curl/wget is off by default (set script_cache.allow_http)", ""
    # Reject any flag that lets curl carry a body / method / upload (exfil), write
    # a file to disk (-o/-O), or pull options from a file that could re-introduce
    # those (-K). Plain GETs only.
    write_flags = {"-X", "--request", "-d", "--data", "--data-raw", "--data-binary",
                   "--data-urlencode", "-T", "--upload-file", "-F", "--form",
                   "-o", "--output", "-O", "--remote-name", "--remote-name-all",
                   "-K", "--config", "--create-dirs"}
    url = ""
    for a in args:
        if a in write_flags or any(a.startswith(f + "=") for f in write_flags):
            # -X GET is technically read-shaped, but any explicit method/body/
            # output flag is rejected — keep the rule simple and total.
            return f"curl write-shaped flag not allowed: {a}", ""
        # short flags can be glued (e.g. -oFILE, -XPOST); catch those too
        if len(a) > 2 and a[0] == "-" and a[1] != "-" and a[1] in ("o", "O", "X", "d", "T", "F", "K"):
            return f"curl write-shaped flag not allowed: {a}", ""
        if a.startswith("http://") or a.startswith("https://"):
            url = a
    if not url:
        return "curl with no literal URL", ""
    if "$" in url or "`" in url:
        return "curl URL must be a literal (no variable/command substitution)", ""
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    if host not in set(http_hosts or ()):
        return f"curl host not on allowlist: {host}", host
    return "", host


def _validate_bash(content: str, allow_http: bool, http_hosts) -> ValidationResult:
    env = CapabilityEnvelope(binaries=set())
    lines = content.splitlines()
    # First non-empty line is the shebang (already checked by caller).
    saw_pipefail = False
    for raw in lines[1:]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        segments, forbidden = _scan_unquoted(line)
        if forbidden:
            return ValidationResult(False, f"forbidden construct: {forbidden[0]}", "bash")
        for seg in segments:
            binary, args = _binary_of(seg)
            if binary is None:
                continue
            if binary == "set":
                if "pipefail" in args:
                    saw_pipefail = True
                continue
            if binary in _BUILTIN_ALLOWLIST:
                continue
            if binary in _DENY_BINARIES and binary not in ("curl",):
                return ValidationResult(False, f"denied binary: {binary}", "bash")
            if binary == "curl":
                reason, host = _validate_curl(args, allow_http, http_hosts)
                if reason:
                    return ValidationResult(False, reason, "bash")
                env.binaries.add("curl")
                if host:
                    env.hosts.add(host)
                continue
            if binary not in SCRIPT_BINARY_ALLOWLIST:
                return ValidationResult(False, f"binary not on allowlist: {binary}", "bash")
            # per-binary write-shape checks
            if binary == "sort" and any(
                    a in ("-o", "--output") or a.startswith("--output=")
                    or a.startswith("-o") and len(a) > 2 for a in args):
                return ValidationResult(False, "sort -o/--output (file write) not allowed", "bash")
            if binary == "venn":
                if any(a == "--confirm" or a.startswith("--confirm=") for a in args):
                    return ValidationResult(False, "venn --confirm (write op) forbidden", "bash")
                svc, tool = _venn_service_tool(args)
                if svc or tool:
                    env.venn_tools.add(f"{svc}:{tool}")
            if binary == "gh":
                bad = _gh_mutation(args)
                if bad:
                    return ValidationResult(False, f"gh mutation not allowed: {bad}", "bash")
            env.binaries.add(binary)
    if not saw_pipefail:
        return ValidationResult(False, "bash script must use 'set -euo pipefail'", "bash")
    return ValidationResult(True, "", "bash", env)


def _venn_service_tool(args: list[str]) -> tuple[str, str]:
    svc = tool = ""
    for i, a in enumerate(args):
        if a in ("-s", "--server") and i + 1 < len(args):
            svc = args[i + 1]
        if a in ("-t", "--tool") and i + 1 < len(args):
            tool = args[i + 1]
    return svc, tool


# gh subcommands that mutate remote state.
_GH_MUTATION_VERBS = {"create", "edit", "close", "merge", "delete", "comment",
                      "review", "ready", "reopen", "lock", "unlock", "rerun",
                      "approve", "transfer", "rename", "add", "remove", "set",
                      "push", "sync", "clone", "fork", "checkout", "download",
                      "develop", "restore", "disable", "enable", "cancel"}


def _gh_mutation(args: list[str]) -> str:
    """Return the offending verb/flag if a gh invocation mutates state, else ''.

    gh's read verbs (view/list/status/checks) are fine; anything that writes is
    rejected. For ``gh api`` we must catch every write shape:
      - an explicit non-GET/HEAD method in any form: ``-X POST``, ``--method POST``,
        ``--method=POST``, ``-XPOST``;
      - gh auto-promotes a request to POST whenever a field flag is present
        (``-f`` / ``-F`` / ``--field`` / ``--raw-field`` / ``--input``), even with
        no explicit method — so any of those on an ``api`` call is a write."""
    positionals = [a for a in args if not a.startswith("-")]
    for verb in positionals:
        if verb in _GH_MUTATION_VERBS:
            return verb
    if "api" in positionals:
        field_flags = {"-f", "-F", "--field", "--raw-field", "--input"}
        for i, a in enumerate(args):
            # explicit method, any spelling
            if a in ("-X", "--method") and i + 1 < len(args):
                if args[i + 1].upper() not in ("GET", "HEAD"):
                    return f"api method {args[i + 1]}"
            if a.startswith("--method="):
                if a.split("=", 1)[1].upper() not in ("GET", "HEAD"):
                    return f"api {a}"
            if a.startswith("-X") and len(a) > 2:
                if a[2:].upper() not in ("GET", "HEAD"):
                    return f"api {a}"
            # field flags imply POST (gh auto-promotes)
            if a in field_flags or any(a.startswith(f + "=") for f in field_flags):
                return f"api write-field {a}"
    return ""


def validate_script(content: str, *, allow_http: bool = False,
                    http_hosts=()) -> ValidationResult:
    """Static validation gate (§3.2). A script must clear this before it is ever
    pinned or run unattended. Denylist-backstopped allowlist: unknown binary →
    reject; known binary used in a write-shaped way → reject; an unparseable or
    construct-heavy script → reject (we validate only flat simple-command bash —
    a script we can't parse into simple commands is rejected, not waved through)."""
    if not content or not content.strip():
        return ValidationResult(False, "empty script")
    first = content.splitlines()[0].strip()
    interp = VALID_SHEBANGS.get(first)
    if interp is None:
        return ValidationResult(False, f"disallowed/missing shebang: {first!r}")
    return _validate_bash(content, allow_http, http_hosts)


# ---------------------------------------------------------------------------
# Runtime sandbox (§3.3)
# ---------------------------------------------------------------------------

def _rlimit_preexec():  # pragma: no cover - exercised in a child process
    """preexec_fn applying RLIMIT_* in the forked child before exec."""
    try:
        import resource
    except ImportError:
        return
    def _set(res, soft):
        try:
            hard = resource.getrlimit(res)[1]
            cap = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
            resource.setrlimit(res, (cap, hard))
        except (ValueError, OSError):
            pass
    _set(resource.RLIMIT_FSIZE, _RLIMIT_FSIZE)
    _set(resource.RLIMIT_AS, _RLIMIT_AS)
    _set(resource.RLIMIT_CPU, _RLIMIT_CPU)
    _set(resource.RLIMIT_CORE, 0)
    if hasattr(resource, "RLIMIT_NPROC"):
        _set(resource.RLIMIT_NPROC, _RLIMIT_NPROC)


def run_sandboxed(script_content: str, env: dict, timeout: int, *, name: str = "script"):
    """Run script *bytes* in a disposable scratch sandbox (§3.3).

    Takes the script **content** (not a path) and writes it into a fresh
    ``mkdtemp`` it owns, then executes that copy. This is deliberate: the caller
    verifies the bytes (sha256 + re-validate) and hands those exact bytes here, so
    the verified bytes are the executed bytes — closing the verify→exec TOCTOU
    window (a path-based exec would re-open a file an attacker could swap, or
    follow a symlink, between verification and exec).

    The scratch is the CWD and HOME/TMPDIR/XDG_* all point *into* it, so tools
    that need a cache work but nothing relative escapes to the repo or the real
    $HOME; a bounded ``RLIMIT_FSIZE`` (not zero — gh/venn legitimately write
    caches) plus ``RLIMIT_AS``/``CPU``/``NPROC``/``CORE`` bound resource abuse;
    the whole scratch tree is deleted after the run. Returns a CompletedProcess,
    or None on timeout / OS error (treated as a failed run by the caller)."""
    scratch = Path(tempfile.mkdtemp(prefix="msc-"))
    try:
        runner = scratch / f".{_safe_name(name)}.run.sh"
        runner.write_text(script_content)
        runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
        senv = dict(env)
        senv["HOME"] = str(scratch)
        senv["TMPDIR"] = str(scratch)
        for var in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                    "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
            senv[var] = str(scratch)
        kwargs = dict(capture_output=True, text=True, timeout=timeout,
                      env=senv, cwd=str(scratch))
        if os.name == "posix":
            kwargs["preexec_fn"] = _rlimit_preexec
        return subprocess.run([str(runner)], **kwargs)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("script_cache sandbox run failed: %s", e)
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Script + trusted-state paths
# ---------------------------------------------------------------------------

def _scripts_dir() -> Path:
    from modastack import paths
    d = paths.state_dir() / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(monitor_name: str) -> str:
    return monitor_name.replace("/", "_").replace("..", "_")


def _active_path(name: str) -> Path:
    return _scripts_dir() / f"{_safe_name(name)}.sc.sh"


def _pending_path(name: str) -> Path:
    d = _scripts_dir() / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe_name(name)}.sh"


def _state_path(name: str) -> Path:
    return _scripts_dir() / f"{_safe_name(name)}.state.json"


def _load_trusted_state(name: str) -> dict:
    p = _state_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()) or {}
    except (json.JSONDecodeError, OSError):
        log.warning("Corrupt script_cache state at %s — resetting", p)
        return {}


def _save_trusted_state(name: str, state: dict) -> None:
    """Persist the trusted-state sidecar atomically (tmp write + os.replace), so a
    crash or fleet-churn kill mid-write can't truncate it into corrupt JSON that
    _load_trusted_state would discard (dropping the pinned sha256 + envelope =
    losing the security baseline)."""
    try:
        p = _state_path(name)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, p)
    except OSError as e:
        log.warning("script_cache %s: couldn't persist trusted state: %s", name, e)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fingerprint(prompt: str, id_field: str, extra: dict) -> str:
    """Identity of the monitor config — a change invalidates the cached script."""
    relevant = {k: extra[k] for k in sorted(extra)
                if k not in ("approval", "max_age", "on_persistent_failure")}
    blob = json.dumps({"prompt": prompt, "id_field": id_field, "extra": relevant},
                      sort_keys=True)
    return _sha256(blob)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def _install_policy() -> dict:
    """Install-wide ``script_cache:`` block from agent.yaml, if any."""
    try:
        from modastack import paths
        import yaml
        root = paths.bound_root() or paths.modastack_root()
        cfg = paths.modastack_dir(root) / "agent.yaml"
        if not cfg.is_file():
            return {}
        raw = yaml.safe_load(cfg.read_text()) or {}
        sc = raw.get("script_cache", {})
        return sc if isinstance(sc, dict) else {}
    except Exception:
        return {}


def _policy(monitor) -> dict:
    """Resolved policy: install defaults overlaid by per-monitor extra."""
    base = {
        "approval": DEFAULT_APPROVAL,
        "allow_http": False,
        "http_hosts": [],
        "max_age": None,
        "on_persistent_failure": "degrade",
    }
    base.update(_install_policy())
    extra = monitor.extra or {}
    for k in base:
        if k in extra:
            base[k] = extra[k]
    return base


# ---------------------------------------------------------------------------
# Generation (agent runtime) — injectable for tests
# ---------------------------------------------------------------------------

@dataclass
class GenResult:
    success: bool
    items: list | None = None
    script: str | None = None
    cost_usd: float = 0.0
    error: str = ""


def _build_generation_description(prompt: str, policy: dict) -> str:
    """The specialized generation prompt (§3.1). The agent observes (read-only),
    returns this tick's items, AND proposes a cacheable script as *text* in its
    verdict (we write the file ourselves — the agent never writes it)."""
    allowed = ", ".join(sorted(SCRIPT_BINARY_ALLOWLIST))
    http = ("Raw curl GETs are permitted to these hosts only: "
            + ", ".join(policy.get("http_hosts") or [])
            ) if policy.get("allow_http") else (
            "Raw curl/wget are NOT permitted — use venn or gh.")
    return (
        f"You are generating a cacheable, read-only monitoring script.\n\n"
        f"CHECK (natural language):\n{prompt}\n\n"
        f"Do two things:\n"
        f"1. Perform the check NOW using read-only commands, and collect the "
        f"current items it finds (a JSON list of objects).\n"
        f"2. Propose a deterministic shell script that reproduces step 1 on "
        f"future runs and prints the same JSON list to stdout.\n\n"
        f"The script MUST:\n"
        f"- start with '#!/usr/bin/env bash' then 'set -euo pipefail' (bash only);\n"
        f"- use ONLY these binaries: {allowed};\n"
        f"- be read-only and side-effect free: no file writes/redirections, no "
        f"rm/mv/cp/mkdir/chmod, no sudo, no eval/exec/source, no command or "
        f"process substitution ANYWHERE (not even inside double quotes), no "
        f"functions, no backgrounding, no package managers, no git mutation;\n"
        f"- use 'venn tools execute' WITHOUT '--confirm' (read-only Venn only);\n"
        f"- use only read-shaped 'gh' (view/list/api GET — never create/edit/"
        f"comment/merge or 'gh api' with -f/-X);\n"
        f"- take no arguments and print ONLY the JSON list to stdout. {http}\n\n"
        f"It will be statically rejected and never run if it violates the above.\n\n"
        f"Output your verdict as a SINGLE final line of JSON in this form:\n"
        f'{{"finding": true, "details": {{"items": [ ... ], '
        f'"script": "#!/usr/bin/env bash\\nset -euo pipefail\\n..."}}}}\n'
        f"Use finding=false only when the check itself found nothing to report; "
        f"still include items (an empty list) and the script."
    )


def generate_candidate(monitor, cwd: str | None, policy: dict) -> GenResult:
    """Run the agent runtime to produce (this tick's items, a candidate script).

    Reuses ``run_check_blocking`` (the same supervised, read-only, budget-capped
    agent loop as every description-only check) so generation introduces no new
    agent trust surface (§2). Injectable: tests monkeypatch this symbol."""
    from modastack.subagent import run_check_blocking

    prompt = monitor.extra.get("prompt", "")
    desc = _build_generation_description(prompt, policy)
    res = run_check_blocking(desc, cwd or ".", name=f"scriptgen-{_safe_name(monitor.name)}")
    if not res.success:
        return GenResult(False, error=res.error or "generation failed",
                         cost_usd=res.total_cost_usd)
    details = res.details or {}
    items = details.get("items")
    script = details.get("script")
    if not isinstance(items, list):
        items = None
    return GenResult(True, items=items, script=script, cost_usd=res.total_cost_usd)


# ---------------------------------------------------------------------------
# Notification (§3.4 — PROCEED-BUT-NOTIFY). Real wire path, not a stub.
# ---------------------------------------------------------------------------

def publish(event: str, data: dict) -> bool:
    """Publish through the same real event wire every monitor finding uses.

    The manager subscribes and relays to the human's Slack; the event also lands
    in events.jsonl (the observable record). Injectable for tests."""
    try:
        from modastack.events.publish import post_event
        return post_event(event, data)
    except Exception as e:  # never let a notify failure break a tick
        log.warning("script_cache publish %s failed: %s", event, e)
        return False


def _notify(event: str, monitor, payload: dict, state: dict) -> None:
    """Emit a real post-hoc notification AND append an observable record.

    Two channels, both real: (1) publish the event on the wire (→ manager →
    Slack), (2) append to the sidecar state's ``notifications`` log + a structured
    log line. This is the load-bearing safety trade for auto-running generated
    scripts without a pre-approval gate, so it must never silently no-op: a failed
    publish is recorded, not swallowed."""
    data = {"monitor": monitor.name, **payload}
    ok = publish(event, data)
    record = {"at": _now().isoformat(), "event": event, "published": ok, **payload}
    state.setdefault("notifications", []).append(record)
    # keep the audit log bounded
    state["notifications"] = state["notifications"][-50:]
    level = log.info if ok else log.warning
    level("script_cache %s: %s %s (published=%s)",
          monitor.name, event, payload.get("mode", payload.get("reason", "")), ok)
    # Best-effort direct Slack on top of the wire path, when configured.
    _slack_notify(monitor, event, payload)


def _slack_notify(monitor, event: str, payload: dict) -> None:
    """Best-effort direct Slack message when a bot token + notify channel are
    configured (script_cache.notify_channel). Additive to the event publish —
    never raises."""
    try:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        channel = (monitor.extra or {}).get("notify_channel") or _install_policy().get("notify_channel")
        if not token or not channel:
            return
        from modastack.slack import post_slack_message
        text = (f":mag: script_cache `{monitor.name}` — {event.split('/')[-1]}\n"
                f"> {payload.get('summary') or payload.get('reason') or payload.get('mode', '')}")
        post_slack_message(token, channel, text)
    except Exception as e:
        log.debug("script_cache slack notify skipped: %s", e)


# ---------------------------------------------------------------------------
# Pin / approval lifecycle (§3.4, §6)
# ---------------------------------------------------------------------------

def _write_header(script: str, monitor, fp: str, model: str = "") -> str:
    """Prepend a provenance header AFTER the shebang line (humans read it; the
    trusted sha/envelope live in sidecar state, not here — the header is inside
    the executed file and therefore mutable)."""
    lines = script.splitlines()
    shebang = lines[0] if lines else "#!/usr/bin/env bash"
    body = "\n".join(lines[1:])
    header = (
        f"# modastack script_cache — monitor: {monitor.name}\n"
        f"# generated: {_now().isoformat()}  fingerprint: {fp[:16]}\n"
        f"# AUTO-GENERATED by the agent runtime; validated read-only. Do not edit.\n"
    )
    return f"{shebang}\n{header}{body}\n"


def _smoke_ok(content: str, env: dict, name: str) -> bool:
    """Run a candidate once in the sandbox and require parseable list output —
    rejects a script that exits 0 but prints garbage (§6.1)."""
    cp = run_sandboxed(content, env, TOOL_TIMEOUT, name=f"smoke-{name}")
    if cp is None or cp.returncode != 0:
        return False
    return _parse_items(cp.stdout, name) is not None


def _pin(name: str, content: str, monitor, fp: str, envelope: CapabilityEnvelope,
         state: dict) -> None:
    """Atomically activate a validated+smoked script and record trusted state."""
    headed = _write_header(content, monitor, fp)
    tmp = _scripts_dir() / f".tmp-{_safe_name(name)}.sh"
    tmp.write_text(headed)
    tmp.chmod(tmp.stat().st_mode | stat.S_IEXEC)
    active = _active_path(name)
    os.replace(tmp, active)
    state["fingerprint"] = fp
    state["sha256"] = _sha256(headed)
    state["envelope"] = envelope.to_json()
    state["pinned_at"] = _now().isoformat()
    state["script_regen_fails"] = 0
    state["backoff_until"] = None


def _verify_integrity(name: str, state: dict, allow_http: bool, http_hosts) -> str | None:
    """TOCTOU re-verify before an unattended run (§3.3): read the active script
    ONCE, confirm its sha256 matches trusted state AND it still passes validation,
    and return those exact bytes for the caller to execute. A tampered or
    mismatched script returns None (→ self-heal), never executed.

    Returning the verified bytes (rather than a bool) is what closes the TOCTOU
    window: ``run_sandboxed`` executes this returned content, not a re-read of the
    on-disk path that could be swapped between this check and exec."""
    active = _active_path(name)
    try:
        content = active.read_text()
    except OSError:
        return None
    if _sha256(content) != state.get("sha256"):
        log.warning("script_cache %s: on-disk hash mismatch — refusing (TOCTOU)", name)
        return None
    vr = validate_script(content, allow_http=allow_http, http_hosts=http_hosts)
    if not vr.ok:
        log.warning("script_cache %s: re-validation failed (%s) — refusing", name, vr.reason)
        return None
    return content


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def _record_tick(state: dict, mode: str, cost: float, *,
                 returncode: int | None = None, duration_ms: int = 0,
                 conditions_count: int = 0) -> None:
    state["last_mode"] = mode
    state["last_tick_at"] = _now().isoformat()
    if mode == "cached":
        state["cached_runs"] = state.get("cached_runs", 0) + 1
    else:
        state["fallback_runs"] = state.get("fallback_runs", 0) + 1
        state["total_agent_cost_usd"] = round(
            state.get("total_agent_cost_usd", 0.0) + (cost or 0.0), 6)
        state["last_regen_at"] = _now().isoformat()
    state["last_tick"] = {
        "mode": mode, "cost_usd": cost, "returncode": returncode,
        "duration_ms": duration_ms, "conditions_count": conditions_count,
    }


# ---------------------------------------------------------------------------
# Circuit breaker / backoff (§5)
# ---------------------------------------------------------------------------

def _backoff_active(state: dict) -> bool:
    until = _parse_iso(state.get("backoff_until"))
    return until is not None and _now() < until


def _bump_failure(name: str, monitor, state: dict, on_persistent_failure: str) -> None:
    """Increment the consecutive-regen-fail counter; at SCRIPT_REGEN_MAX fire
    ``script.failing`` and either pause (policy) or degrade with exponential
    backoff so we don't hammer the agent every tick."""
    fails = state.get("script_regen_fails", 0) + 1
    state["script_regen_fails"] = fails
    if fails >= SCRIPT_REGEN_MAX:
        backoff = min(_BACKOFF_CAP, TOOL_TIMEOUT * (2 ** (fails - SCRIPT_REGEN_MAX + 1)))
        if on_persistent_failure == "pause":
            state["paused"] = True
            _notify("monitor/script.failing", monitor,
                    {"reason": "persistent regen failure — paused",
                     "fails": fails}, state)
        else:
            state["backoff_until"] = (_now() + timedelta(seconds=backoff)).isoformat()
            _notify("monitor/script.failing", monitor,
                    {"reason": "persistent regen failure — degraded with backoff",
                     "fails": fails, "backoff_s": backoff}, state)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def _run_active(monitor, state: dict, policy: dict, fp: str, env: dict,
                id_field: str) -> list[Condition] | None | bool:
    """Try the active cached script. Returns conditions on success, or False to
    signal 'fall through to self-heal' (no active script, stale, tampered, or
    failed)."""
    active = _active_path(monitor.name)
    if not active.exists():
        return False
    if state.get("fingerprint") != fp:
        log.info("script_cache %s: fingerprint changed — regenerating", monitor.name)
        return False
    max_age = policy.get("max_age")
    if max_age:
        try:
            from modastack.monitors.schema import parse_interval
            pinned = _parse_iso(state.get("pinned_at"))
            if pinned and (_now() - pinned).total_seconds() > parse_interval(max_age):
                log.info("script_cache %s: max_age exceeded — regenerating", monitor.name)
                return False
        except Exception as e:
            log.warning("script_cache %s: bad max_age %r (%s) — ignoring",
                        monitor.name, max_age, e)
    # Read + verify the bytes ONCE, then execute exactly those bytes (closes the
    # verify→exec TOCTOU — run_sandboxed runs the verified content, not a re-read).
    content = _verify_integrity(monitor.name, state, policy["allow_http"], policy["http_hosts"])
    if content is None:
        return False
    cp = run_sandboxed(content, env, TOOL_TIMEOUT, name=monitor.name)
    if cp is None or cp.returncode != 0:
        log.info("script_cache %s: cached script failed — self-healing", monitor.name)
        return False
    items = _parse_items(cp.stdout, monitor.name)
    if items is None:
        log.info("script_cache %s: cached script printed garbage — self-healing", monitor.name)
        return False
    conditions = _items_to_conditions(items, id_field)
    # A successful cached run is the breaker's reset signal (§5).
    state["script_regen_fails"] = 0
    state["backoff_until"] = None
    _record_tick(state, "cached", 0.0, returncode=0, conditions_count=len(conditions))
    log.info("script_cache %s: mode=cached cost=$0 (%d conditions)",
             monitor.name, len(conditions))
    return conditions


def _self_heal(monitor, state: dict, policy: dict, fp: str, env: dict,
               id_field: str, cwd: str | None) -> list[Condition] | None:
    """Agent runtime: produce this tick's items AND a candidate script; pin per
    approval mode; never waste the tick (return the agent's items either way)."""
    gen = generate_candidate(monitor, cwd, policy)
    if not gen.success or gen.items is None:
        _bump_failure(monitor.name, monitor, state, policy["on_persistent_failure"])
        _record_tick(state, "fallback_regen", gen.cost_usd, returncode=1)
        log.warning("script_cache %s: generation failed (%s)", monitor.name, gen.error)
        return None  # indeterminate — leave state untouched downstream

    conditions = _items_to_conditions(gen.items, id_field)
    mode = "first_gen" if not _active_path(monitor.name).exists() else "fallback_regen"

    candidate = gen.script
    if candidate and policy["approval"] != "off":
        vr = validate_script(candidate, allow_http=policy["allow_http"],
                             http_hosts=policy["http_hosts"])
        if vr.ok and _smoke_ok(candidate, env, monitor.name):
            # A valid, smoke-passing candidate is NOT a regen failure — whether we
            # pin it (auto / in-envelope) or queue it for review, generation
            # succeeded, so the breaker resets (§5: reset on a successful pin; a
            # queued-but-valid candidate is equally healthy).
            state["script_regen_fails"] = 0
            state["backoff_until"] = None
            if _should_pin(state, vr.envelope, policy["approval"]):
                _pin(monitor.name, candidate, monitor, fp, vr.envelope, state)
                _notify("monitor/script.first_run", monitor, {
                    "mode": mode, "approval": policy["approval"],
                    "summary": f"auto-pinned generated script for {monitor.name}",
                    "envelope": vr.envelope.to_json(),
                    "sha256": state["sha256"],
                }, state)
            else:
                _queue_review(monitor, candidate, vr, state)
        else:
            reason = vr.reason if not vr.ok else "smoke run failed"
            log.warning("script_cache %s: candidate rejected (%s)", monitor.name, reason)
            _bump_failure(monitor.name, monitor, state, policy["on_persistent_failure"])
    _record_tick(state, mode, gen.cost_usd, returncode=0,
                 conditions_count=len(conditions))
    return conditions


def _should_pin(state: dict, envelope: CapabilityEnvelope, approval: str) -> bool:
    """auto → always pin. review → pin only when it stays inside the previously
    approved capability envelope (a mechanical self-heal); otherwise queue for a
    human (a capability change needs fresh eyes)."""
    if approval == "auto":
        return True
    # review mode
    prior = state.get("envelope")
    if prior is None:
        return False  # never approved → first run needs human
    return CapabilityEnvelope.from_json(prior).covers(envelope)


def _queue_review(monitor, candidate: str, vr: ValidationResult, state: dict) -> None:
    """Write the candidate to pending/ and fire a review request; keep using the
    agent runtime until a human promotes it (review mode / out-of-envelope)."""
    try:
        _pending_path(monitor.name).write_text(candidate)
    except OSError as e:
        log.warning("script_cache %s: couldn't write pending script: %s", monitor.name, e)
    state["pending_envelope"] = vr.envelope.to_json()
    _notify("monitor/script.review_requested", monitor, {
        "summary": f"generated script for {monitor.name} awaits approval",
        "reason": "review mode (or capability change on self-heal)",
        "envelope": vr.envelope.to_json(),
    }, state)


def approve_pending(monitor, scripts_dir: Path | None = None) -> bool:
    """Promote a queued ``pending/<name>.sh`` to active (CLI approve-script).

    Re-validates the pending script, smoke-runs it, then atomically pins it and
    records the trusted sha256 + capability envelope. Returns False when there is
    nothing to approve or the candidate no longer validates."""
    name = monitor.name
    pending = _pending_path(name)
    if not pending.exists():
        log.error("script_cache %s: no pending script to approve", name)
        return False
    content = pending.read_text()
    policy = _policy(monitor)
    vr = validate_script(content, allow_http=policy["allow_http"],
                         http_hosts=policy["http_hosts"])
    if not vr.ok:
        log.error("script_cache %s: pending script no longer validates (%s)", name, vr.reason)
        return False
    env = dict(os.environ)
    if not _smoke_ok(content, env, name):
        log.error("script_cache %s: pending script failed smoke run", name)
        return False
    state = _load_trusted_state(name)
    fp = _fingerprint(monitor.extra.get("prompt", ""),
                      monitor.extra.get("id_field", "id"), monitor.extra or {})
    _pin(name, content, monitor, fp, vr.envelope, state)
    state.pop("pending_envelope", None)
    _save_trusted_state(name, state)
    pending.unlink(missing_ok=True)
    log.info("script_cache %s: pending script approved + pinned", name)
    return True


def recache(monitor) -> None:
    """Explicit invalidation (CLI recache): drop the active script + trusted
    state so the next tick regenerates from scratch."""
    name = monitor.name
    _active_path(name).unlink(missing_ok=True)
    _pending_path(name).unlink(missing_ok=True)
    state = _load_trusted_state(name)
    for k in ("fingerprint", "sha256", "envelope", "pinned_at",
              "script_regen_fails", "backoff_until", "paused", "pending_envelope"):
        state.pop(k, None)
    _save_trusted_state(name, state)
    log.info("script_cache %s: cache invalidated — next tick regenerates", name)


def script_cache(monitor, projects: list[Path]) -> list[Condition] | None:
    """``script_cache`` check runner (#327). See module docstring.

    Returns a list of Conditions (possibly empty = all clear) or None
    (indeterminate — the detection failed; the scheduler leaves state untouched
    and retries next interval)."""
    prompt = (monitor.extra or {}).get("prompt")
    if not prompt:
        log.error("script_cache monitor %s: missing required 'prompt'", monitor.name)
        return None
    id_field = (monitor.extra or {}).get("id_field", "id")
    policy = _policy(monitor)
    state = _load_trusted_state(monitor.name)
    fp = _fingerprint(prompt, id_field, monitor.extra or {})
    env = dict(os.environ)
    cwd = str(projects[0]) if projects else None

    try:
        if state.get("paused"):
            log.warning("script_cache %s: paused after persistent failure — "
                        "skipping (run `monitors recache %s` to resume)",
                        monitor.name, monitor.name)
            return None

        # Always try the cheap cached fast path first — the circuit breaker
        # throttles only *regeneration*, never a still-valid pinned script. A
        # cached hit also resets the breaker (in _run_active).
        result = _run_active(monitor, state, policy, fp, env, id_field)
        if result is not False:
            return result  # cached hit (conditions)

        # No usable cached script → we'd regenerate. When the breaker has tripped,
        # gate regen behind the backoff window so we don't hammer the agent every
        # tick; detection resumes (at reduced frequency) once it elapses (§5).
        if _backoff_active(state):
            log.info("script_cache %s: in regen backoff — skipping regen this tick",
                     monitor.name)
            return None

        return _self_heal(monitor, state, policy, fp, env, id_field, cwd)
    finally:
        _save_trusted_state(monitor.name, state)


# Native check runners, keyed by the monitor's `check` field. Auto-loaded by the
# scheduler's *_checks.py glob — no scheduler change to register.
CHECKS = {"script_cache": script_cache}
