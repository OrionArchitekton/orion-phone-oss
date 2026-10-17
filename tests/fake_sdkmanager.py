"""Fake sdkmanager for tests: logs calls; fails like the real one when licenses are not accepted."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    state = json.loads(Path(os.environ["FAKE_ADB_STATE"]).read_text())
    with open(Path(state["log_dir"]) / "sdkmanager.log", "a") as log:
        log.write(json.dumps(argv) + "\n")
    if not state.get("licenses_accepted", True):
        sys.stderr.write("Warning: License for package Android Emulator not accepted.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
