"""Fake `systemctl --user` for tests: logs calls; starting an inactive phone unit is a new run of it (a new InvocationID)
that boots its phone to the lock screen; starting an active unit does nothing.

Faults, set in the shared state file by the tests: `systemctl_fail` (that verb exits 1), `systemctl_hang` (that verb
sleeps `hang_seconds` and changes nothing), `systemctl_kill_parent` (that verb SIGKILLs its caller before it changes
anything), `stop_ineffective` (stop exits 0, but a phone its unit did not start keeps running), `start_inactive` (start
exits 0, but the unit does not become active), `retain_id_after_stop` (a stopped unit reports itself failed and keeps
its last InvocationID, as systemd does for a failed unit), `stop_after_show` (once: the unit stops right after a
`show` that answered it was running, as a session cap or a crash would end it between a caller's look and its next step),
`reload_ineffective` (daemon-reload exits 0 but the manager loads nothing, so what was pending stays pending), and
`reload_applies_then_fails` (daemon-reload loads everything, then exits 1: the answer was lost, or our wait ran out and the
manager went on). `systemctl_fail_on` and `systemctl_hang_on` ([verb, unit]) fail or hang that verb for that unit only, and
`fail_reload_from` (N) fails the Nth daemon-reload and every one after it, and `ineffective_reload_from` (N) makes the Nth and
every later one exit 0 and load nothing, so that a test can fail setup's reload and not the install's, and
`show_fails_from_reload` (N) fails every `show` once N reloads have been made, so that a look after a reload cannot read a unit.
`unit_pids` (unit ->
the pid of a process group a test started as that unit's run, for the tests that need a
stop to reach a real supervisor) makes `stop` do what systemd does with KillMode=control-group: wait `stop_delay` seconds
(the caller holds whatever it holds meanwhile), SIGTERM the group, give it `stop_wait` seconds (default 20) to end, and
otherwise SIGKILL it, noting the unit in `stop_needed_kill`.

The adb server's unit is Type=simple, which systemd counts as started the moment adb is spawned, so `start` exits 0 at once
whatever adb then does. The fake models what a real host showed (adb 1.0.41, systemd 255, another server holding port 5037):
the unit's adb aborted about 0.7 s after it was spawned (`could not install *smartsocket* listener: Address already in use`,
SIGABRT), and with RestartPreventExitStatus=SIGABRT the unit stays failed. `server_aborts_after` (seconds) makes the server's
unit show `ActiveState=failed` that long after its start (0: failed from the first look); `server_never_binds` has its adb alive
and never listening. The port itself is the tests' (see `server_listening`, and ServerPort in conftest, which opens it).

An enable or a disable with `--no-reload` marks the unit-file state outdated for the whole manager (probed on a real host,
systemd 255, with a throwaway unit that has an [Install] section: phone b went from `NeedDaemonReload=no` to `yes`, and so did
phone a; a daemon-reload brought both back to `no`; a disable with `--no-reload` made both `yes` again). The fake keeps that as
`links_changed`: a daemon-reload clears it, and while it is set `show` says `NeedDaemonReload=yes` for every unit, whatever else
it knows of the unit. Without `--no-reload` the verb reloads the manager itself, which the fake does not model: no code here
runs one.

A unit the manager has no file for (`not_found`, see below) is what systemd 255 answers for one that is not installed
(probed with a transient name nothing had made; its `Restart=no` is the default and was not among what was probed): `show` exits
0 and prints the defaults, `TimeoutStopUSec=1min 30s`,
`RuntimeMaxUSec=infinity`, `RuntimeRandomizedExtraUSec=0`, an empty `Environment=`, `KillMode=control-group`,
`SendSIGKILL=yes`, `LoadState=not-found`, `ActiveState=inactive`, `NeedDaemonReload=no` and an empty `InvocationID=`; `stop` exits 5 with `Failed to stop X: Unit X not loaded.`; `start` exits
5 with `Failed to start X: Unit X not found.`; and `enable` exits 1 with `Failed to enable unit: Unit file X does not
exist.`. A daemon-reload that finds the unit's file in ORION_PHONE_UNIT_DIR loads it. A process that outlives its unit file
(started by hand while the unit is `not_found`) is a running unit: it can be stopped, and `ActiveState` says active (how
systemd prints a unit that is running and not found was not probed, since that needs a reload of a real user manager).

The state key `show_line_end` is what `show` prints after each line ("\n" unless set: systemd never prints a carriage
return, so a test sets `\r\n`, which a reader must take for a line end, or a lone CR, a doubled one, or a CR after the
newline, which it must not). `show_last` names the property printed last, whatever systemd's order, and `show_last_end` is
what follows that last line (`show_line_end` unless set): a lone CR there, with no newline after it, is the end of the output
and not a line end, which a reader must not take for one.

`show` also answers Restart, RuntimeMaxUSec, RuntimeRandomizedExtraUSec, KillMode, TimeoutStopUSec, SendSIGKILL, Environment,
LimitCORE, LimitCORESoft and NeedDaemonReload in the format real systemd
255 prints (probed on a host: `LimitCORE=1` and `LimitCORESoft=1` for a unit with LimitCORE=1, `infinity` and `0` for one
that names none; `RuntimeMaxUSec=2h` for a 7200 s cap, `1h 30min`, `1d`, `45s`, `infinity` for none;
`RuntimeRandomizedExtraUSec=0` when there is none, `1h`, `1d`, `1min 30s`; `Environment=` as space separated assignments,
each in double quotes with backslash escapes when it holds a space, a quote, a backslash, a dollar sign or a backtick (see
`quote_assignment`); `NeedDaemonReload=no` or `yes`; `KillMode=control-group` (also `process`, `mixed`, `none`, as set);
`TimeoutStopUSec=1min 30s` for the manager's default of 90 s, `30s`, `59s`, `1min`, `1h`, `infinity`;
`SendSIGKILL=yes` (the default) or `no`; `Restart=no` for a locked phone's unit, `Restart=on-failure` for an open phone's and
`Restart=always` for the adb server's (read on a host, systemd 255: with several units in one call it prints one `Restart=` line
for each and a blank line between them, and `--value` prints the bare `no`; the fake does the same for every property); systemd
prints in its own order, TimeoutStopUSec, RuntimeMaxUSec,
RuntimeRandomizedExtraUSec, Environment, KillMode, SendSIGKILL, LoadState, ActiveState, NeedDaemonReload, InvocationID,
whatever order is asked for; `LoadState` is `loaded` for a unit the manager has, or what
`load_text` says). What it
answers is what the manager has loaded of the unit (see `loaded_unit`): its cap in seconds, its extra time in seconds (a
drop-in's RuntimeRandomizedExtraSec, which install-units does not write, so a reload keeps it), its kill mode (`kill_mode`),
its stop time in seconds (`stop_timeout`) and whether a stop that runs out of time kills what is left (`send_sigkill`, yes or
no; a unit file that names none of these has the defaults, control-group, 90 s and yes, and a reload takes all three from the
file), whether the manager starts it again when it ends (`restart`: `no` for a locked phone's unit and `on-failure` for an open
phone's, as install-units writes them; `always` for the adb server's; a unit file that names none has the default, `no`, and a
reload takes it from the file), whether a reload is pending, and its environment: the caller's own ORION_PHONE_CONFIG,
ORION_PHONE_STATE, ANDROID_AVD_HOME and DISPLAY, as a
unit installed from that environment carries them, except that `env_state` (None for none) is the ORION_PHONE_STATE it
carries instead, and a reload takes that from the unit file. A test changes all that with `Env.set_loaded_unit`;
`cap_text`, `extra_text`, `kill_mode_text`, `stop_timeout_text`, `send_sigkill_text`, `restart_text`, `reload_text`,
`state_text`, `load_text` and `environment_text` are printed verbatim instead of the cap, the extra time, the kill mode, the
stop time, SendSIGKILL, Restart, the reload state, ActiveState, LoadState and the environment, and `omit` lists properties
`show` leaves out. `pending` is what a drop-in on disk says that the manager has not loaded yet (a dict with `cap`, `extra`,
`kill_mode`, `stop_timeout`, `send_sigkill` and/or `restart`): until a daemon-reload `show` says NeedDaemonReload=yes and
still prints the old values, and the
reload loads it into the unit's definition whether the unit is running or not (systemd re-arms a running service's
runtime limit from the reloaded one), over what the unit file says, as a drop-in does.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

SERIALS = {  # c is a phone only the tests that add it to their config have
    "orion-phone-a.service": "emulator-5554", "orion-phone-b.service": "emulator-5556", "orion-phone-c.service": "emulator-5558",
}
VERBS = ("start", "stop", "daemon-reload", "enable", "disable", "show")
SERVER_UNIT = "orion-phone-adb-server.service"
LOCKED = {"state": "device", "booted": True, "screen": "content", "keyguard": "true"}
# The session cap, in seconds, of each unit as install-units writes it for the tests' config: the open phone a has none,
# the locked phone b has its 120 minutes. Until a daemon-reload says otherwise this is what the manager has loaded, so the
# tests that never install units still see units that are right.
INSTALLED_CAPS = {"orion-phone-a.service": None, "orion-phone-b.service": 7200, "orion-phone-c.service": 7200}
# and the Restart of each, as install-units writes it: an open phone's unit restarts on failure, a locked phone's never, and the
# adb server's always; a unit that is none of these (or names none) has the manager's default, no
INSTALLED_RESTARTS = {
    "orion-phone-a.service": "on-failure", "orion-phone-b.service": "no", "orion-phone-c.service": "no",
    "orion-phone-adb-server.service": "always",
}
DEFAULT_RESTART = "no"
RESTART_LINE = re.compile(r"^Restart=(\S+)$", re.MULTILINE)
CAP_LINE = re.compile(r"^RuntimeMaxSec=([0-9]+)$", re.MULTILINE)
KILL_LINE = re.compile(r"^KillMode=(\S+)$", re.MULTILINE)
STOP_LINE = re.compile(r"^TimeoutStopSec=([0-9]+)$", re.MULTILINE)
SIGKILL_LINE = re.compile(r"^SendSIGKILL=(\S+)$", re.MULTILINE)
# what a unit that names none of them has: the manager's defaults
DEFAULT_KILL_MODE, DEFAULT_STOP_TIMEOUT, DEFAULT_SEND_SIGKILL = "control-group", 90, "yes"
# The core limits (hard, soft) as `show` prints them, LimitCORE and LimitCORESoft: what a unit that names none has (probed on
# systemd 255: infinity and 0), and what a locked phone's unit, written with LimitCORE=1, has (1 and 1), until a reload says
# otherwise.
CORE_LINE = re.compile(r"^LimitCORE=(\S+)$", re.MULTILINE)
DEFAULT_CORE = ("infinity", "0")
INSTALLED_CORES = {"orion-phone-b.service": ("1", "1"), "orion-phone-c.service": ("1", "1")}
VERBATIM = (
    "cap_text", "reload_text", "environment_text", "kill_mode_text", "stop_timeout_text", "send_sigkill_text", "restart_text",
    "core_text", "core_soft_text",
)


def loaded_unit(state: dict, unit: str) -> dict:
    """What the manager has loaded of a unit: `cap` (seconds, None for no cap), `restart` and `reload_pending` (the file changed
    since)."""
    restart = INSTALLED_RESTARTS.get(unit, DEFAULT_RESTART)
    return state.setdefault("loaded", {}).setdefault(
        unit, {"cap": INSTALLED_CAPS.get(unit), "restart": restart, "reload_pending": False}
    )


def reload_units(state: dict) -> None:
    """daemon-reload: the manager reads the unit files and drop-ins again. A phone unit that has a file in
    ORION_PHONE_UNIT_DIR is now loaded as that file says (its RuntimeMaxSec, or no cap), then whatever a drop-in held
    pending is loaded over it, and no reload is pending any more. The loaded values are those of the unit's definition, so
    a running unit takes them too. With `reload_ineffective` nothing is loaded and everything stays pending."""
    if state.get("reload_ineffective"):
        return
    state.pop("links_changed", None)  # the manager has read the unit-file state again: nothing is outdated any more
    unit_dir = os.environ.get("ORION_PHONE_UNIT_DIR")
    for unit in SERIALS:
        entry = loaded_unit(state, unit)
        entry["reload_pending"] = False
        path = Path(unit_dir) / unit if unit_dir else None
        if path is not None and path.is_file():
            entry.pop("not_found", None)  # the manager has read the file: the unit exists now
            found = CAP_LINE.search(path.read_text())
            entry["cap"] = int(found.group(1)) if found else None
            named = STATE_LINE.search(path.read_text())  # a unit file writes % and backslash doubled
            entry["env_state"] = named.group(1).replace("\\\\", "\\").replace("%%", "%") if named else None
            killed, stop, forced = (
                KILL_LINE.search(path.read_text()), STOP_LINE.search(path.read_text()), SIGKILL_LINE.search(path.read_text())
            )
            entry["kill_mode"] = killed.group(1) if killed else DEFAULT_KILL_MODE
            entry["stop_timeout"] = int(stop.group(1)) if stop else DEFAULT_STOP_TIMEOUT
            entry["send_sigkill"] = forced.group(1) if forced else DEFAULT_SEND_SIGKILL
            restarted = RESTART_LINE.search(path.read_text())
            entry["restart"] = restarted.group(1) if restarted else DEFAULT_RESTART
            cored = CORE_LINE.search(path.read_text())  # one value sets both limits; `soft:hard` sets each
            soft, _, hard = cored.group(1).partition(":") if cored else (DEFAULT_CORE[1], "", DEFAULT_CORE[0])
            entry["core"], entry["core_soft"] = (hard or soft), soft
            for text in VERBATIM:  # whatever a test had the manager print is gone with the old load
                entry.pop(text, None)
        pending = entry.pop("pending", {})
        for key in ("cap", "extra", "kill_mode", "stop_timeout", "send_sigkill", "restart", "core", "core_soft"):
            if key in pending:
                entry.pop(f"{key}_text", None)
        entry.update(pending)


def timespan(seconds: int | None) -> str:
    """A time span as `systemctl show` prints it: the largest units first, none for zero parts ("2h", "1h 30min", "1d")."""
    if seconds is None:
        return "infinity"
    parts = []
    for name, size in (("w", 604800), ("d", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{name}")
    return " ".join(parts) or "0"


QUOTED_ASCII = frozenset(" !\"$&'()*;<>?[\\`|")  # and every control character, and DEL
ESCAPED = {
    "\\": "\\\\", '"': '\\"', "$": "\\$", "`": "\\`", "\a": "\\a", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\v": "\\v",
    "\f": "\\f", "\r": "\\r",
}


def quote_assignment(assignment: str) -> str:
    """One variable of an Environment property as `systemctl show` prints it (probed on systemd 255, one character at a
    time): bare, unless it holds a space, one of the ASCII characters !"$&'()*;<>?[\\` and |, a control character or DEL;
    then the whole assignment is in double quotes, with a backslash put before each backslash, double quote, dollar sign
    and backtick, a C style escape (a, b, t, n, v, f, r) or a three digit octal number for a control character or DEL.
    A percent sign, # ] ^ { } ~ and everything that is not ASCII (U+0085, U+2028 and U+2029 included) are printed as they
    are, in a bare word too."""
    def control(c: str) -> bool:
        return ord(c) < 32 or ord(c) == 127

    if not any(c in QUOTED_ASCII or control(c) for c in assignment):
        return assignment
    return '"' + "".join(ESCAPED.get(c, f"\\{ord(c):03o}" if control(c) else c) for c in assignment) + '"'


STATE_LINE = re.compile(r'^Environment="ORION_PHONE_STATE=(.*)"$', re.MULTILINE)


def environment_value(entry: dict) -> str:
    """The Environment property: the caller's variables as a unit installed from them carries them."""
    if "environment_text" in entry:
        return entry["environment_text"]
    assignments = []
    for name in ("ORION_PHONE_CONFIG", "ORION_PHONE_STATE", "ANDROID_AVD_HOME", "DISPLAY"):
        value = entry["env_state"] if name == "ORION_PHONE_STATE" and "env_state" in entry else os.environ.get(name)
        if value is not None:
            assignments.append(f"{name}={value}")
    return " ".join(quote_assignment(each) for each in assignments)


