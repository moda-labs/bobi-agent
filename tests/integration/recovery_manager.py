"""Child manager harness: real Session/transport, with a test-only stub barrier."""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from bobi.brain.stub import _StubSession
from bobi.paths import bind_root
from bobi.session import Session


def main():
    root, markers, brain, boot = sys.argv[1:]
    root, markers = Path(root), Path(markers)
    bind_root(root)
    logging.basicConfig(level=logging.INFO)

    class BarrierBrain(_StubSession):
        async def receive_response(self):
            if "PREFIX_EVENT" in self._pending:
                with (markers / "prefix").open("a") as output:
                    output.write("done\n")
            elif "PENDING_EVENT" in self._pending:
                (markers / f"attempt-{boot}").touch()
                (markers / "first").touch()
                while not (markers / "release").exists():
                    await asyncio.sleep(0.02)
                (markers / "second").touch()
            async for message in super().receive_response():
                yield message

    class ObservedSession(Session):
        async def _process_message(self, message):
            with (markers / f"deliveries-{boot}.jsonl").open("a") as output:
                output.write(json.dumps(message.text) + "\n")
            return await super()._process_message(message)

    session = ObservedSession(name="recovery-manager", cwd=str(root), role="manager")
    if brain == "stub":
        session._make_brain_session = lambda resume=None: BarrierBrain(resume=resume)
    if not session.start(startup_prompt=None, timeout=120):
        raise RuntimeError("recovery manager failed to start")
    try:
        if session._subscription is None or not session._subscription.client.wait_connected(20):
            raise RuntimeError("event subscription failed to connect")
        (markers / f"ready-{boot}").touch()
        while True:
            if session._state == "error":
                raise RuntimeError("recovery manager entered terminal error; see brain diagnostic")
            time.sleep(0.1)
    finally:
        session.stop()


if __name__ == "__main__":
    main()
