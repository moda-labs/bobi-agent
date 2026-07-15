"""OpenAI Codex brain adapter (epic #485, Phase 2 — MVP).

Backs the provider-agnostic :class:`~bobi.brain.base.BrainSession` with the
Codex CLI's headless contract (validated in the #485 Phase-0 spike):
``codex exec --json`` streams NDJSON turn events, ``codex exec resume <thread>``
continues a prior session by its rollout, and ``thread.started.thread_id`` is the
resume token.

This is a **stateless-per-turn** session: there is no long-lived process: each
``receive_response`` cold-starts a ``codex exec`` subprocess that (re)plays the
thread, streams one turn, and exits. That satisfies the persistent
``connect``/``query``/``receive_response``/``disconnect`` interface the manager
loop drives — the manager already injects between turns — at the cost of a
process spawn per turn. A hot ``app-server`` session is a later optimization.

MVP scope / known gaps (tracked in the spec): no per-message cost in dollars
(token counts only - the spend surfaces estimate dollars at fold time from the
recorded token facts, see ``bobi.costs``, #760); the system prompt is prepended
to the first turn of a fresh thread rather than written as ``AGENTS.md``.

MCP servers (#428 Stage 4): Codex reads them from ``~/.codex/config.toml`` at
process start (nothing rides the CLI invocation), so :meth:`CodexBrain.make_session`
renders the team's effective ``mcp_servers`` (splatted into ``options`` by
``subagent.py``) to that file before the first ``codex exec`` runs. See
``bobi.brain.codex_config``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from bobi.brain.base import (
    AssistantText,
    BrainCapabilities,
    BrainCost,
    BrainMessage,
    BrainSession,
    TurnResult,
)

# Headless, non-interactive, fully autonomous: the box is the sandbox, so we
# bypass Codex's approval/sandbox prompts (the analog of Claude's
# ``bypassPermissions``). ``--skip-git-repo-check`` lets it run outside a repo.
_EXEC_FLAGS = (
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
)

# Codex emits one JSON object per line, and a single item.completed event can
# carry a large assistant message. asyncio's default subprocess stream limit is
# only 64 KiB, which turns a healthy long NDJSON event into a ValueError from
# StreamReader.readline().
_CODEX_STREAM_LIMIT = 16 * 1024 * 1024
_CODEX_TERMINATE_TIMEOUT = 5.0


def _instructions(system_prompt: Any) -> str:
    """Extract the agent instructions from a brain system_prompt.

    The call sites still build Claude's ``{"preset": ..., "append": "<role +
    tools + policy>"}`` dict; for Codex we take the ``append`` text. A plain
    string is used as-is.
    """
    if isinstance(system_prompt, dict):
        return str(system_prompt.get("append", "") or "")
    if isinstance(system_prompt, str):
        return system_prompt
    return ""


def _costs(u: dict, model: str) -> list[BrainCost]:
    # codex's usage reports input_tokens INCLUSIVE of cached_input_tokens
    # (its non_cached_input() is input - cached), so record both as-is;
    # summing them double-counts every cache read. reasoning_output_tokens
    # is likewise a subset of output_tokens - never add it. #760
    return [BrainCost(model=model or "codex",
                      input_tokens=u.get("input_tokens", 0) or 0,
                      cached_input_tokens=u.get("cached_input_tokens", 0) or 0,
                      output_tokens=u.get("output_tokens", 0) or 0)]


async def _write_stdin(writer: asyncio.StreamWriter, text: str) -> None:
    try:
        writer.write(text.encode("utf-8"))
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _spawn_codex(
    argv: list[str],
    cwd: str,
    stdin_text: str | None = None,
) -> AsyncIterator[dict]:
    """Run ``codex exec`` and yield its NDJSON events as parsed dicts.

    When ``stdin_text`` is provided, the caller must include ``-`` in argv and
    this function writes then closes stdin. Otherwise stdin is ``/dev/null`` -
    ``codex exec`` blocks reading a piped-but-open stdin (Phase-0 gotcha).
    Non-JSON lines (banners) are skipped.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_CODEX_STREAM_LIMIT,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(proc.stderr.read())
    stdin_task = (
        asyncio.create_task(_write_stdin(proc.stdin, stdin_text))
        if stdin_text is not None and proc.stdin is not None else None
    )
    exhausted = False
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                exhausted = True
                break
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except json.JSONDecodeError:
                continue
    finally:
        if not exhausted and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    proc.wait(), timeout=_CODEX_TERMINATE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        await proc.wait()
        if stdin_task is not None:
            try:
                await asyncio.wait_for(
                    stdin_task, timeout=_CODEX_TERMINATE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                stdin_task.cancel()
        try:
            stderr = await asyncio.wait_for(
                stderr_task, timeout=_CODEX_TERMINATE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            stderr_task.cancel()
            stderr = b""
        if exhausted and proc.returncode:
            detail = stderr.decode("utf-8", "replace").strip()
            tail = detail[-2000:] if detail else "no stderr"
            raise RuntimeError(
                f"codex subprocess exited {proc.returncode}: {tail}"
            )


class _CodexSession:
    """A :class:`BrainSession` backed by per-turn ``codex exec`` subprocesses."""

    provider = "openai"

    def __init__(
        self,
        *,
        cwd: str,
        instructions: str,
        resume: str | None = None,
        model: str = "",
        effort: str = "",
        runner=None,
        mcp_servers: dict | None = None,
        mcp_env: dict[str, str] | None = None,
        config_overrides: list[str] | None = None,
        provider: str = "openai",
    ) -> None:
        self.provider = provider
        self._cwd = cwd or "."
        self._instructions = instructions
        self._thread_id = resume or None
        self._model = model
        self._effort = effort
        self._runner = runner or _spawn_codex
        self._pending: str | None = None
        # Effective MCP servers (rendered into config.toml at make_session).
        # Codex has no live status introspection, so the preflight probe reaches
        # each server directly through get_mcp_status below (#428 Stage 4).
        self._mcp_servers = mcp_servers or {}
        self._mcp_env = mcp_env
        self._config_overrides = list(config_overrides or [])

    async def get_mcp_status(self) -> dict:
        """A Claude-``get_mcp_status``-shaped MCP status for the preflight probe.

        Codex reads MCP from ``~/.codex/config.toml`` and can't report live
        status, so this runs a direct ``initialize`` + ``tools/list`` handshake
        against each configured server (proving the same servers config.toml
        wires up actually answer). Keeps ``validate._async_probe_mcp`` a single
        loop across brains instead of warn-degrading for Codex.

        Probes under the caller-provided runtime env when present so a
        bare-command stdio server and its credentials match the environment
        validate passes to other brains. Falls back to the generic spawn env for
        direct uses outside validation."""
        from bobi.env import agent_spawn_env
        from bobi.mcp_handshake import preflight_timeout, probe_servers

        return await probe_servers(
            self._mcp_servers,
            timeout=preflight_timeout(),
            env=self._mcp_env or agent_spawn_env(),
        )

    async def connect(self, prompt: str | None = None) -> None:
        # No persistent process — just stash any startup prompt for the first
        # receive_response (mirrors how session.py drains the connect turn).
        self._pending = prompt

    async def query(self, text: str) -> None:
        self._pending = text

    async def disconnect(self) -> None:
        return None

    def _build_argv(self) -> list[str]:
        flags = list(_EXEC_FLAGS)
        if self._model:
            flags += ["-m", self._model]
        if self._effort:
            flags += ["-c", f"model_reasoning_effort={self._effort}"]
        for override in self._config_overrides:
            flags += ["-c", override]
        if self._thread_id:
            # `resume` has a narrower flag set (no -C/--sandbox); cwd comes from
            # the subprocess cwd. The bypass/skip flags are accepted here too.
            return ["codex", "exec", "resume", self._thread_id, *flags, "-"]
        return ["codex", "exec", *flags, "-"]

    async def receive_response(self) -> AsyncIterator[BrainMessage]:
        if self._pending is None:
            # No input to run (e.g. a rotation reconnect's drain) — emit a no-op
            # result so the caller advances to waiting_input instead of hanging.
            yield TurnResult(session_id=self._thread_id or "")
            return

        prompt = self._pending
        self._pending = None
        # A fresh thread carries no context, so prepend the agent instructions;
        # a resumed thread already has them in its rollout.
        if not self._thread_id and self._instructions:
            prompt = f"{self._instructions}\n\n{prompt}"

        argv = self._build_argv()
        async for ev in self._runner(argv, self._cwd, prompt):
            etype = ev.get("type")
            if etype == "thread.started":
                self._thread_id = ev.get("thread_id") or self._thread_id
            elif etype == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    yield AssistantText(text=item["text"])
            elif etype == "turn.completed":
                usage = ev.get("usage") or {}
                # Deliberately do NOT feed codex usage to the manager's context-
                # rotation metric. codex exec reports a per-turn AGGREGATE (summed
                # across internal model calls), which over-counts context fill by
                # an order of magnitude (observed 900K–1.2M live) and triggers a
                # rotation STORM — the manager resets the thread almost every turn,
                # losing continuity. Codex manages its own context window
                # (auto-compaction), so the manager keeps one stable thread. Cost
                # attribution still uses the usage. (#485 follow-up: a turn-count
                # rotation if unbounded rollout growth ever becomes an issue.)
                yield TurnResult(
                    session_id=self._thread_id or "",
                    costs=_costs(usage, self._model),
                )
                return
            elif etype in ("turn.failed", "error"):
                err = ev.get("error")
                msg = err.get("message") if isinstance(err, dict) else None
                yield TurnResult(
                    session_id=self._thread_id or "",
                    is_error=True,
                    result_text=msg or ev.get("message") or "codex turn failed",
                )
                return

        # Stream ended without a terminal event — treat as a lost turn.
        yield TurnResult(
            session_id=self._thread_id or "",
            is_error=True,
            result_text="codex exited without completing the turn",
        )


class CodexBrain:
    """Factory for Codex sessions (``brain: {kind: codex}``).

    Gateway-aware (#789): when a gateway base URL is pinned (``brain.base_url``
    on a codex-engine team), every invocation carries the ``bobi_gateway``
    provider overrides, the cost-attribution provider flips to ``"gateway"``,
    and cross-model resume drops to the conservative fresh+reinject path. Same
    machinery either way - gateway mode is endpoint config, not a different
    brain.
    """

    name = "codex"
    # Efforts per the OpenAI API's ReasoningEffortParam enum (verified live
    # 2026-07-14 on codex-cli 0.144.4: an unknown value 400s at turn start).
    # The vocabulary holds in gateway mode too - the value rides codex's
    # model_reasoning_effort config, whatever endpoint it dials.
    _EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

    @property
    def provider(self) -> str:
        from bobi.brain.gateway import gateway_base_url

        return "gateway" if gateway_base_url() else "openai"

    @property
    def capabilities(self) -> BrainCapabilities:
        # ``codex exec resume <thread> -m <other-model>`` genuinely switches
        # the thread's model with the transcript intact - verified 2026-07-04
        # on codex-cli 0.142.2 (#649): the rollout records turn_context model
        # gpt-5.4 then gpt-5.5, and the resumed turn recalled conversation-only
        # state. Note the usable model set depends on the account's auth mode
        # (ChatGPT-plan auth rejects some models with a 400 at turn start).
        # Whether a gateway backend honors resume-with-another-model is
        # backend-dependent: gateway mode ships conservative (fresh+reinject
        # on model switches), the #649 arc.
        from bobi.brain.gateway import gateway_base_url

        return BrainCapabilities(
            cross_model_resume=not gateway_base_url(),
            efforts=self._EFFORTS,
        )

    def make_session(
        self,
        *,
        cwd: str | None,
        system_prompt: Any,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        from bobi.brain import resolve_effort_option, resolve_model_option
        from bobi.brain.gateway import gateway_base_url

        opts = options or {}
        model = resolve_model_option(str(opts.get("model", "") or ""))
        effort = resolve_effort_option(str(opts.get("effort", "") or ""))
        # Codex reads MCP servers from ~/.codex/config.toml, not from the CLI
        # invocation, so render the team's effective mcp_servers to disk before
        # the session's first `codex exec`. Render when there is a set to write OR
        # a stale bobi-managed block to clear (a team that dropped its MCP deps);
        # otherwise never touch the file. The write is idempotent (a no-op when
        # unchanged) but errors PROPAGATE: a codex team that declares MCP and
        # can't render config would silently run MCP-less, so surface it rather
        # than pass preflight and fail at runtime.
        from bobi.brain.codex_config import (
            codex_home, config_has_managed_block, write_codex_config,
        )
        mcp_servers = opts.get("mcp_servers") or {}
        home = codex_home()
        if mcp_servers or config_has_managed_block(home):
            write_codex_config(mcp_servers, home)
        config_overrides: list[str] = []
        if gateway_base_url():
            from bobi.brain.gateway_openai import gateway_openai_overrides

            config_overrides = gateway_openai_overrides()
        return _CodexSession(
            cwd=cwd or ".",
            instructions=_instructions(system_prompt),
            resume=resume,
            # Claude-specific options (skills/hooks/permission_mode/max_turns)
            # don't apply to Codex; only model and effort overrides are honored.
            model=model,
            effort=effort,
            mcp_servers=mcp_servers,
            mcp_env=opts.get("env") if isinstance(opts.get("env"), dict) else None,
            config_overrides=config_overrides,
            provider=self.provider,
        )
