"""Terminal REPL for the setup session.

One persistent Claude session converses with the user; this module owns
the terminal: it streams assistant text out, reads user input between
turns, and handles Ctrl-C (interrupt the turn) / Ctrl-D (pause, resume
later with --resume). Deterministic actions happen inside the session's
in-process tools (tools.py), so the loop itself stays dumb.

Spike-verified SDK behavior this relies on:
- an in-process tool may block on a stdin prompt mid-turn (stdin is
  uncontended while the REPL awaits the turn)
- client.interrupt() ends the receive_response() iterator with a
  ResultMessage and the session remains usable
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import click

from modastack.setup.prompt import build_setup_prompt, kickoff_prompt
from modastack.setup.state import SetupState
from modastack.setup.tools import create_setup_server

INPUT_PROMPT = "you> "


def _read_user_input() -> str | None:
    """Read one user message from the terminal. None on EOF (Ctrl-D)."""
    try:
        line = input(INPUT_PROMPT)
    except EOFError:
        return None
    # A trailing backslash continues onto the next line.
    parts = [line]
    while parts[-1].endswith("\\"):
        parts[-1] = parts[-1][:-1]
        try:
            parts.append(input("...> "))
        except EOFError:
            break
    return "\n".join(parts)


def _make_write_guard(project: Path, state: SetupState) -> dict:
    """PreToolUse hook: transcript hygiene + scoped writes.

    Denies reads that touch .modastack/.env (secrets stay out of the
    transcript) and file writes outside the team source being built.
    Best-effort for Bash (string match), exact for Write/Edit/Read.
    """
    from claude_agent_sdk import HookMatcher

    def _deny(reason: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    env_file = (project / ".modastack" / ".env").resolve()

    def _resolve(raw: str) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            p = project / p
        return p.resolve()

    async def _guard(input_data, tool_use_id, context):
        tool = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}

        # Path-bearing tools get exact checks on the resolved target —
        # never on file *content*, which may legitimately mention .env.
        if tool in ("Read", "Write", "Edit", "NotebookEdit", "Grep", "Glob"):
            raw = str(tool_input.get("file_path")
                      or tool_input.get("path") or "")
            target = _resolve(raw) if raw else None
            if target == env_file or (
                    tool in ("Grep", "Glob") and target == env_file.parent):
                return _deny(
                    ".modastack/.env holds secrets and stays out of this "
                    "conversation — use save_credential and check_venn instead"
                )
            if tool in ("Write", "Edit", "NotebookEdit"):
                allowed = ((project / "agents" / state.team_name).resolve()
                           if state.team_name else None)
                if not allowed or target is None or not (
                        target == allowed or target.is_relative_to(allowed)):
                    return _deny(
                        "setup writes only the team source at "
                        f"agents/{state.team_name or '<name>'}/ — installation "
                        "happens through install_team, never by editing "
                        ".modastack/ or other project files"
                    )
        elif ".env" in str(tool_input):
            # Best-effort for Bash and everything else: block anything that
            # so much as mentions an env file rather than risk a transcript
            # leak. save_credential/check_venn cover the legitimate needs.
            return _deny(
                "this command references a .env file — secrets stay out of "
                "this conversation; use save_credential and check_venn instead"
            )
        return {}

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[_guard])]}


async def run_repl(project: Path, model: str | None = None,
                   resume: bool = False,
                   client_factory: Callable | None = None,
                   input_fn: Callable[[], str | None] | None = None,
                   secret_prompt_fn: Callable[[str, str, str], str] | None = None,
                   ) -> int:
    """Run the setup conversation. Returns a process exit code."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )
    from modastack.sdk import get_cli_path

    read_input = input_fn or _read_user_input

    state = None
    if resume:
        state = SetupState.load(project)
        if state is None:
            click.echo("No setup in progress to resume — run `modastack setup`.",
                       err=True)
            return 1
        if state.finished:
            click.echo("The previous setup finished — run `modastack setup` "
                       "to start a new one.", err=True)
            return 1
    if state is None:
        state = SetupState()
        SetupState.clear(project)

    server = create_setup_server(state, project, prompt_fn=secret_prompt_fn)
    options = ClaudeAgentOptions(
        cwd=str(project),
        permission_mode="bypassPermissions",
        max_turns=200,
        cli_path=get_cli_path(),
        model=model,
        resume=state.session_id or None,
        mcp_servers={"setup": server},
        hooks=_make_write_guard(project, state),
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": build_setup_prompt(),
        },
    )
    client = (client_factory or ClaudeSDKClient)(options)

    await client.connect()
    await client.query(kickoff_prompt(project, state, resumed=resume))

    loop = asyncio.get_running_loop()

    # Ctrl-C never reaches this coroutine as KeyboardInterrupt — asyncio.run
    # cancels the task instead — so SIGINT gets an explicit handler: during
    # a turn it interrupts the agent (the turn still ends with a
    # ResultMessage, spike-verified); at the input prompt it just reminds
    # the user how to pause, since the blocked reader thread can't be
    # cancelled from here.
    import signal

    turn_active = False

    def _on_sigint():
        if turn_active:
            asyncio.ensure_future(client.interrupt())
            click.echo("\n(interrupting — type to continue, Ctrl-D to pause)")
        else:
            click.echo("\n(Ctrl-D on an empty line pauses setup)")

    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
    except (NotImplementedError, RuntimeError):
        pass  # non-main thread or unsupported platform: Ctrl-C just exits

    exit_code = 1
    try:
        while True:
            turn_active = True
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            click.echo(block.text + "\n")
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        state.session_id = msg.session_id
                        state.save(project)
            turn_active = False

            if state.finished:
                exit_code = 0
                break

            # Re-prompt on blank lines here — re-entering receive_response()
            # with no in-flight turn would block forever.
            while True:
                text = await loop.run_in_executor(None, read_input)
                if text is None or text.strip():
                    break
            if text is None:
                click.echo("\nSetup paused. Resume anytime with "
                           "`modastack setup --resume`.")
                break
            await client.query(text)
    finally:
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            await client.disconnect()
        except Exception:
            pass

    if state.finished:
        SetupState.clear(project)
    return exit_code
