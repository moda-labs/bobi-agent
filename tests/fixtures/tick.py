"""What the real agent runs in V21 (#1122): a command that keeps writing.

It writes its own pid and ticks beside its own path, so the test judges what
happened to the agent's tool command from the command itself. It exits by itself
after a bound, so a failed test cannot leak a writer for long.
"""

import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIFETIME_SECONDS = 180.0

(HERE / "tick.pid").write_text(str(os.getpid()))
deadline = time.monotonic() + LIFETIME_SECONDS
while time.monotonic() < deadline:
    with open(HERE / "ticks", "a") as ticks:
        ticks.write("t\n")
    time.sleep(0.2)