def aborted(state: dict, unit: str) -> bool:
    """Has the adb server's adb aborted? With another server holding its port it does, `server_aborts_after` seconds after the
    unit was started (a real host: about 0.7 s, SIGABRT, a core dump), and the unit stays failed. `start` had exited 0 long
    before: the unit is Type=simple."""
    after, started = state.get("server_aborts_after"), state.get("started_at", {}).get(unit)
    return (
        unit == SERVER_UNIT and after is not None and started is not None and unit in state.get("invocations", {})
        and time.time() - started >= after
    )


def server_listening(state: dict) -> bool:
    """Is the adb server's port open? It is while the server's unit is active, its adb having bound the port, unless that adb
    never binds it (`server_never_binds`) or has aborted."""
    return (
        SERVER_UNIT in state.get("invocations", {}) and not aborted(state, SERVER_UNIT) and not state.get("server_never_binds")
    )


def start_unit(state: dict, unit: str) -> None:
    """A new run of an inactive unit: its own InvocationID, and its phone boots to the lock screen."""
    runs = state.setdefault("invocations", {})
    if unit in runs and aborted(state, unit):  # a failed unit that is started again is a new run, which aborts again
        runs.pop(unit)
    if unit in runs or state.get("start_inactive"):
        return
    runs[unit] = uuid.uuid4().hex
    state.setdefault("started_at", {})[unit] = time.time()
    state.setdefault("retired", {}).pop(unit, None)
    serial = SERIALS.get(unit)
    if serial:  # a phone that is already there (one its unit did not start) stays as it is
        state["devices"].setdefault(serial, {**LOCKED, "boot_ts": time.time()})  # boot_ts: when this boot began


