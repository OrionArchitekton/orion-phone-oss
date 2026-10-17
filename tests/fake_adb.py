"""Fake adb for tests. Serves synthetic device state from FAKE_ADB_STATE and logs every call.

It reads the JSON state file the tests write, and changes it only for faults that pass with time (a blip that
ends) and for `emu kill`; it never talks to a real phone. Faults: `adb_blip` N (`devices` lists nothing for N
calls), `adb_ghost` N (it lists the stopped phone as offline for N calls), `adb_devices_fail`, `emu_kill_fail`, and
`adb_devices_raw`, the whole text `devices` prints, exactly (to end a listing in a lone CR with no newline after it).
A device's UI dump is a real file beside the log, so what one call leaves behind the next call reads. The device
fault `ui_dump` = "silent-fail" makes `uiautomator dump` exit 0 and write nothing, as the real tool does (printing why on
stderr, `ui_dump_said` if set, NULL_ROOT if not), `ui_dump_failures` N does that for the next N dumps only, as a window
that is not ready yet does, and the
fault `ui_dump_stream` = "endless" makes reading the dump never end, as a hostile screen's could (about 10 MiB a
second, and only a kill stops it; as a last resort it stops by itself after 512 MiB). The device fields `crypto_type`
(what `getprop ro.crypto.type` prints, "file" unless set) and `debuggable` (`ro.debuggable`, "0" unless set) are what
the booted image reports: the safe defaults are a Google Play image's, and tests set the unsafe ones. `boot_end` (after `sys.boot_completed`)
and `image_end` (after `ro.crypto.type` and `ro.debuggable`) are what the device prints after a property value ("\n" unless
set: a pty makes it CR LF, and a test may want worse, a lone CR or a CR after the newline).

`dumpsys activity activities` prints the dump in the shape the pinned Android 16 image gives it (see `activity_dump`): the
device field `keyguard` ("true", "false", or "unknown" for a dump with no KeyguardController block at all) is the lock state
in that block; the display policy, which that image prints in the supervisor's dump before the block, prints
`isKeyguardShowing=` too (the block's value, and false when there is no block; the device field `display_policy`, "true" or
"false", prints another); and `dump_extra` and `dump_after` are text an app controls, printed where the real dump prints app
text: among the activity records, before the block, and after it.

`dumpsys lock_settings` prints the screen lock in the shape of the pinned Android 16 image (see `lock_settings_dump`): the
device field `credential` is user 0's `CredentialType` ("PIN" unless set, so that a locked phone is protected as a test
expects; "NONE" is a phone with no screen lock, as a new one boots), and `lock_settings` is the whole text exactly as the
phone prints it, for the reports a reader must refuse.

The fake stands for the one adb server orion-phone runs, which listens on `tcp:localhost:5037` (SERVER_SOCKET), and serves
a call only when it goes there, as the real adb decides it (see `endpoint`): `-L SOCKET` ahead of the call's own words names
the server and the environment is then not read; with no `-L`, a variable of the environment names it (`ADB_SERVER_SOCKET`,
else `ANDROID_ADB_SERVER_ADDRESS` and `ANDROID_ADB_SERVER_PORT`), and with none set the default is the managed server. A call
that goes anywhere else fails at once with exit 1 (the real adb, under WSL2's mirrored networking, hangs for its whole time
limit), whatever it asks for: `emu kill` too, which the real adb sends to the emulator's console without the server, so that
a test sees which calls name the server's address. `adb_raw.log` holds every call exactly as it was made, `-L` and all;
`adb.log` holds the calls that were served, with their `-L` taken off, so a test reads a call as it always has.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
import zlib
from pathlib import Path

UI_XML = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
    '<hierarchy rotation="0">'
    '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
    'content-desc="" clickable="false" bounds="[0,0][1080,2400]">'
    '<node index="0" text="Search" resource-id="com.example:id/search" '
    'class="android.widget.EditText" content-desc="" clickable="true" bounds="[40,200][1040,320]" />'
    '<node index="1" text="" resource-id="" class="android.widget.ImageButton" '
    'content-desc="Settings" clickable="true" bounds="[900,60][1040,180]" />'
    "</node></hierarchy>"
)


def png_1px() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x10\x20\x30\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def raw_frame(blank: bool) -> bytes:
    width, height = 4, 4
    header = struct.pack("<IIII", width, height, 1, 0)
    if blank:
        return header + b"\x00\x00\x00\xff" * (width * height)
    return header + bytes((i * 37) % 256 for i in range(width * height * 4))


GHOST_SERIAL = "emulator-5556"  # the phone the `adb_ghost` fault keeps listing after it is gone
SERVER_SOCKET = "tcp:localhost:5037"  # where the adb server the fake stands for listens: the only endpoint it serves


def keyguard_block(value: str) -> str:
    """The KeyguardController block as `ActivityTaskSupervisor.dump` prints it with the prefix "  " (AOSP, android-14 and
    android-15 as read: KeyguardController.dump, and KeyguardDisplayState.dumpStatus for the per-display line, which is one
    column less deep than the lines around it, because it adds one space to the prefix where they add two), ending with
    the empty line that closes it. The pinned Android 16 image prints it with the same indentation (header two columns,
    fields four, the per-display line three, an empty line to close it), and prints the per-display line exactly as that
    image does."""
    return (
        "  KeyguardController:\n"
        f"    mKeyguardShowing={value}\n"
        "    mAodShowing=false\n"
        "    mKeyguardGoingAway=false\n"
        f"   KeyguardShowing={value} AodShowing=false KeyguardGoingAway=false DismissalRequested=false  Occluded=false "
        "DismissingKeyguardActivity=null TurnScreenOnActivity=null at display=0\n"
        "    mDismissalRequested=false\n"
        "\n"
    )


def display_dump(dev: dict) -> str:
    """What the pinned Android 16 image's supervisor dump holds before the KeyguardController block: the display's own dump,
    with the display policy in it (and the insets policy after it, each closed by an empty line). The display policy
    prints the lock state under another name, `isKeyguardShowing=`, which no reader may take for the block's own. Here it
    is the block's value, and false when there is no block (`keyguard` "unknown"), so that a reader that counted it would
    read a dump with no block as unlocked. The device field `display_policy` prints another value, to show it decides
    nothing."""
    shown = dev.get("display_policy", "false" if dev["keyguard"] == "unknown" else dev["keyguard"])
    return (
        "  Display: mDisplayId=0 (organized)\n"
        "  DisplayPolicy\n"
        "    mCarDockEnablesAccelerometer=true mDeskDockEnablesAccelerometer=true\n"
        "    mKeyguardDrawComplete=true mWindowManagerDrawComplete=true\n"
        "    mShowingDream=false mDreamingLockscreen=false\n"
        f"    isKeyguardShowing={shown}\n"
        "    mNavBarOpacityMode=2\n"
        "\n"
        "  InsetsPolicy\n"
        "    status: WINDOW_STATE_SHOWING\n"
        "    nav: WINDOW_STATE_SHOWING\n"
        "\n"
    )


def activity_dump(dev: dict) -> str:
    """`dumpsys activity activities` in the order the pinned Android 16 image prints it (AOSP,
    ActivityTaskManagerService.dumpActivitiesLocked): the header, the activity records (where an app's own text,
    `dump_extra`, appears: an intent's data, a task label), the resumed activity, then ActivityTaskSupervisor.dump, which
    holds the display's own dump (see `display_dump`) and then the KeyguardController block (`keyguard`; none when it is
    "unknown"), followed by more state, some of it activity records again (`dump_after`). The lines around the block
    that were not checked against the sources (the task listing, the lock-task state) are stand-ins."""
    block = "" if dev["keyguard"] == "unknown" else keyguard_block(dev["keyguard"])
    return (
        "ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n"
        "Display #0 (activities from top to bottom):\n"
        "  * Task{1 #1 type=standard A=10001:com.example.notes}\n"
        "    * Hist #0: ActivityRecord{1a2b3c4 u0 com.example.notes/.Main t1}\n"
        "        packageName=com.example.notes processName=com.example.notes\n"
        "        Intent { act=android.intent.action.MAIN cmp=com.example.notes/.Main }\n"
        + dev.get("dump_extra", "")
        + "  ResumedActivity: ActivityRecord{1a2b3c4 u0 com.example.notes/.Main t1}\n"
        "\n"
        "ActivityTaskSupervisor state:\n"
        "  topDisplayFocusedRootTask=Task{1 #1 type=standard A=10001:com.example.notes}\n"
        + display_dump(dev)
        + block
        + "  LockTaskController:\n"
        "    mLockTaskModeState=NONE\n"
        "  mCurTaskIdForUser={0=1}\n"
        "  mUserRootTaskInFront={}\n"
        "  mVisibilityTransactionDepth=0\n"
        "  isHomeRecentsComponent=true\n"
        "  mNoHistoryActivities=[]\n"
        "\n"
        + dev.get("dump_after", "")
    )


def lock_settings_dump(dev: dict) -> str:
    """`dumpsys lock_settings` as the pinned Android 16 image prints it (read on that image with no screen lock and with a
    PIN; the ids are synthetic). The report holds the lock settings service's state per user, `User State:` and one block
    for each, and then its keys and its storage. User 0's block says `CredentialType: NONE` with no screen lock (and then
    `IsLockScreenDisabled: true`, which a PIN's block does not print) and `CredentialType: PIN` with one; `Quality` is 0
    either way. The blocks end with a line of exactly two spaces. Under `Storage:` stands a line that begins like the user's
    block, `  User 0 [/data/system_de/0/spblob]:`, and it is not the user's block."""
    credential = dev.get("credential", "PIN")
    disabled = "    IsLockScreenDisabled: true\n" if credential == "NONE" else ""
    return (
        "Current lock settings service state:\n"
        "\n"
        "User State:\n"
        "  User 0\n"
        "    LSKF-based SP protector ID: 7c1d9e2b4a6f8053\n"
        "    LSKF last changed: 2026-10-04 10:43:13 (previous protector: 3b5d7f9102e4c6a8)\n"
        "    SID: 0000000000000000\n"
        "    Quality: 0\n"
        f"    CredentialType: {credential}\n"
        + disabled
        + "    SeparateChallenge: true\n"
        "    Metrics: known\n"
        "  \n"
        "Keys in namespace:\n"
        "  synthetic_password_7c1d9e2b4a6f8053\n"
        "  \n"
        "Storage:\n"
        "  User 0 [/data/system_de/0/spblob]:\n"
        "        60 2026-10-04 09:36:51 0000000000000000.e0\n"
        "     16384 2026-10-04 09:36:51 c4e8a2f60b1d3975.secdis\n"
        "  \n"
    )


