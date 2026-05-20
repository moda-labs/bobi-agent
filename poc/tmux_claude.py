"""Proof of concept: puppet Claude Code inside tmux.

Tests three critical operations:
1. Spawn an interactive claude session in tmux
2. Capture output and detect when claude is waiting for input
3. Inject text as if a human typed it

Usage:
    python poc/tmux_claude.py spawn test-session
    python poc/tmux_claude.py capture test-session
    python poc/tmux_claude.py inject test-session "hello world"
    python poc/tmux_claude.py ask test-session "/pickup AGD-17"
    python poc/tmux_claude.py wait-for-prompt test-session
    python poc/tmux_claude.py demo
    python poc/tmux_claude.py kill test-session
    python poc/tmux_claude.py list
"""

import re
import shutil
import subprocess
import sys
import time


TMUX = shutil.which("tmux") or "tmux"
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"


def session_exists(name: str) -> bool:
    result = subprocess.run(
        [TMUX, "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def spawn_session(name: str, cwd: str = ".", skip_permissions: bool = True) -> bool:
    """Spawn an interactive claude session in a tmux window."""
    if session_exists(name):
        print(f"Session '{name}' already exists")
        return False

    cmd = [CLAUDE]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    subprocess.run([
        TMUX, "new-session",
        "-d",                   # detached
        "-s", name,             # session name
        "-x", "200",            # wide terminal
        "-y", "50",             # tall terminal
    ] + cmd, cwd=cwd)

    time.sleep(2)

    if session_exists(name):
        print(f"Session '{name}' spawned")
        # Enable logging
        log_path = f"/tmp/agentd-{name}.log"
        subprocess.run([
            TMUX, "pipe-pane", "-t", name, "-o", f"cat >> {log_path}",
        ])
        print(f"Logging to {log_path}")
        return True
    else:
        print(f"Failed to spawn session '{name}'")
        return False


def capture_pane(name: str, lines: int = 100) -> str:
    """Capture the current visible content of a tmux pane."""
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", name, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return result.stdout


def inject_text(name: str, text: str, settle_time: float = 2.0) -> None:
    """Type text into the tmux session as if a human typed it.

    settle_time: seconds to wait after injecting, giving claude time to
    start processing before we check state. Without this, detect_prompt_state
    may see the ❯ prompt before claude begins working.
    """
    subprocess.run([TMUX, "send-keys", "-t", name, "-l", text])
    subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    print(f"Injected: {text[:80]}...")
    time.sleep(settle_time)


def detect_prompt_state(name: str) -> dict:
    """Analyze the pane to determine what state claude is in.

    Returns a dict with:
      - state: 'waiting_input' | 'working' | 'asking_question' | 'exited' | 'unknown'
      - question: str (if asking_question)
      - options: list[str] (if asking_question with numbered options)
      - last_lines: list[str] (last N non-empty lines)
    """
    if not session_exists(name):
        return {"state": "exited", "last_lines": []}

    raw = capture_pane(name, lines=50)
    lines = [l for l in raw.splitlines() if l.strip()]

    if not lines:
        return {"state": "unknown", "last_lines": []}

    last_lines = lines[-20:]
    bottom = "\n".join(lines[-10:])

    # Claude Code shows "❯" prompt when waiting for user input
    # The prompt line contains ❯ followed by suggestion text or empty
    # The bottom line usually shows the permissions mode indicator
    for line in reversed(lines[-5:]):
        if "❯" in line and "bypass permissions" not in line:
            # Check if this is the input prompt (not a previous command)
            # The active prompt has ❯ with suggestion text or just ❯
            # and the pane bottom shows the mode indicator
            if any("bypass permissions" in l or "⏵⏵" in l for l in lines[-3:]):
                return {"state": "waiting_input", "last_lines": last_lines}
            break

    # AskUserQuestion shows numbered options like:
    #   1. Option one
    #   2. Option two
    #   3. Other
    option_pattern = r"^\s*\d+\.\s+.+"
    options = [l.strip() for l in last_lines if re.match(option_pattern, l)]
    if len(options) >= 2:
        # Find the question text (usually above the options)
        question_lines = []
        for l in reversed(last_lines):
            if re.match(option_pattern, l):
                continue
            if l.strip():
                question_lines.insert(0, l.strip())
            if len(question_lines) >= 3:
                break
        return {
            "state": "asking_question",
            "question": " ".join(question_lines),
            "options": options,
            "last_lines": last_lines,
        }

    # Check if claude process is still running
    pane_pid_result = subprocess.run(
        [TMUX, "list-panes", "-t", name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if pane_pid_result.returncode == 0:
        pane_pid = pane_pid_result.stdout.strip()
        # Check if the pane's child process is alive
        children = subprocess.run(
            ["pgrep", "-P", pane_pid],
            capture_output=True, text=True,
        )
        if children.returncode != 0 or not children.stdout.strip():
            return {"state": "exited", "last_lines": last_lines}

    # Default: claude is working (tool calls, reading files, etc.)
    return {"state": "working", "last_lines": last_lines}


def wait_for_prompt(name: str, timeout: int = 300, poll_interval: int = 2,
                    require_change: bool = False) -> dict:
    """Wait until claude is waiting for input or asking a question.

    If require_change is True, capture the initial pane content and wait
    until the content changes AND claude is at a prompt. This prevents
    false positives where we detect the prompt before claude starts.
    """
    start = time.time()
    last_state = ""
    initial_content = capture_pane(name, lines=50) if require_change else None
    content_changed = not require_change

    while time.time() - start < timeout:
        state = detect_prompt_state(name)
        if state["state"] != last_state:
            print(f"  State: {state['state']}")
            last_state = state["state"]

        if not content_changed:
            current = capture_pane(name, lines=50)
            if current != initial_content:
                content_changed = True

        if state["state"] == "working":
            content_changed = True

        if content_changed and state["state"] in ("waiting_input", "asking_question", "exited"):
            return state

        time.sleep(poll_interval)

    return {"state": "timeout", "last_lines": []}


def kill_session(name: str) -> None:
    subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True)
    print(f"Session '{name}' killed")


def list_sessions() -> None:
    result = subprocess.run(
        [TMUX, "list-sessions"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(result.stdout)
    else:
        print("No tmux sessions")


def demo():
    """Full demo: spawn claude, send a task, capture the result."""
    session = "agentd-poc"

    if session_exists(session):
        kill_session(session)
        time.sleep(1)

    print("=== Step 1: Spawn claude in tmux ===")
    spawn_session(session, cwd="/Users/zkozick/dev/agentd")

    print("\n=== Step 2: Wait for claude to be ready ===")
    state = wait_for_prompt(session, timeout=30)
    print(f"State: {state['state']}")

    if state["state"] != "waiting_input":
        print("Claude not ready, aborting")
        return

    print("\n=== Step 3: Send a simple task ===")
    inject_text(session, "What files are in the engineer/ directory? Just list them, nothing else.")

    print("\n=== Step 4: Wait for claude to finish and show prompt ===")
    state = wait_for_prompt(session, timeout=60)
    print(f"State: {state['state']}")

    print("\n=== Step 5: Capture final output ===")
    output = capture_pane(session, lines=30)
    print(output)

    print("\n=== Step 6: Clean up ===")
    kill_session(session)
    print("Demo complete!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "spawn":
        name = sys.argv[2] if len(sys.argv) > 2 else "agentd-test"
        cwd = sys.argv[3] if len(sys.argv) > 3 else "/Users/zkozick/dev/agentd"
        spawn_session(name, cwd=cwd)

    elif cmd == "capture":
        name = sys.argv[2] if len(sys.argv) > 2 else "agentd-test"
        print(capture_pane(name))

    elif cmd == "inject":
        name = sys.argv[2]
        text = sys.argv[3]
        inject_text(name, text)

    elif cmd == "state":
        name = sys.argv[2] if len(sys.argv) > 2 else "agentd-test"
        import json
        state = detect_prompt_state(name)
        print(json.dumps(state, indent=2))

    elif cmd == "wait-for-prompt":
        name = sys.argv[2] if len(sys.argv) > 2 else "agentd-test"
        state = wait_for_prompt(name)
        import json
        print(json.dumps(state, indent=2))

    elif cmd == "ask":
        name = sys.argv[2]
        text = sys.argv[3]
        inject_text(name, text)
        state = wait_for_prompt(name)
        import json
        print(json.dumps(state, indent=2))

    elif cmd == "kill":
        name = sys.argv[2] if len(sys.argv) > 2 else "agentd-test"
        kill_session(name)

    elif cmd == "list":
        list_sessions()

    elif cmd == "demo":
        demo()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