def group_is_alive(pid: int) -> bool:
    """Is there a running process with this pid? A zombie, which only waits to be reaped, is not one."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def signal_unit(state: dict, unit: str) -> None:
    """A stop as systemd makes it for a unit whose processes a test started (see `unit_pids`): after `stop_delay`, SIGTERM to
    the unit's whole process group, then up to `stop_wait` seconds for its leader to end, then SIGKILL (and a note)."""
    pid = state.get("unit_pids", {}).get(unit)
    if not pid:
        return
    time.sleep(float(state.get("stop_delay", 0)))
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return  # it has gone already
    deadline = time.time() + float(state.get("stop_wait", 20))
    while group_is_alive(pid) and time.time() < deadline:
        time.sleep(0.02)
    if group_is_alive(pid):
        state.setdefault("stop_needed_kill", {})[unit] = True
        os.killpg(pid, signal.SIGKILL)


def stop_unit(state: dict, unit: str) -> None:
    """The run ends and its phone goes with it, except a phone the unit did not start when `stop_ineffective` says so."""
    last = state.setdefault("invocations", {}).pop(unit, None)
    if last and state.get("retain_id_after_stop"):
        state.setdefault("retired", {})[unit] = last
    serial = SERIALS.get(unit)
    if serial and not state.get("stop_ineffective"):
        state["devices"].pop(serial, None)