def save(state_path: Path, state: dict) -> None:
    tmp_path = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")  # per process: no shared temp file
    tmp_path.write_text(json.dumps(state))
    os.replace(tmp_path, state_path)


UI_PATH = "/data/local/tmp/orion_ui.xml"


def _dump_file(state: dict, serial: str) -> Path:
    """The device's UI dump file, kept beside the log so it persists between calls like a real file."""
    return Path(state["log_dir"]) / f"{serial}-orion_ui.xml"


# What uiautomator prints on stderr, exiting 0, when Android's accessibility bridge has no window to give it (measured on the
# pinned image, right after an app's start).
NULL_ROOT = "ERROR: null root node returned by UiTestAutomationBridge.\n"
# orion-phone's dump command: it prints its marker, new for every call, only once the dump file has content
UI_DUMP_COMMAND = re.compile(
    rf"rm -f {re.escape(UI_PATH)} && uiautomator dump {re.escape(UI_PATH)} 2>&1 && test -s {re.escape(UI_PATH)} "
    r"&& echo (orion-ui-dump-written-[0-9a-f]{16}) \|\| true"
)


def _uiautomator_dump(state: dict, serial: str, dev: dict) -> None:
    # Like the real tool: "could not get idle state" leaves the old file alone and still exits 0.
    if dev.get("ui_dump") != "silent-fail":
        _dump_file(state, serial).write_text(dev.get("ui_xml", UI_XML))


