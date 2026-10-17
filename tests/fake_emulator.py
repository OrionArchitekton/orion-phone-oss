"""A stand-in for the Android emulator, for the tests of the unit that supervises it.

It records how it was started, then behaves as FAKE_EMULATOR says, and records how it is stopped. Everything is written
under FAKE_EMULATOR_DIR: emulator.json (its argv, working directory, the environment a unit gives it, and what its
standard input is), emulator.events (one line per event, with the time), and emulator.pid, written last, once its signal
handlers are in place, so a test that has seen the pid can signal it. The pid file is whole or absent: it is written under
a temporary name and renamed into place, so a test that waits for it to exist and then reads it never finds it empty.
FAKE_EMULATOR_PID_DELAY (seconds, none by default) holds the rename back that long: the gap it guards is microseconds wide,
and a test widens it to see that nobody is let into it.

Modes: `sleep` (the default: run until stopped; SIGTERM is logged and ends it with status 0), `ignore-term` (SIGTERM is
logged and ignored, so only SIGKILL ends it), `exit:N` (exit with status N after FAKE_EMULATOR_LIFETIME seconds), and
`die:N` (be killed by signal N after the same time).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

directory = Path(os.environ["FAKE_EMULATOR_DIR"])
mode = os.environ.get("FAKE_EMULATOR", "sleep")
lifetime = float(os.environ.get("FAKE_EMULATOR_LIFETIME", "0.3"))


def event(text: str) -> None:
    fd = os.open(directory / "emulator.events", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f"{time.time():.3f} {text}\n".encode())
    finally:
        os.close(fd)


def publish(path: Path, text: str, delay: float = 0.0) -> None:
    """Write `text` to `path` whole or not at all: to a temporary name in the same directory, then renamed over `path`. A
    file that exists is never empty or cut short, whoever is waiting for it. `delay` holds the rename back, to widen the
    gap between the two names for a test."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    time.sleep(delay)
    os.replace(tmp, path)


def on_term(signum: int, frame: object) -> None:
    if mode == "ignore-term":
        event("SIGTERM ignored")
        return
    event("SIGTERM")
    sys.exit(0)


stdin_is_dev_null = os.path.samestat(os.fstat(0), os.stat("/dev/null"))
(directory / "emulator.json").write_text(
    json.dumps(
        {
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "env": {
                name: os.environ.get(name)
                for name in ("ANDROID_SDK_ROOT", "ANDROID_AVD_HOME", "INVOCATION_ID", "DISPLAY", "ORION_PHONE_STATE")
            },
            "stdin_is_dev_null": stdin_is_dev_null,
        }
    )
)
signal.signal(signal.SIGTERM, on_term)
event("started")
publish(directory / "emulator.pid", str(os.getpid()), float(os.environ.get("FAKE_EMULATOR_PID_DELAY", "0")))

if mode.startswith("exit:"):
    time.sleep(lifetime)
    event(f"exiting with {mode.split(':', 1)[1]}")
    sys.exit(int(mode.split(":", 1)[1]))
if mode.startswith("die:"):
    time.sleep(lifetime)
    number = int(mode.split(":", 1)[1])
    event(f"killing itself with signal {number}")
    if number != signal.SIGKILL:  # whose disposition cannot be set, and need not be
        signal.signal(number, signal.SIG_DFL)
    os.kill(os.getpid(), number)
    time.sleep(60)  # a signal that does not end it: the test sees that
give_up = time.time() + 300  # a test run killed before it could clean up must not leave an emulator running for ever
while time.time() < give_up:
    time.sleep(0.05)
event("gave up waiting to be stopped")