def show(state: dict, args: list[str]) -> str:
    """`show -p NAME ... [--value] UNIT ...`. Properties print in systemd's order, not the caller's, and several units print one
    block each with a blank line between them (read on a real host, systemd 255)."""
    wanted: list[str] = []
    units: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-p":  # the word after it is a property
            wanted.append(args[i + 1])
            i += 1
        elif not args[i].startswith("-"):
            units.append(args[i])
        i += 1
    return "\n".join(show_unit(state, unit, wanted, "--value" in args) for unit in units)


def show_unit(state: dict, unit: str, wanted: list[str], value_only: bool) -> str:
    """What `show` prints for one unit: its `wanted` properties, as `NAME=value` lines or as bare values."""
    runs, retired = state.get("invocations", {}), state.get("retired", {})
    entry = loaded_unit(state, unit)
    if entry.get("not_found"):  # a unit with no file: systemd answers for it, with its defaults (probed), and exits 0
        values = {
            "Restart": DEFAULT_RESTART,
            "TimeoutStopUSec": timespan(DEFAULT_STOP_TIMEOUT),
            "RuntimeMaxUSec": "infinity",
            "RuntimeRandomizedExtraUSec": "0",
            "Environment": "",
            "LimitCORE": DEFAULT_CORE[0],
            "LimitCORESoft": DEFAULT_CORE[1],
            "KillMode": DEFAULT_KILL_MODE,
            "SendSIGKILL": DEFAULT_SEND_SIGKILL,
            "LoadState": "not-found",
            "ActiveState": "active" if unit in runs else "inactive",
            "NeedDaemonReload": "yes" if state.get("links_changed") else "no",
            "InvocationID": runs.get(unit, ""),
        }
    else:
        values = {  # in the order systemd 255 prints these thirteen (Restart is a service property, ahead of the timeouts)
            "Restart": entry["restart_text"] if "restart_text" in entry else entry.get("restart", DEFAULT_RESTART),
            "TimeoutStopUSec": entry["stop_timeout_text"] if "stop_timeout_text" in entry else timespan(
                entry.get("stop_timeout", DEFAULT_STOP_TIMEOUT)),
            "RuntimeMaxUSec": entry["cap_text"] if "cap_text" in entry else timespan(entry["cap"]),
            "RuntimeRandomizedExtraUSec": entry["extra_text"] if "extra_text" in entry else timespan(entry.get("extra", 0)),
            "Environment": environment_value(entry),
            "LimitCORE": entry["core_text"] if "core_text" in entry else entry.get(
                "core", INSTALLED_CORES.get(unit, DEFAULT_CORE)[0]),
            "LimitCORESoft": entry["core_soft_text"] if "core_soft_text" in entry else entry.get(
                "core_soft", INSTALLED_CORES.get(unit, DEFAULT_CORE)[1]),
            "KillMode": entry["kill_mode_text"] if "kill_mode_text" in entry else entry.get("kill_mode", DEFAULT_KILL_MODE),
            "SendSIGKILL": entry["send_sigkill_text"] if "send_sigkill_text" in entry else entry.get(
                "send_sigkill", DEFAULT_SEND_SIGKILL),
            "LoadState": entry["load_text"] if "load_text" in entry else "loaded",
            "ActiveState": entry["state_text"] if "state_text" in entry else (
                "failed" if aborted(state, unit)
                else "active" if unit in runs else ("failed" if unit in retired else "inactive")),
            "NeedDaemonReload": entry["reload_text"] if "reload_text" in entry else (
                "yes" if state.get("links_changed") or entry["reload_pending"] or entry.get("pending") else "no"),
            "InvocationID": runs.get(unit) or retired.get(unit, ""),
        }
    shown = [name for name in values if name in wanted and name not in entry.get("omit", [])]
    if state.get("show_last") in shown:  # a test wants this one printed last, whatever systemd's order
        shown.append(shown.pop(shown.index(state["show_last"])))
    lines = [values[name] if value_only else f"{name}={values[name]}" for name in shown]
    end = state.get("show_line_end", "\n")  # what ends each line: "\n" unless a test wants a CR LF, or worse
    last_end = state.get("show_last_end", end)  # and the last one, which no newline need follow
    return "".join(line + (last_end if i == len(lines) - 1 else end) for i, line in enumerate(lines))