def _stream_forever() -> None:
    """A dump that never ends. The pace keeps a reader that does not stop paying in time, not in memory."""
    chunk = b"<node text=" + b"x" * (65536 - 11)
    for _ in range(8192):
        try:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        except BrokenPipeError:  # the reader gave up: so does the stream
            raise SystemExit(1)
        time.sleep(0.006)


def _cat_dump(state: dict, serial: str, dev: dict) -> None:
    if dev.get("ui_dump_stream") == "endless":
        _stream_forever()
        return
    dump = _dump_file(state, serial)
    if dump.exists():
        sys.stdout.buffer.write(dump.read_bytes())
    else:  # exec-out merges stderr into stdout and exits 0
        sys.stdout.write(f"cat: {UI_PATH}: No such file or directory\n")


def endpoint(argv: list[str]) -> tuple[str, list[str]]:
    """The socket a call goes to, and the call without the `-L SOCKET` that named it. A leading `-L SOCKET` names the server
    and the environment is not read; with none, the endpoint variables do: `ADB_SERVER_SOCKET`, or `ANDROID_ADB_SERVER_ADDRESS`
    (localhost by default) and `ANDROID_ADB_SERVER_PORT` (5037 by default) together. Observed with platform-tools adb 1.0.41,
    which follows each of the three when it is given no `-L` and ignores all three when it is given one. The tests set one
    variable at a time, so which of them would win over another is not modeled."""
    if len(argv) > 1 and argv[0] == "-L":
        return argv[1], argv[2:]
    named = os.environ.get("ADB_SERVER_SOCKET")
    if named:
        return named, argv
    host = os.environ.get("ANDROID_ADB_SERVER_ADDRESS") or "localhost"
    port = os.environ.get("ANDROID_ADB_SERVER_PORT") or "5037"
    return f"tcp:{host}:{port}", argv


