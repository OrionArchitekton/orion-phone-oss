"""Fake avdmanager for tests: `create avd -n NAME` makes NAME.avd/config.ini where the real tool would.

Observed with the real command-line tools 23.0: ANDROID_AVD_HOME is used only when it names an existing directory (an
empty value names the working directory). A missing one is ignored, not created, and the phone goes where the tool
looks next: ANDROID_USER_HOME/avd when that variable is set, else ~/.android/avd. Those two the tool creates. A test
that hands this fake a missing ANDROID_AVD_HOME therefore sees the phone land elsewhere, as it would for real. (The real
tool takes ~ from the account database, not from $HOME, which a test cannot move: tests never rely on that default.)
Every call is logged with the ANDROID_AVD_HOME it was given and where the AVD went.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def avd_home() -> Path:
    named = os.environ.get("ANDROID_AVD_HOME")
    if named is not None and Path(named).is_dir():
        return Path(named)
    user = os.environ.get("ANDROID_USER_HOME")
    home = Path(user) / "avd" if user is not None else Path("~/.android/avd").expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home


def main(argv: list[str]) -> int:
    state = json.loads(Path(os.environ["FAKE_ADB_STATE"]).read_text())
    entry = {"argv": argv, "stdin": sys.stdin.read(), "avd_home": os.environ.get("ANDROID_AVD_HOME")}
    if argv[:2] == ["create", "avd"]:
        name = argv[argv.index("-n") + 1]
        avd = avd_home() / f"{name}.avd"
        avd.mkdir(parents=True)
        (avd / "config.ini").write_text("hw.keyboard = no\nhw.lcd.density = 420\n")
        entry["landed"] = str(avd)
    with open(Path(state["log_dir"]) / "avdmanager.log", "a") as log:
        log.write(json.dumps(entry) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