def not_found_refusal(state: dict, verb: str, words: list[str]) -> tuple[int, str] | None:
    """What systemd 255 does when start, stop or enable is asked of a unit it has not found (probed): the exit status and the
    message, or None when the verb can go ahead. A unit that runs can be stopped even when its file is gone."""
    for unit in (word for word in words if not word.startswith("-")):
        entry = state.get("loaded", {}).get(unit)
        if not entry or not entry.get("not_found"):
            continue
        if verb == "start":
            return 5, f"Failed to start {unit}: Unit {unit} not found."
        if verb == "stop" and unit not in state.get("invocations", {}):
            return 5, f"Failed to stop {unit}: Unit {unit} not loaded."
        if verb in ("enable", "disable"):
            return 1, f"Failed to {verb} unit: Unit file {unit} does not exist."
    return None


def save(state_path: Path, state: dict) -> None:
    tmp_path = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")  # per process: no shared temp file
    tmp_path.write_text(json.dumps(state))
    os.replace(tmp_path, state_path)


def main(argv: list[str]) -> int:
    state_path = Path(os.environ["FAKE_ADB_STATE"])
    state = json.loads(state_path.read_text())
    with open(Path(state["log_dir"]) / "systemctl.log", "a") as log:
        log.write(json.dumps(argv) + "\n")
    if argv[:1] != ["--user"] or len(argv) < 2:
        sys.stderr.write("fake systemctl: expected --user VERB\n")
        return 2
    verb = argv[1]
    if verb not in VERBS:
        sys.stderr.write(f"fake systemctl: unsupported verb {verb}\n")
        return 2
    if state.get("systemctl_fail") == verb:
        sys.stderr.write(f"fake systemctl: {verb} failed (test-injected)\n")
        return 1
    for_unit = [verb, argv[-1]]  # the unit is the last word
    if state.get("systemctl_fail_on") == for_unit:  # that verb, for that unit only
        sys.stderr.write(f"fake systemctl: {verb} failed for {argv[-1]} (test-injected)\n")
        return 1
    if state.get("systemctl_hang") == verb or state.get("systemctl_hang_on") == for_unit:  # outlives its caller's timeout
        time.sleep(float(state.get("hang_seconds", 5)))
        return 0
    if state.get("systemctl_kill_parent") == verb:  # the caller is killed as the verb begins
        os.kill(os.getppid(), signal.SIGKILL)
        return 1
    if verb == "show" and state.get("show_fails_from_reload") is not None and (
        state.get("reloads", 0) >= state["show_fails_from_reload"]
    ):  # the manager no longer answers for units, from the Nth reload on
        sys.stderr.write("fake systemctl: show failed after the reload (test-injected)\n")
        return 1
    if verb == "show":
        unit = argv[-1]
        was_running = unit in state.get("invocations", {})
        sys.stdout.write(show(state, argv[2:]))
        if was_running and state.get("stop_after_show"):  # the answer was true when given; the unit stops after it
            state["stop_after_show"] = False
            stop_unit(state, unit)
            save(state_path, state)
        return 0
    if verb == "daemon-reload":
        number = state["reloads"] = state.get("reloads", 0) + 1  # this is the Nth reload of the run
        if state.get("fail_reload_from") is not None and number >= state["fail_reload_from"]:
            save(state_path, state)
            sys.stderr.write("fake systemctl: daemon-reload failed (test-injected)\n")
            return 1
        if state.get("ineffective_reload_from") is not None and number >= state["ineffective_reload_from"]:
            save(state_path, state)
            return 0  # it answered, and loaded nothing: what was outdated still is
        reload_units(state)
        if state.get("reload_applies_then_fails"):
            save(state_path, state)
            sys.stderr.write("fake systemctl: daemon-reload loaded the units, then failed (test-injected)\n")
            return 1
    refusal = not_found_refusal(state, verb, argv[2:])
    if refusal:
        sys.stderr.write(refusal[1] + "\n")
        return refusal[0]
    if verb in ("enable", "disable") and "--no-reload" in argv[2:]:
        state["links_changed"] = True  # every unit now needs a reload, until the next daemon-reload
    for unit in argv[2:]:
        if verb == "start":
            start_unit(state, unit)
        elif verb == "stop":
            signal_unit(state, unit)
            stop_unit(state, unit)
    save(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