def main(argv: list[str]) -> int:
    state_path = Path(os.environ["FAKE_ADB_STATE"])
    state = json.loads(state_path.read_text())
    log_dir = Path(state["log_dir"])
    with open(log_dir / "adb_raw.log", "a") as raw:
        raw.write(json.dumps(argv) + "\n")
    socket_spec, argv = endpoint(argv)
    if socket_spec != SERVER_SOCKET:  # nothing listens there: the real adb is refused, or (under WSL2) hangs
        sys.stderr.write(f"error: cannot connect to daemon at {socket_spec}: the server listens on {SERVER_SOCKET}\n")
        return 1
    with open(log_dir / "adb.log", "a") as log:
        log.write(json.dumps(argv) + "\n")
    devices = state["devices"]
    if argv == ["devices"]:
        if state.get("adb_devices_fail"):
            sys.stderr.write("error: cannot connect to daemon (test-injected)\n")
            return 1
        listed = dict(devices)
        if state.get("adb_blip", 0) > 0:  # an adb server that just restarted lists nothing for a moment
            state["adb_blip"] -= 1
            save(state_path, state)
            listed = {}
        elif state.get("adb_ghost", 0) > 0:  # a stopped phone that adb still lists, offline, for a moment
            state["adb_ghost"] -= 1
            save(state_path, state)
            listed.setdefault(GHOST_SERIAL, {"state": "offline"})
        if "adb_devices_raw" in state:
            sys.stdout.write(state["adb_devices_raw"])
            return 0
        rows = "".join(f"{serial}\t{dev['state']}\n" for serial, dev in listed.items())
        rows += "".join(row + "\n" for row in state.get("adb_raw_rows", []))  # rows exactly as a test wants them printed
        sys.stdout.write("List of devices attached\n" + rows + "\n")
        return 0
    if len(argv) < 4 or argv[0] != "-s":
        sys.stderr.write(f"fake adb: unsupported call {argv}\n")
        return 2
    serial, verb, command = argv[1], argv[2], argv[3]
    dev = devices.get(serial)
    if dev is None or dev["state"] != "device":
        sys.stderr.write(f"error: device '{serial}' not found\n")
        return 1
    if dev.get("fail") and command.startswith(dev["fail"]):  # adbd dropped while `adb devices` still lists it
        sys.stderr.write("error: closed\n")
        return 1
    if verb == "emu" and command == "kill":
        if state.get("emu_kill_fail"):
            sys.stderr.write("error: emulator console unreachable (test-injected)\n")
            return 1
        devices.pop(serial)
        save(state_path, state)
        sys.stdout.write("OK: killing emulator, bye bye\nOK\n")
        return 0
    if verb == "exec-out":
        if command == "screencap":
            if dev["screen"] == "garbled":  # exec-out merges the device's stderr into stdout and exits 0
                sys.stdout.write("/system/bin/sh: screencap: inaccessible or not found\n")
            elif dev["screen"] != "empty":
                sys.stdout.buffer.write(raw_frame(dev["screen"] == "blank"))
            return 0
        if command == "screencap -p":
            if dev["screen"] == "garbled":
                sys.stdout.write("/system/bin/sh: screencap: inaccessible or not found\n")
            elif dev["screen"] != "empty":
                sys.stdout.buffer.write(png_1px())
            return 0
        if command == f"cat {UI_PATH}":
            _cat_dump(state, serial, dev)
            return 0
        if command == f"cat {UI_PATH} && rm -f {UI_PATH}":  # a cat that is killed never reaches the rm, as on a phone
            _cat_dump(state, serial, dev)
            _dump_file(state, serial).unlink(missing_ok=True)
            return 0
    if verb == "shell":
        if command == "getprop sys.boot_completed":
            sys.stdout.write((dev["boot_text"] if "boot_text" in dev else "1" if dev["booted"] else "") + dev.get("boot_end", "\n"))
            return 0
        if command == "getprop ro.crypto.type":  # an unset property prints an empty line, as getprop does
            sys.stdout.write(dev.get("crypto_type", "file") + dev.get("image_end", "\n"))
            return 0
        if command == "getprop ro.debuggable":
            sys.stdout.write(dev.get("debuggable", "0") + dev.get("image_end", "\n"))
            return 0
        if command == "dumpsys activity activities":
            sys.stdout.write(activity_dump(dev))
            return 0
        if command == "dumpsys lock_settings":
            sys.stdout.write(dev["lock_settings"] if "lock_settings" in dev else lock_settings_dump(dev))
            return 0
        if command == f"rm -f {UI_PATH}":
            _dump_file(state, serial).unlink(missing_ok=True)
            return 0
        if command == f"uiautomator dump {UI_PATH}":
            _uiautomator_dump(state, serial, dev)
            return 0
        if command == f"rm -f {UI_PATH} && uiautomator dump {UI_PATH}":
            _dump_file(state, serial).unlink(missing_ok=True)
            _uiautomator_dump(state, serial, dev)
            return 0
        dumped = UI_DUMP_COMMAND.fullmatch(command)
        if dumped:
            _dump_file(state, serial).unlink(missing_ok=True)  # the tool's own words are on its stderr, merged here
            if dev.get("ui_dump_failures", 0) > 0:  # a window that is not ready yet, for that many dumps
                dev["ui_dump_failures"] -= 1
                save(state_path, state)
                sys.stdout.write(dev.get("ui_dump_said", NULL_ROOT))  # exit 0, nothing written: `test -s` fails
                return 0
            if dev.get("ui_dump") == "silent-fail":
                sys.stdout.write(dev.get("ui_dump_said", NULL_ROOT))
                return 0
            _uiautomator_dump(state, serial, dev)
            sys.stdout.write(f"UI hierchary dumped to: {UI_PATH}\n{dumped.group(1)}\n")  # the real tool's spelling
            return 0
        if command == "input keyevent KEYCODE_WAKEUP" and dev.get("wake_unlocks"):  # a lock screen a wake would dismiss
            dev["keyguard"] = "false"
            save(state_path, state)
            return 0
        if command.startswith(("input ", "am start ", "monkey ")):
            return 0
    sys.stderr.write(f"fake adb: unsupported call {argv}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
