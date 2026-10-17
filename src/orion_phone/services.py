"""systemd user units and the emulator command line."""

from __future__ import annotations

import contextlib
import ipaddress
import math
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orion_phone.codes import Code, PhoneError, Result
from orion_phone.config import UNIT_UNSAFE, Config, PhoneConfig, git_tree_problem, unit_unsafe
from orion_phone.lease import Lease, LeaseStore
from orion_phone.lines import split_lines

DEFAULT_AVD_HOME = "~/.android/avd"  # where the Android tools keep phones when neither variable below is set


def _given_absolute(variable: str, raw: str) -> Path:
    """The value of a variable that gives the AVD home: it must be an absolute path. The Android tools read the variable as
    it stands, so an empty value or a relative one (a ~ is not expanded for them either) would put phone data in whatever
    directory they run from."""
    if not os.path.isabs(raw):
        raise PhoneError(
            Code.USAGE,
            f"{variable} must be an absolute path (got {raw!r}): the Android tools read it from the environment as it "
            "stands, so a relative path, or one that starts with ~, would put phone data in whatever directory they run "
            "from; set an absolute path, or unset it",
        )
    return Path(raw)


def _checked_avd_home(path: Path, source: str) -> Path:
    """The AVD home at `path`, resolved and checked; `source` is what an error names. It is resolved so that the tree check
    sees where a link leads and so that what is handed on has no `..` the OS could fail to follow; it must lie outside any
    git working tree (a repository could take the phones' data into a commit), and it must hold nothing a unit file cannot
    carry, because the units name it: refusing that here stops setup before it downloads anything."""
    way_out = "" if source == "ANDROID_AVD_HOME" else " (ANDROID_AVD_HOME chooses another directory)"
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:  # a symlink loop raises RuntimeError before Python 3.13
        raise PhoneError(Code.USAGE, f"cannot resolve {source} {str(path)!r}: {exc}{way_out}") from None
    problem = git_tree_problem(resolved)
    if problem:
        raise PhoneError(Code.USAGE, f"{source} path {resolved} {problem}{way_out}")
    if unit_unsafe(str(resolved)):
        raise PhoneError(
            Code.USAGE,
            f"{source} {str(resolved)!r} contains {UNIT_UNSAFE}, which a unit file cannot carry "
            f"(the units name the AVD home){way_out}",
        )
    return resolved


def avd_home() -> Path:
    """The AVD home: the directory that holds the phones' data (disk images, snapshots), worked out as the Android tools do
    and checked. ANDROID_AVD_HOME when it is set, else ANDROID_USER_HOME/avd when that is set, else ~/.android/avd.

    This is the only way to it. The real tools ignore an ANDROID_AVD_HOME that is not an existing directory and put the phone
    in their default home instead, so the answer is not left to the environment: it is handed on as an absolute path. Setup
    makes the directory and runs avdmanager with it, the units always name it, and a launch tells the emulator. A variable
    that gives it must hold an absolute path; whichever it is, the path is resolved, outside any git working tree, and fit
    to be written into a unit (see _checked_avd_home). A refusal is USAGE naming the variable, or ANDROID_AVD_HOME as the way
    out when the default is what fails. The directory need not exist yet: setup makes it, and a launch refuses one that is
    not there. A variable that does not decide (ANDROID_USER_HOME when ANDROID_AVD_HOME is set) is not looked at.
    """
    named = os.environ.get("ANDROID_AVD_HOME")
    if named is not None:
        return _checked_avd_home(_given_absolute("ANDROID_AVD_HOME", named), "ANDROID_AVD_HOME")
    user = os.environ.get("ANDROID_USER_HOME")
    if user is not None:
        return _checked_avd_home(_given_absolute("ANDROID_USER_HOME", user) / "avd", "ANDROID_USER_HOME")
    return _checked_avd_home(Path(DEFAULT_AVD_HOME).expanduser(), "the default AVD home")


def unit_dir() -> Path:
    return Path(os.environ.get("ORION_PHONE_UNIT_DIR", "~/.config/systemd/user")).expanduser()


# The unit that runs the debug bridge's server (see render_adb_server_unit). A phone's unit is orion-phone-<name>.service
# and a name is 1 to 16 lowercase letters or digits, so no phone's unit can have this name: it has a hyphen after the prefix.
ADB_SERVER_UNIT = "orion-phone-adb-server.service"
# The port that server listens on, and its address in the form adb's `-L` takes. They are written once: the unit gives the
# address to the server (render_adb_server_unit), every adb call gives it to the client (adb.Adb), and setup looks at the port
# for the server (wait_for_adb_server), so none of them can come apart.
ADB_SERVER_PORT = 5037
ADB_SERVER_ADDRESS = f"tcp:localhost:{ADB_SERVER_PORT}"


SYSTEMCTL_TIMEOUT = 120  # seconds one systemctl call may take; systemd gives a unit 90 to stop before it kills it
MAX_LIMIT = 86400  # seconds: no time limit can be raised past a day, so no value can overflow a subprocess timeout
DEFAULT_PROBE_HOST = "127.0.0.1"  # where an emulator listens for its console and adb connections
# Seconds a connect to one of its ports may take. A closed loopback port usually refuses at once, but under WSL2's mirrored
# networking the connect is dropped instead, and hangs until this time is up.
PROBE_TIMEOUT = 0.5
# How setup knows that the adb server it has just started is up (see wait_for_adb_server): within SERVER_READY_WAIT seconds in
# all, the server's unit must be active and its port answering, and have stayed so for SERVER_SETTLE seconds, looked at every
# SERVER_POLL seconds. Both times are tuning variables like the other time limits (see server_waits): a slow machine may need
# longer ones, and the tests run with short ones.
SERVER_READY_WAIT = 10.0  # ORION_PHONE_SERVER_WAIT tunes it
MAX_SERVER_WAIT = 120.0  # seconds: the most it can be made, so that no setup is held longer by a server that does not come up
SERVER_SETTLE = 2.0  # longer than the abort of an adb that finds its port taken takes to come (about 0.7 s on a real host)
MAX_SERVER_SETTLE = 30.0  # seconds: the most the settle can be made (ORION_PHONE_SERVER_SETTLE tunes it)
SERVER_POLL = 0.25
# What systemd reads in the program path of ExecStart: it splits on whitespace and handles quotes, backslashes, $
# and %, and (checked with systemd-analyze) it does not unescape $$ in that word. No escape makes such a path safe,
# so it is refused.
_UNSAFE_IN_EXEC = frozenset(['"', "'", "\\", "$", "%"])


def limit(variable: str, default: float) -> float:
    """A time limit in seconds: the environment variable when it holds a finite number above zero, else the default.
    A slow machine can raise a limit and a test can lower one, up to a day; a bad value never turns a limit off."""
    try:
        value = float(os.environ[variable])
    except (KeyError, ValueError):
        return default
    return min(value, MAX_LIMIT) if math.isfinite(value) and value > 0 else default


def _unit_value(value: object) -> str:
    """Escape for a quoted Environment value: % specifiers expand everywhere, and backslashes are unescaped inside
    the quotes. Config already rejects double quotes, control characters, and line and paragraph separators."""
    return str(value).replace("%", "%%").replace("\\", "\\\\")


def _check_exec_path(path: str, what: str, way_out: str) -> None:
    """Refuse the program path of an ExecStart that systemd would misread (see _UNSAFE_IN_EXEC), or that a unit file cannot
    carry at all (unit_unsafe: a control character, a line or paragraph separator, a double quote). `what` says whose path it
    is, for the message, and `way_out` how to fix it: the interpreter of a phone's unit, the adb of the server's."""
    if not path or any(c.isspace() or c in _UNSAFE_IN_EXEC for c in path) or unit_unsafe(path):
        raise PhoneError(
            Code.USAGE,
            f"cannot write units for {what} {path!r}: systemd would misread its path in ExecStart "
            f"(whitespace, quotes, backslash, $, % and control characters are the trouble); {way_out}",
        )


def render_unit(cfg: Config, phone: PhoneConfig, python: str, display: str | None, avd: Path) -> str:
    """The unit's text. `display` and `avd` (the checked AVD home) come from the shell that runs install-units. The AVD
    home is always written, so that the unit starts the phone from the directory setup used whatever its own environment
    holds; DISPLAY only when that shell had one."""
    _check_exec_path(python, "the interpreter", "run install-units with a Python whose path has none")
    if display and unit_unsafe(display):  # it comes from the shell that runs install-units, and lands inside quotes
        raise PhoneError(
            Code.USAGE,
            f"DISPLAY {display!r} contains {UNIT_UNSAFE}, which a unit file cannot carry; "
            "fix or unset it, then run install-units again",
        )
    if unit_unsafe(str(avd)):  # the same, for the same reason (avd_home refuses it first: this is the backstop)
        raise PhoneError(
            Code.USAGE,
            f"ANDROID_AVD_HOME {str(avd)!r} contains {UNIT_UNSAFE}, which a unit file cannot carry; fix or unset it, "
            "then run install-units again",
        )
    lines = [
        "[Unit]",
        f"Description=orion-phone {phone.name} ({phone.tier} tier)",
        # Every adb call a phone makes but one needs the debug bridge's server (see render_adb_server_unit), which runs from
        # login (the command that tells an emulator to exit goes to the emulator's console itself). This has starting an open
        # phone start it too (Wants), and first (After), should it have been stopped. A lease on a locked phone talks to adb
        # before it starts the phone, so it never gets the server this way, and this alone is not what keeps adb answering.
        # Wants is weak: stopping the phone leaves the server running, and a server that fails to start does not stop the
        # phone from starting.
        f"Wants={ADB_SERVER_UNIT}",
        f"After={ADB_SERVER_UNIT}",
        "",
        "[Service]",
        "Type=simple",
        f'Environment="ORION_PHONE_CONFIG={_unit_value(cfg.path)}"',
        f'Environment="ORION_PHONE_STATE={_unit_value(cfg.state_dir)}"',
    ]
    lines.append(f'Environment="ANDROID_AVD_HOME={_unit_value(avd)}"')
    if display:
        lines.append(f'Environment="DISPLAY={_unit_value(display)}"')
    # -P (Python 3.11+): a user unit starts in the user's home directory, and `python -m` would put that directory first
    # on sys.path, so an `orion_phone` folder or `orion_phone.py` kept there would run in place of the installed package.
    lines.append(f"ExecStart={python} -P -m orion_phone _run {phone.name}")
    # LimitCORE=1 keeps the phone's memory out of crash dumps: an open phone's holds the sessions of the accounts it is
    # signed in to, and a locked phone's, once its owner has unlocked it, what the lock protects; a dump also runs to
    # gigabytes, and an open phone waits for its dump to be written before it starts again. Not 0: a crash handler the
    # kernel pipes dumps to (systemd-coredump, WSL2's crash capture) is given one whatever the limit, except at 1, where
    # the kernel drops it: core limits are otherwise irrelevant to pipes, and fs/coredump.c takes a limit of 1 as the
    # signal to abort a piped dump (on WSL2 it logged "RLIMIT_CORE is set to 1, aborting core" for a crash under this
    # limit). Only a locked unit's is checked (unit_cap_problem): for an open phone it is care, not a guarantee.
    if phone.tier == "locked":  # no [Install]: nothing may start a locked phone on its own
        # KillMode and TimeoutStopSec are systemd's defaults, written out because the watch relies on them: the emulator's
        # helper processes go with the stop, and a stop (a grace for SIGTERM, a second after SIGKILL) ends inside the time
        # systemd gives it.
        lines += [
            "Restart=no", f"RuntimeMaxSec={phone.session_cap_minutes * 60}", "KillMode=control-group",
            f"TimeoutStopSec={UNIT_STOP_TIMEOUT}", "LimitCORE=1", "",
        ]
    else:
        lines += ["Restart=on-failure", "RestartSec=10", "LimitCORE=1", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def render_adb_server_unit(cfg: Config) -> str:
    """The text of the unit that runs the debug bridge's server as a service of its own (ADB_SERVER_UNIT).

    Why it is a service: the adb client starts its own server only when its first connect to loopback port 5037 is
    refused. Where a connect to a port nobody listens on is dropped and hangs instead (WSL2's mirrored networking), that
    refusal never comes, so with no server running every adb call that goes through it waits out its time limit. A server
    that is already running has no such trouble, so orion-phone keeps it running itself.

    It has to be running before the first call, and a phone's own start cannot see to that: a lease on a locked phone talks
    to adb (it looks at what adb lists, and tells the emulator to exit when its console answers) before it starts the
    phone's unit. So the server runs from login, whatever the tiers. `[Install]` has default.target want it; install-units
    enables it (the login link only, made before its one reload and with no reload of its own: it starts nothing); setup
    starts it, after its last reload and the look that follows, and waits until it is up (see wait_for_adb_server: the start
    alone is no proof, since the unit counts as started once adb is spawned) before it starts the phones. Every phone's unit
    also wants it and starts after it (see render_unit): an open phone's unit starting starts a server that is not running. A
    lease on a locked phone does not get the server that way, since its first adb call comes before its unit starts. So a
    stopped server is started when the user's service manager next starts (normally after a reboot, or a restart of the WSL
    instance: a login starts nothing when the manager already runs, as with lingering on or another session open), by hand, or
    by an open phone's unit. Only setup checks that the server came up; a start at login is not checked.

    The unit runs `adb -L ADB_SERVER_ADDRESS nodaemon server` (`-L tcp:localhost:5037`), adb being android.adb, in the
    foreground as the unit's own process. The listen address is given, not left to ADB_SERVER_SOCKET or
    ANDROID_ADB_SERVER_PORT, which the user manager's environment may hold: it is loopback port 5037, and never `-a`, which
    would listen on every interface. orion-phone's own adb calls name the same address (see adb.Adb), so none of the
    variables that name a server's address (ADB_SERVER_SOCKET, ANDROID_ADB_SERVER_ADDRESS, ANDROID_ADB_SERVER_PORT) can send
    one elsewhere from its environment: a client left to the environment follows them, to another server or to an address
    where none is running. Another adb tool reaches the server only when it goes to that default address, which is where an
    adb client goes when its environment names none.

    Restarts (observed with platform-tools adb 1.0.41 under systemd 255). `adb kill-server` makes the server exit with
    status 0, so Restart=always is needed (on-failure would leave it down), and the unit starts it again two seconds later.
    That holds when nothing else takes the port. Where a refused connect makes any adb client start a server of its own
    (ordinary Linux), something can take the port first: an adb call in those two seconds, or another tool's server (an
    IDE's, or a newer platform-tools after an update). The unit's next start then finds the port held, and adb aborts
    (`could not install *smartsocket* listener: Address already in use`). Left to Restart=always that is a loop with no end,
    one start about every 2.7 seconds, which never trips the start limit, and a core dump every time. So an abort is not
    restarted (RestartPreventExitStatus=SIGABRT): the unit stays failed while the other server serves the phones, and a
    plain start brings it back once that server is gone. Under WSL2's mirrored networking adb cannot start a server of its
    own, which is why orion-phone runs one.

    The adb path is the program of ExecStart, which systemd reads for whitespace, quotes, backslash, $ and %, and a unit
    file cannot carry a control character: a path it would misread is refused, as an interpreter's is."""
    _check_exec_path(str(cfg.adb), "the debug bridge at android.adb", "set android.adb to a path that has none")
    lines = [
        "[Unit]",
        "Description=orion-phone adb server (the debug bridge's server, on loopback port 5037)",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={cfg.adb} -L {ADB_SERVER_ADDRESS} nodaemon server",
        "Restart=always",
        "RestartSec=2",
        "RestartPreventExitStatus=SIGABRT",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


class SystemctlFailed(PhoneError):
    """systemctl ran and exited with a failure status: it answered, and the answer was no. (Not running at all, and not
    answering in time, are PhoneErrors of their own: there is no answer to look into.)"""


def _decoded(raw: bytes) -> str:
    """What a tool printed, as UTF-8 text with replacement and nothing else done to it. In particular no newline translation:
    a capture in text mode turns `2h` and two carriage returns before the newline into `2h` and an empty line, and a lone
    carriage return into a newline, before any reader can see that the record was not exact."""
    return raw.decode("utf-8", "replace")


def _systemctl(*args: str) -> str:
    """Run `systemctl --user ARGS` and return what it printed (see _decoded). Every way it can fail is a PhoneError naming the
    unit."""
    data = {"unit": args[-1]} if len(args) > 1 and not args[-1].startswith("-") else {}  # the unit is the last word
    timeout = limit("ORION_PHONE_SYSTEMCTL_TIMEOUT", SYSTEMCTL_TIMEOUT)
    try:
        proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise PhoneError(Code.USAGE, "systemctl not found; orion-phone runs phones as systemd user services", data) from None
    except subprocess.TimeoutExpired:  # a failure like any other: the caller frees a session that could not start
        raise PhoneError(
            Code.PHONE_DOWN, f"systemctl --user {' '.join(args)} timed out after {timeout:g}s", data
        ) from None
    except OSError as exc:  # not executable, a directory, a broken interpreter line
        raise PhoneError(Code.USAGE, f"cannot run systemctl: {exc.strerror or exc}", data) from None
    if proc.returncode != 0:
        raise SystemctlFailed(
            Code.PHONE_DOWN, f"systemctl --user {' '.join(args)} failed: {_decoded(proc.stderr).strip()}", data
        )
    return _decoded(proc.stdout)


def systemctl(*args: str) -> None:
    _systemctl(*args)


def invocation_id(unit: str) -> str:
    """The InvocationID of the unit's current run, or "" when the unit is not active.

    systemd gives every run of a unit its own id, so a phone that was stopped and started again (by the session cap, an
    operator, anything) is a different boot. An inactive or failed unit keeps its last id, so the state is read in the
    same call and anything but "active" is "": a boot that is over is not one a session can stand on.
    """
    values = _show(unit, "ActiveState", "InvocationID")
    return values.get("InvocationID", "") if values.get("ActiveState", "") == "active" else ""


# Microseconds in each unit `systemctl show` prints a time span with ("2h", "1h 30min", "45s"), as systemd defines them.
_TIMESPAN_US = {
    "us": 1, "ms": 1_000, "s": 1_000_000, "min": 60_000_000, "h": 3_600_000_000, "d": 86_400_000_000,
    "w": 604_800_000_000, "month": 2_629_800_000_000, "y": 31_557_600_000_000,
}
_TIMESPAN_PART = r"[0-9]{1,12}(?:us|ms|s|min|h|d|w|month|y)"
_TIMESPAN = re.compile(rf"{_TIMESPAN_PART}(?: ?{_TIMESPAN_PART})*")


def _timespan_us(text: str) -> int | None:
    """The microseconds in a time span as `systemctl show` prints one ("2h", "1h 30min", "45s"), or None when the text is
    anything else: only whole numbers of known units, every character accounted for. A guess is never made."""
    if not _TIMESPAN.fullmatch(text):
        return None
    return sum(int(number) * _TIMESPAN_US[unit] for number, unit in re.findall(r"([0-9]+)([a-z]+)", text))


def _got(value: str | None) -> str:
    return "nothing" if value is None else repr(value[:40])  # what systemctl printed, quoted and short


def _show(unit: str, *properties: str) -> dict[str, str]:
    """`systemctl --user show -p NAME ... UNIT` as the name=value lines it printed. systemd prints in its own order, and may
    print more than was asked, so nothing relies on either.

    A line ends at a newline and nowhere else. systemd escapes every ASCII control character in what it prints, but not
    U+0085, U+2028 or U+2029, which `str.splitlines` also breaks on, so a value in a unit's environment could forge a line
    of its own (probed on a real unit: X=a<U+2028>RuntimeMaxUSec=2h made the cap of a unit that has none read as 2h). A
    property printed more than once cannot be told from a forged one, and no honest output repeats one: it is refused.

    A line is an exact record, `name=value`: at most one carriage return before the newline is taken off (a CR LF line
    end; see split_lines, which also leaves a carriage return at the very end of the output, with no newline after it, where
    it is), and nothing else is trimmed from the name or the value, so `2h` followed by two carriage returns, ` 2h`, `2h `
    and a last `2h` followed by a lone carriage return are not `2h`, and a name with a blank in it is another name (the
    property is then missing). Probed on systemd 255 with
    transient units (a default unit, environments ending in a space, a tab, a carriage return and a backslash, an empty
    environment value, caps with and without spaces): no line has a carriage return, a blank at either end, or a blank
    around the `=`, so a blank there is not systemd's, and a property it spoils is unreadable, which is refused."""
    args = ["show"]
    for name in properties:
        args += ["-p", name]
    out = _systemctl(*args, unit)
    values: dict[str, str] = {}
    for line in split_lines(out):
        name, equals, value = line.partition("=")
        if not equals or not name:
            continue
        if name in values:
            raise PhoneError(
                Code.USAGE,
                f"systemctl printed {name} for {unit} more than once, so its output cannot be trusted (a value in the "
                f"unit's environment that holds a line break would look like this): look at the unit with systemctl --user "
                f"cat {unit}",
                {"unit": unit},
            )
        values[name] = value
    return values


def _stop_problem(values: dict[str, str]) -> str | None:
    """Why a stop of this unit would not end what it started in the time the watch needs, from the properties `systemctl show`
    printed (`values`); None when it would. The session cap, the lease watch and a release all end a phone by stopping its
    unit, so the stop must reach every process the unit started: KillMode must be control-group (probed on systemd 255:
    `process` kills the supervisor and leaves the emulator running, `none` kills nothing, and in both the unit then reads as
    stopped). And it must be given the time the watch's own stop can take, a grace for SIGTERM and a second one after SIGKILL
    (STOP_GRACE twice), as a finite span: with `infinity` nothing ever forces a stop that sticks. And when that time runs out
    systemd must kill what is left: SendSIGKILL must be yes (probed on systemd 255, with a process that ignores SIGTERM under
    TimeoutStopSec=2: `yes` ended the stop after the two seconds with nothing left running, `no` ended it with every process
    of the unit still running and the unit read as stopped). A unit that names none of these has the manager's defaults,
    control-group, 90 s and yes, which are enough. What cannot be read with certainty is a problem too, and so is a property
    that is not there."""
    problems = []
    mode = values.get("KillMode")
    if not mode:
        problems.append(f"could not read KillMode from systemctl (got {_got(mode)})")
    elif mode != "control-group":
        problems.append(
            f"KillMode={_plain(mode[:40])}, but it must be control-group: the session cap, the lease watch and a release "
            "end a phone by stopping its unit, and only that mode makes the stop reach every process the unit started"
        )
    stop = values.get("TimeoutStopUSec")
    if stop == "infinity":
        problems.append("TimeoutStopUSec=infinity gives a stop that is never forced, so a stop that sticks would never end")
    else:
        span = _timespan_us(stop) if stop else None
        if span is None:
            problems.append(f"could not read TimeoutStopUSec from systemctl (got {_got(stop)})")
        elif span < round(2 * STOP_GRACE * 1_000_000):
            problems.append(
                f"TimeoutStopUSec={stop} is less than the {2 * STOP_GRACE:g} s (two graces of {STOP_GRACE:g} s) that stopping "
                "the emulator can take, so systemd could end the stop before the phone is off"
            )
    send = values.get("SendSIGKILL")
    if not send:
        problems.append(f"could not read SendSIGKILL from systemctl (got {_got(send)})")
    elif send != "yes":
        problems.append(
            f"SendSIGKILL={_plain(send[:40])}, but it must be yes: with anything else a stop that runs out of time leaves "
            "running what it has not ended"
        )
    return "; ".join(problems) or None


def _restart_problem(values: dict[str, str]) -> str | None:
    """Why the manager would start this unit's phone again on its own once the unit ends, from the properties `systemctl show`
    printed (`values`); None when it would not. A locked phone is started only by this tool's own cold boot, at the beginning
    of a session, and its run is over when its unit ends (the cap, a stop, a crash): the unit is written with Restart=no, and
    with no [Install] section, so that nothing brings it back. Any other value has the manager start the phone again by itself
    after the ends it names (on-failure after a failure, on-success after a clean exit, always after any), a boot that no lease
    asked for. A drop-in can set it, so the loaded value must be `no`, which is also what a unit that names none has (read on
    systemd 255: `Restart=no` for a locked phone's unit, `on-failure` for an open phone's, `always` for the adb server's). What
    cannot be read with certainty is a problem too, and so is a property that is not there."""
    restart = values.get("Restart")
    if restart == "no":
        return None
    if not restart:
        return f"could not read Restart from systemctl (got {_got(restart)})"
    return (
        f"Restart={_plain(restart[:40])}, but it must be no: nothing may start a locked phone again on its own, and a unit "
        "that the manager restarts brings back a phone that no lease asked for"
    )


def _core_problem(values: dict[str, str]) -> str | None:
    """Why a crash of this unit's emulator could leave a dump of the phone's memory, from the properties `systemctl show`
    printed (`values`); None when it could not. That memory holds what the lock protects once the owner has unlocked the
    phone, so the unit is written with LimitCORE=1, which sets both core limits to 1. Only 1 keeps the dump from a crash
    handler the kernel pipes dumps to (systemd-coredump, WSL2's crash capture): such a handler gets a dump whatever the soft
    limit, 0 included, except at 1, where the kernel drops it. Core limits are otherwise irrelevant to pipes, and
    fs/coredump.c takes 1 as the signal to abort a piped dump (it uses 1 itself to stop a crash handler's own crash from
    recursing). Checked on WSL2: a crash under 0 was captured, and one under 1 was not, the kernel logging "RLIMIT_CORE is
    set to 1, aborting core". The hard limit must be 1 too, or the process could raise its soft limit again. A unit that names none has the
    manager's defaults, LimitCORE=infinity and LimitCORESoft=0 (probed on systemd 255), which fail. A process keeps the
    limits it started with: a reload does not change them. What cannot be read with certainty is a problem too, and so is a
    property that is not there."""
    problems = []
    for name in ("LimitCORE", "LimitCORESoft"):
        value = values.get(name)
        if not value:
            problems.append(f"could not read {name} from systemctl (got {_got(value)})")
        elif value != "1":
            problems.append(
                f"{name}={_plain(value[:40])}, but it must be 1: with any other limit a crash of the phone's emulator can "
                "leave a dump of the phone's memory"
            )
    return "; ".join(problems) or None


def _cap_problem(values: dict[str, str], minutes: int) -> str | None:
    """Why the session this unit allows is not exactly the configured cap of `minutes`, or would not be ended at it, from the
    properties `systemctl show` printed (`values`); None when it is. The cap is RuntimeMaxUSec, and RuntimeRandomizedExtraUSec
    must be 0: the manager stops a unit at its cap plus a random span of up to that extra time, so a session could outlast the
    cap. The cap ends a session by stopping the unit, so what a stop does counts too (see _stop_problem), and so does whether
    the manager would start the phone again afterwards (see _restart_problem), and so do the core limits that keep the phone's
    memory out of crash dumps (see _core_problem). What cannot be read with certainty is a problem too, and so is a property
    that is not there: nothing here passes on a guess."""
    problems = []
    cap = values.get("RuntimeMaxUSec")
    if cap == "infinity":
        problems.append("the unit has no session cap (RuntimeMaxUSec=infinity)")
    else:
        loaded = None if cap is None else _timespan_us(cap)
        if loaded is None:
            problems.append(f"could not read RuntimeMaxUSec from systemctl (got {_got(cap)})")
        elif loaded != minutes * 60 * 1_000_000:
            problems.append(f"its session cap is {cap}, but the config says {minutes} minutes")
    extra = values.get("RuntimeRandomizedExtraUSec")
    if extra == "0":  # how systemd prints no extra time: every other spelling is something this has not seen it print
        pass
    elif extra == "infinity" or (extra is not None and (_timespan_us(extra) or 0) > 0):
        problems.append(
            f"RuntimeRandomizedExtraUSec={extra} lets a session outlast its cap, so RuntimeRandomizedExtraSec must be "
            "removed from the unit and its drop-ins"
        )
    else:
        absent = "; systemd before 250 does not print it, and a missing answer is never taken as zero" if extra is None else ""
        problems.append(f"could not read RuntimeRandomizedExtraUSec from systemctl (got {_got(extra)}{absent})")
    stop_problem = _stop_problem(values)
    if stop_problem:
        problems.append(stop_problem)
    restart_problem = _restart_problem(values)
    if restart_problem:
        problems.append(restart_problem)
    core_problem = _core_problem(values)
    if core_problem:
        problems.append(core_problem)
    return "; ".join(problems) or None


def _unit_problem(values: dict[str, str], minutes: int) -> str | None:
    """Why a unit, from the properties `systemctl show` printed (`values`), is not the capped one the config calls for: a
    reload pending (what runs is not known to be what is on disk), or anything `_cap_problem` finds. None when it is."""
    problems = []
    reload = values.get("NeedDaemonReload")
    if reload == "yes":
        problems.append("systemd has not reloaded the unit file since it changed (NeedDaemonReload=yes)")
    elif reload != "no":
        problems.append(f"could not read NeedDaemonReload from systemctl (got {_got(reload)})")
    cap_problem = _cap_problem(values, minutes)
    if cap_problem:
        problems.append(cap_problem)
    return "; ".join(problems) or None


def _load_problem(values: dict[str, str], unit: str) -> str | None:
    """Why the manager does not have this unit loaded, from the properties `systemctl show` printed (`values`); None when it
    has. What `show` prints for a unit that is not loaded is systemd's own defaults (no cap, an empty environment), not the
    unit's settings, so when this finds a problem nothing else that was printed is worth reading."""
    load = values.get("LoadState")
    if load == "loaded":
        return None
    if load == "not-found":
        return f"systemd has no unit named {unit} (LoadState=not-found): it is not installed"
    if not load:
        return f"could not read LoadState from systemctl (got {_got(load)})"
    return f"systemd has not loaded the unit (LoadState={_plain(load[:40])})"


_ENV_ESCAPES = {
    "\\": "\\", '"': '"', "$": "$", "`": "`", "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
}
_ENV_QUOTED = frozenset("\"'\\$`")  # in a bare word these would have been quoted: it is not what systemd printed


def _parse_environment(text: str) -> dict[str, str] | None:
    """The variables in a unit's Environment property as `systemctl show` prints it (probed on systemd 255, one character at
    a time): assignments separated by single spaces (none at either end), each bare, or, when it holds a space, one of
    !"$&'()*;<>?[\\` and |, a control character or DEL, in double quotes with a backslash before each backslash, double
    quote, dollar sign and backtick, and a C style escape (a, b, t, n, v, f, r) or a three digit octal number for a control
    character or DEL. Everything else is printed as it is, in a bare word too: a percent sign, # ] ^ { } ~, and all that is
    not ASCII (U+0085, U+2028 and U+2029 among it). A bare word is refused only when it holds what would make it ambiguous
    (a quote, a backslash, $, a backtick, a control character), the rest being read as it stands. A variable named twice is
    the last one (systemd merges them itself). None when the text is anything else, a space at either end or two together
    included: nothing here trims or guesses at what systemd printed."""
    variables: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        chars: list[str] = []
        if text[i] == '"':
            i += 1
            while True:
                if i >= n:
                    return None  # a quote that is never closed
                c = text[i]
                i += 1
                if c == '"':
                    break
                if c != "\\":
                    chars.append(c)
                    continue
                if i >= n:
                    return None
                if text[i] in _ENV_ESCAPES:
                    chars.append(_ENV_ESCAPES[text[i]])
                    i += 1
                    continue
                digits = text[i : i + 3]  # a control character is a backslash and three octal digits
                if len(digits) != 3 or any(d not in "01234567" for d in digits) or int(digits, 8) >= 0o200:
                    return None
                chars.append(chr(int(digits, 8)))
                i += 3
            if i < n and text[i] != " ":
                return None  # text right after a closing quote
        else:
            end = text.find(" ", i)
            word = text[i : n if end < 0 else end]
            if any(c in _ENV_QUOTED or ord(c) < 32 for c in word):
                return None
            chars = list(word)
            i += len(word)
        name, equals, value = "".join(chars).partition("=")
        if not equals or not name:
            return None
        variables[name] = value
        if i < n:  # at a space: exactly one separates the assignments, and none ends the text
            i += 1
            if i >= n or text[i] == " ":
                return None
    return variables


def _plain(value: str) -> str:
    """A value for a message: as it is, except that a character that does not print is shown as its escape."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in value)[:300]


def _state_problem(values: dict[str, str], state_dir: Path) -> str | None:
    """Why the state directory a unit watches for its lease is not the one this command uses (None: the same one). A unit
    carries the ORION_PHONE_STATE of the shell that installed it, and `_run` watches the leases there: a command with another
    state directory would have every boot it starts stopped at the watch's first look, no lease being held in the unit's.
    Paths are compared resolved, so a link, a trailing slash, or dots in the path are no difference. What cannot be read with
    certainty is a problem too."""
    raw = values.get("Environment")
    if raw is None:
        return "could not read Environment from systemctl (got nothing)"
    variables = _parse_environment(raw)
    if variables is None:
        return f"could not read Environment from systemctl (got {_got(raw)})"
    named = variables.get("ORION_PHONE_STATE")
    if not named:
        return "its unit names no state directory (ORION_PHONE_STATE is not in its environment)"
    if not os.path.isabs(named):
        return f"its unit names a state directory that is not an absolute path ({_plain(named)})"
    try:
        if Path(named).resolve() == state_dir.resolve():
            return None
    except (OSError, RuntimeError):  # a path that cannot be resolved cannot be shown to be the same one
        pass
    return f"its unit watches the state directory {_plain(named)}, but this command uses {_plain(str(state_dir))}"


def unit_cap_problem(phone: PhoneConfig, state_dir: Path) -> str | None:
    """Why the unit the service manager has loaded for this locked phone cannot start it for a command that uses
    `state_dir`, or None when it can.

    A locked phone's session cap lives in its unit (RuntimeMaxSec), and the manager runs what it has loaded, not what is on
    disk: a phone switched to locked keeps its old open unit, which has no cap, until install-units rewrites it and the
    manager reloads, and a unit file changed since the last reload is not what would run. So the loaded unit must carry
    exactly the configured cap, with no extra run time past it (RuntimeRandomizedExtraSec, which a drop-in can set), a stop
    that ends the phone (see _stop_problem), a Restart of no (see _restart_problem: nothing may start a locked phone again
    on its own) and core limits of 1 (see _core_problem: a crash must leave no dump of the phone's memory), and have no
    reload pending. It must also watch the state directory the command uses (see _state_problem).
    What cannot be read with certainty is a problem too: nothing here lets a start through on a guess. A unit the manager
    has not loaded (not installed yet, or its file removed) is the only problem told of it: it has no settings to read.
    Raises PhoneError when systemctl cannot answer at all: a failure, not a pass.
    """
    values = _show(
        phone.unit_name, "LoadState", "RuntimeMaxUSec", "RuntimeRandomizedExtraUSec", "KillMode", "TimeoutStopUSec",
        "SendSIGKILL", "Restart", "NeedDaemonReload", "Environment", "LimitCORE", "LimitCORESoft",
    )
    not_loaded = _load_problem(values, phone.unit_name)
    if not_loaded:
        return not_loaded
    problems = [_unit_problem(values, phone.session_cap_minutes), _state_problem(values, state_dir)]
    return "; ".join(problem for problem in problems if problem) or None


NOT_RUNNING = ("inactive", "failed")  # the two states with no process behind them; any other may be a running phone


def _not_installed(values: dict[str, str]) -> bool:
    """Is it certain, from the properties `systemctl show` printed (`values`), that systemd has no unit by this name and that
    nothing runs under it? `show` answers for a name it has no file for like for any other (probed on systemd 255: exit 0, the
    unit's defaults, LoadState=not-found, ActiveState=inactive), so a first install, before any unit is written, is no error.
    LoadState alone does not say it: whether a unit that runs can be not found (its file removed and the manager reloaded)
    was not probed, and a unit that runs is read like any other running unit."""
    return values.get("LoadState") == "not-found" and values.get("ActiveState") in NOT_RUNNING


def not_installed(unit: str) -> bool:
    """Is this unit certainly not installed (see _not_installed)? Asked after a `systemctl stop` failed, to tell a unit that
    was never installed from any other reason a stop can fail. A look that cannot be made is no: the stop's own failure then
    stands."""
    try:
        values = _show(unit, "LoadState", "ActiveState")
    except PhoneError:
        return False
    return _not_installed(values)


StopPhone = Callable[[PhoneConfig], None]  # stops a locked phone and sees that it is gone, or raises PhoneError


class StopNotAttempted(PhoneError):
    """A `stop` that could not begin: the phone's action lock stayed busy for as long as the stop waits for it, or its lock file
    cannot be opened. Nothing was done to the phone, so nothing is known of its emulator, and it is a failure like a phone not
    shown gone but never said in the same words: it is not said that the emulator was told to exit, or that it still answers.
    The message begins "not stopped: " and says why. USAGE, since what is wrong is for the operator to put right."""

    def __init__(self, because: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(Code.USAGE, f"not stopped: {because}", data)


@dataclass
class _Failure:
    """A locked phone a look could not deal with. `problem` is what is wrong with its running unit, or None when the unit
    could not be read at all; `error` is why the phone was not shown gone, or why its unit was not read."""

    phone: PhoneConfig
    problem: str | None
    error: PhoneError


def _look_failed(
    failures: list[_Failure], stopped: list[str], after_reload: bool, reload_failed: bool, again: str = "install-units"
) -> PhoneError:
    """The one error a look at the running locked phones ends in, once every one of them has been looked at.

    Each phone that was not shown gone is named, with what is wrong with its unit and what still answers for it (the error
    `stop` gave), and told not to be trusted until it is gone. A phone is stopped when its unit is not confirmed to enforce
    the session cap, which has more causes than a missing cap (a kill mode, a stop time, extra run time, a restart, a reload
    pending), so the words say no more than that. A phone whose stop never began (StopNotAttempted: its action lock stayed
    busy, or could not be opened) is named in other words, the stop's own (not stopped, and why), and told not to be trusted
    until it is stopped: nothing is known of its emulator, so nothing is said of it. Units that could not be read follow:
    whether they enforce their cap is not known. The phones stopped (and shown gone) first are named last. If nothing but
    reads failed, the manager is not answering for the units and the first read's own error stands, as it always did, with
    `unread` added; after a reload that worked it also says that the units are written and what to run again, and names the
    phones stopped, as every failure after the writes does: the sync of the unit directory, the adb server's login link, the
    reload (see _reload_failed) and this look, whichever phone or unit it fails on. `data` names the first phone not stopped
    or not shown gone, as it always did, and lists them all (`failed`), those among them whose stop never began
    (`not_stopped`), and the phones whose units could not be read (`unread`). `again` is the command the advice after a
    reload tells the operator to run (the one before the writes is always install-units, which is the only caller there)."""
    gone = [f for f in failures if f.problem is not None]
    unread = [f for f in failures if f.problem is None]
    if not gone:
        read = failures[0].error
        error = PhoneError(read.code, read.message, {**read.data, "unread": [f.phone.name for f in unread]})
        if after_reload and not reload_failed:  # the units are written and loaded, and what runs could not be read
            names = ", ".join(f"phone {f.phone.name}" for f in unread)
            return step_failed(
                stopped, error,
                f"the units are written and systemd reloaded them, but the unit of {names} could not be read, so it is not "
                "known whether a running locked phone's unit enforces its session cap",
                again,
            )
        return error
    now = "now " if after_reload and not reload_failed else ""
    never = [f for f in gone if isinstance(f.error, StopNotAttempted)]
    said = []
    for f in gone:
        if f in never:
            what = f"{f.error.message}; do not trust the phone until it is stopped"
        else:
            what = f"it could not be stopped and shown gone ({f.error.message}); do not trust the phone until it is gone"
        said.append(
            f"phone {f.phone.name} is {now}running from {f.phone.unit_name}, which is not confirmed to enforce the session "
            f"cap its config calls for ({f.problem}); {what}; stop it by hand (systemctl --user stop {f.phone.unit_name}, "
            f"and if its emulator still runs, adb -s {f.phone.serial} emu kill)"
        )
    said += [
        f"{f.phone.unit_name} of phone {f.phone.name} could not be read ({f.error.message}), so it is not known whether it "
        "enforces its session cap"
        for f in unread
    ]
    cat = f"systemctl --user cat {' '.join(f.phone.unit_name for f in gone)}"
    if not after_reload:
        lead, tail = "", "then run install-units again"
    elif reload_failed:
        lead = "the units were written, but systemd has not confirmed that it loaded them, and "
        tail = (
            f"remove what changes the unit from what the config calls for (a drop-in: {cat} lists them), then run {again} "
            "again once systemd is responsive"
        )
    else:
        lead = "the units were written and systemd reloaded them, and "
        tail = (
            f"remove what changes the unit from what the config calls for (a drop-in: {cat} lists them), then run {again} again"
        )
    first = f" (stopped and shown gone first: {', '.join(stopped)})" if stopped else ""
    data: dict[str, object] = {
        "phone": gone[0].phone.name,
        "unit": gone[0].phone.unit_name,
        "failed": [f.phone.name for f in gone],
        "stopped": list(stopped),
    }
    if never:
        data["not_stopped"] = [f.phone.name for f in never]
    if unread:
        data["unread"] = [f.phone.name for f in unread]
    return PhoneError(Code.USAGE, f"{lead}{'; and '.join(said)}, {tail}{first}", data)


def stop_uncapped_sessions(
    cfg: Config, stopped: list[str], stop: StopPhone, after_reload: bool = False, reload_failed: bool = False,
    again: str = "install-units",
) -> None:
    """Stop each locked phone that may be running from a unit whose loaded definition is not confirmed to enforce its
    configured session cap, and add the names of the phones stopped to `stopped`. The caller's list, so that a failure
    further on still has it.

    A phone is stopped by `stop`, which the caller gives (phone.py makes it, since services cannot import phone) and which
    must not return before the phone is shown gone: a unit stopped here may be one whose settings leave the emulator alive
    (an old unit with no cap, a kill mode that spares it, a stop that is not forced), so a zero exit from `systemctl stop`
    proves nothing. A phone is added to `stopped` only after `stop` returns; one it cannot show gone is an error that says
    not to trust the phone until it is.

    The manager runs what it has loaded. A phone switched from open to locked keeps running from its old unit, which has
    no cap, until something restarts it, and so does one whose cap the operator has since changed. Whatever the manager does
    with a changed RuntimeMaxSec on a unit that already runs, only a fresh start is known to carry the cap the config calls
    for, so any loaded cap but exactly the configured one (none, another, or one that cannot be read) is stopped, and so is
    a session that may outlast its cap (extra run time other than 0, or none that can be read), one that the manager
    would start again once it ends (a Restart other than no, or none that can be read), and one whose emulator could leave a
    dump of the phone's memory (core limits other than 1, or none that can be read: a process keeps the limits it started
    with, and before the writes the loaded unit is the one the phone was started from); a session that carries exactly the
    cap, with none of that, is left alone. A unit not known to be off is treated as running. A unit that systemd has no file for
    (LoadState is not-found, as before a first install) and that nothing runs under is skipped: there is nothing to stop and
    nothing in it to read, and `show` answers for it like for any other unit (see _not_installed). A phone that cannot be
    stopped, or not shown gone, is USAGE: the install must not report success while a locked phone that is not confirmed to
    enforce its cap keeps running.

    Every locked phone is looked at, whatever happens to the others: a phone that cannot be shown gone, or whose unit cannot
    be read, does not keep a later one from being read and stopped (in the look after the reload, the later phone may have
    been uncapped by that very reload). The failures are collected and raised once, after the last phone, by _look_failed,
    naming each; this look's caller refuses to write units on it, since a phone not shown gone is not safe to leave. A `stop`
    that never began (StopNotAttempted) is such a failure too, in its own words, and the phone is not added to `stopped`.

    This runs twice in an install. Before the writes, a pending reload does not count: the loaded cap is what the running
    process was started under. But the reload that follows loads whatever is pending (a drop-in's RuntimeMaxSec or
    RuntimeRandomizedExtraSec included) and systemd re-arms a running service's runtime limit from it, so a session that
    passed the first look can be uncapped by the reload itself. `after_reload` is that second look: it also reads
    NeedDaemonReload, which must then say no (a reload that left a change unloaded leaves what runs unknown), and a
    failure to stop says that the units are already written and loaded. With `reload_failed` the reload raised, so what the
    manager has loaded is not known to match the disk (it may load it later, or have loaded it): the failure to stop then
    says only that the units are written. `again` is the command a failure after a reload says to run.
    """
    properties = [
        "ActiveState", "LoadState", "RuntimeMaxUSec", "RuntimeRandomizedExtraUSec", "KillMode", "TimeoutStopUSec", "SendSIGKILL",
        "Restart", "LimitCORE", "LimitCORESoft",
    ]
    if after_reload:
        properties.append("NeedDaemonReload")
    failures: list[_Failure] = []
    for phone in cfg.phones.values():
        if phone.tier != "locked":
            continue
        try:
            values = _show(phone.unit_name, *properties)
        except PhoneError as exc:  # its unit cannot be read: the phones after it are still looked at
            failures.append(_Failure(phone, None, exc))
            continue
        if _not_installed(values):  # a first install: systemd has no unit yet, so nothing can run from one
            continue
        if values.get("ActiveState") in NOT_RUNNING:
            continue
        problem = (_unit_problem if after_reload else _cap_problem)(values, phone.session_cap_minutes)
        if problem is None:
            continue
        try:
            stop(phone)
        except PhoneError as exc:  # not shown gone: the phones after it are still looked at, and stopped
            failures.append(_Failure(phone, problem, exc))
            continue
        stopped.append(phone.name)
    if failures:
        raise _look_failed(failures, stopped, after_reload, reload_failed, again)


def stopped_note(stopped: list[str]) -> str:
    """What a result says about the phones install-units stopped; nothing when there were none. A phone is stopped when its
    unit is not confirmed to enforce its session cap and core limits: one that has no cap (an old open unit), one whose
    emulator could leave a crash dump of the phone's memory (a unit written before LimitCORE), and one that cannot be
    confirmed (a reload owed, extra run time, a stop that would not end it), so the note says the last, which holds for all."""
    if not stopped:
        return ""
    return f"; stopped the locked phones that were not confirmed to enforce their session cap and core limits: {', '.join(stopped)}"


def probe_host() -> str:
    """Where the emulator's ports, and setup's look at the adb server's port, are looked for: loopback, where they
    listen. The environment may name another loopback address (tests use one nothing listens on, so they never touch
    a real phone's ports or the real server's); anything that is not a loopback address is ignored. The server itself
    always listens at localhost, so another address here makes setup's readiness look fail even for a healthy server."""
    raw = os.environ.get("ORION_PHONE_PROBE_HOST", "")
    try:
        return raw if ipaddress.ip_address(raw).is_loopback else DEFAULT_PROBE_HOST
    except ValueError:
        return DEFAULT_PROBE_HOST


def _accepts(host: str, port: int) -> bool:
    """Does a plain TCP connect to host and port succeed within PROBE_TIMEOUT? (Refused, or nothing answered in time: no.)"""
    try:
        socket.create_connection((host, port), timeout=PROBE_TIMEOUT).close()
    except OSError:
        return False
    return True


def answering_port(phone: PhoneConfig) -> int | None:
    """The first of the phone's emulator ports (console, then adb) that accepts a connection, or None.

    A plain TCP connect: it does not go through the adb server, which can be blind (just restarted) to a phone that is
    still running. Whatever answers there is an emulator, and a phone's own unit is stopped by now.
    """
    host = probe_host()
    for port in (phone.console_port, phone.console_port + 1):
        if _accepts(host, port):
            return port
    return None


def console_answers(phone: PhoneConfig) -> bool:
    """Does the phone's console port, and that one alone, accept a connection? The question to ask before `adb emu kill`,
    which connects to the console port itself and not through the adb server: answering_port would name the adb port when
    only that one answers, and a kill sent then would hang the same way as one sent to a port nothing listens on. Under
    WSL2's mirrored networking a connect to a closed loopback port is dropped, not refused, so a kill sent to a phone that
    is down waits out adb's whole time limit; with nothing listening there is nothing for it to reach. The look itself
    waits at most PROBE_TIMEOUT."""
    return _accepts(probe_host(), phone.console_port)


def _sync_directory(path: Path) -> None:
    """Make what was just created, renamed, or removed in a directory durable: an entry is on disk only once the
    directory itself is synced (syncing a file does not sync its name)."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def drop_login_links(cfg: Config) -> None:
    """A locked phone must never start on its own. Its unit has no [Install] section, but the link `systemctl enable`
    made while the phone was open outlives the unit rewrite, and systemd still follows it: the phone would boot at
    the next login. Remove that one link for each locked phone; an open phone keeps its own. The directory is then
    synced (whenever it exists, so that a removal an earlier run made is on disk too): a link must not come back
    after a power loss."""
    for phone in cfg.phones.values():
        if phone.tier != "locked":
            continue
        link = unit_dir() / "default.target.wants" / phone.unit_name
        try:
            if link.is_symlink():  # is_symlink: a link whose unit file is gone still counts
                link.unlink(missing_ok=True)
        except OSError as exc:  # a directory that cannot be searched fails the lookup too: the link's state is unknown
            raise PhoneError(
                Code.USAGE,
                f"cannot check or remove {link} ({exc.strerror or exc}); a locked phone must not start on its own, "
                "remove the link by hand",
                {"unit": phone.unit_name},
            ) from None
    wants = unit_dir() / "default.target.wants"
    try:
        _sync_directory(wants)
    except FileNotFoundError:
        pass  # no login links at all: nothing to make durable
    except OSError as exc:
        raise PhoneError(
            Code.USAGE,
            f"cannot sync {wants} ({exc.strerror or exc}); a locked phone must not start on its own, and a login "
            "link removed from there could come back after a power loss",
            {"path": str(wants)},
        ) from None


def _at(exc: OSError) -> str:
    """The file or files an OSError names, for a message (": a", or ": a -> b" for a rename), else nothing."""
    names = " -> ".join(str(name) for name in (exc.filename, exc.filename2) if name)
    return f": {names}" if names else ""


def _write_unit(path: Path, data: bytes) -> None:
    """Replace a unit file whole or not at all. The data goes to a temp file beside it, is synced, and is renamed over
    the unit: a crash leaves the old unit or the new one, never a locked unit cut off before `Restart=no` and
    `RuntimeMaxSec=`. The temp file's name ends in .tmp, which systemd does not read as a unit, so one left by a crash
    is never loaded. Whatever stands at that name is removed first and the temp file is made fresh (O_EXCL, and
    O_NOFOLLOW besides): a link there would carry the write to another file, or to the live unit itself, cut in
    place, and a pipe would make the open wait for a reader. A unit that exists keeps its mode, as it did when it was
    rewritten in place; a new one gets what any new file gets, 0o666 less the umask."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = None
        tmp.unlink(missing_ok=True)  # a directory cannot be unlinked: that is an error naming it, below
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        with os.fdopen(fd, "wb") as fh:  # buffered: all of the data or an error, never a short write
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())  # the data is on disk before the rename makes it the unit
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise PhoneError(
            Code.USAGE,
            f"cannot write {path} ({exc.strerror or exc}{_at(exc)}); fix that, then run install-units again",
            {"unit": path.name},
        ) from None


def _reload_failed(
    cfg: Config, stopped: list[str], failed: PhoneError, stop: StopPhone, again: str = "install-units"
) -> PhoneError:
    """The error to raise for a daemon-reload that failed, once the running locked phones have been looked at anyway.

    A reload that failed is not a reload that did not happen: our wait may run out while systemd goes on, and it then
    loads what was pending, a drop-in included, into a running unit; or its answer may be lost after it loaded. So the
    look after the reload is made all the same, and stops each running locked phone not confirmed to be exactly the
    configured cap with no extra run time and no reload pending (a unit file rewritten since the manager loaded it counts
    as a reload pending). The error stays the reload's, with the phones stopped (the caller adds the list to its data) and
    the advice to run `again` once systemd is responsive. A look that fails too is kept as a note, and its lists
    as data: `failed`, `not_stopped` and `unread`, as the look gave them, so that the data says what the text does. The phones
    stopped, before the reload and by the look, are in `stopped` (one list, the caller's)."""
    note = ""
    carried: dict[str, object] = {}
    try:
        stop_uncapped_sessions(cfg, stopped, stop, after_reload=True, reload_failed=True, again=again)
    except PhoneError as exc:
        note = f"; the running locked phones could not all be checked afterwards: {exc.message}"
        carried = {key: exc.data[key] for key in ("failed", "not_stopped", "unread") if key in exc.data}
    message = (
        f"{failed.message}; the units are written, but systemd has not confirmed that it loaded them, and it may still "
        f"do so: run {again} again once systemd is responsive{stopped_note(stopped)}{note}"
    )
    data = {**failed.data, **carried}
    if stopped:
        data["stopped"] = list(stopped)
    return PhoneError(failed.code, message, data)


def step_failed(stopped: list[str], failed: PhoneError, what: str, again: str, advice: str = "") -> PhoneError:
    """The error for a step that failed once the units were written (a login link, a start): systemctl's own words, then
    `what`, which says how far the install had got and what could not be done, and the advice to run `again`, the command that
    does it all over (the writes too), with `advice`, what to do first, right before it. The phones this run stopped are in the
    message and in the data, as _reload_failed has them: the sessions ended whatever failed after."""
    message = f"{failed.message}; {what}: {advice}run {again} again{stopped_note(stopped)}"
    data = dict(failed.data)
    if stopped:
        data["stopped"] = list(stopped)
    return PhoneError(failed.code, message, data)


def reload_and_look(cfg: Config, stopped: list[str], stop: StopPhone, again: str = "install-units") -> None:
    """The explicit reload that has the manager load what is on disk, and the look at the running locked phones that always
    follows it.

    They go together: the reload loads whatever is pending (a drop-in's session cap or extra run time, kill mode, stop time
    or SendSIGKILL included) into a running unit as well, and that can uncap a session nobody was looking at. So nothing
    reloads without the look after it, and nothing may change a login link after the last of them: a link made without a
    reload (`enable --no-reload`, which is how every link here is made, so that no reload comes after a look) leaves the
    unit-file state of the whole manager outdated, every unit saying NeedDaemonReload=yes until the next reload, and a locked
    lease is refused for that. A reload that fails is not one that did not happen (see _reload_failed). `stopped` is the
    caller's list, to which the look adds the phones it stops, and `again` is the command that the failures tell the operator
    to run."""
    try:
        systemctl("daemon-reload")  # so the manager loads the disk as it now is
    except PhoneError as failed:
        raise _reload_failed(cfg, stopped, failed, stop, again) from None  # it may still happen: look at the phones anyway
    stop_uncapped_sessions(cfg, stopped, stop, after_reload=True, again=again)  # the reload loads a pending drop-in too


def _server_not_up(stopped: list[str], again: str, what_happened: str, state: str, answers: bool) -> PhoneError:
    """The failure of setup when the adb server it started is not up (see wait_for_adb_server): `what_happened` is how it was
    seen, then that the units are written and no open phone was started, then what to do first when the journal says that
    another server holds the port, which is the expected cause, and the advice to run `again`. `state` and `answers` (does the
    port accept a connection) are in the data, with the unit."""
    failed = PhoneError(Code.PHONE_DOWN, what_happened, {"unit": ADB_SERVER_UNIT, "state": state, "port_answers": answers})
    journal = f"journalctl --user -u {ADB_SERVER_UNIT.removesuffix('.service')} -n 20"
    return step_failed(
        stopped, failed,
        "the units are written, loaded and checked, but the adb server is not up, so no open phone was started", again,
        advice=(
            f"if its journal ({journal}) says `Address already in use`, another adb server holds port {ADB_SERVER_PORT}, "
            f"so run `adb kill-server`, then `systemctl --user start {ADB_SERVER_UNIT}`, then "
        ),
    )


def server_waits() -> tuple[float, float]:
    """The settle and the wait that setup's check for the adb server uses, in seconds: SERVER_SETTLE and SERVER_READY_WAIT, or
    what ORION_PHONE_SERVER_SETTLE and ORION_PHONE_SERVER_WAIT say. Like every time limit, each is a finite number above zero
    (anything else is ignored: a bad value never turns the check off or makes it up), and each is held to its maximum, as the
    watch interval is. They are not tied to each other: a settle that is not below the wait is a check that cannot pass, which
    its failure says in numbers, and a settle shorter than the abort of an adb that finds its port taken (SERVER_SETTLE's
    reason) can let a server that is about to fail pass for one that is up, so the environment that sets them is trusted."""
    settle = min(limit("ORION_PHONE_SERVER_SETTLE", SERVER_SETTLE), MAX_SERVER_SETTLE)
    wait = min(limit("ORION_PHONE_SERVER_WAIT", SERVER_READY_WAIT), MAX_SERVER_WAIT)
    return settle, wait


def wait_for_adb_server(stopped: list[str], again: str) -> None:
    """Return once the adb server's unit, just started, is up, or raise saying that it is not (as every failure of a step
    after the writes does: see step_failed, whose `stopped` and `again` these are). Setup calls this right after it starts the
    unit and before it starts an open phone.

    `systemctl start` is no proof. The unit is Type=simple, which systemd counts as started the moment adb is spawned, not once
    adb has bound its port. Observed on a real host (adb 1.0.41, systemd 255) with another adb server holding port 5037:
    `start` exited 0 at once, and the unit's adb aborted about 0.7 s after it was spawned (`could not install *smartsocket*
    listener: Address already in use`, SIGABRT, a core dump), which RestartPreventExitStatus=SIGABRT leaves failed. The other
    server goes on answering, so a port that answers is no proof either, and a unit that is active proves nothing for as long
    as that abort may still come. With no conflict the server was listening and the phone listed within a few seconds.

    So the server is up when its unit is active and its port accepts a plain connect (at probe_host, with PROBE_TIMEOUT, as
    every other look at a port), and has stayed so, without a break, for the settle, SERVER_SETTLE seconds, which is longer
    than that abort takes. It is looked at every SERVER_POLL seconds (or every settle, when that is shorter, so that the settle
    is not overshot by a whole interval), for at most the wait, SERVER_READY_WAIT, in all (see
    server_waits for how the environment changes the two). A unit that is failed or inactive ends the wait at once, whatever
    answers on the port: nothing it runs serves it. A unit in any other state (activating, say, in the restart loop of a
    server that exits at once) is waited on, and the wait ends at the bound with the failure that it did not stay up. A look
    that cannot be made is a failure too, never a pass.

    Only setup waits like this. install-units starts nothing, and a start at login (the manager starting the unit, or an open
    phone's unit starting it through its Wants= and After=) is not checked: its failure shows in `orion-phone status` (adb
    timed out) and in the unit's journal, as the runbook says."""
    unit, port = ADB_SERVER_UNIT, ADB_SERVER_PORT
    settle, wait = server_waits()
    deadline = time.monotonic() + wait
    steady_since: float | None = None  # since when the unit has been active with its port answering, without a break
    while True:
        try:
            state = _show(unit, "ActiveState").get("ActiveState", "")
        except PhoneError as failed:  # no answer is no pass
            raise step_failed(
                stopped, failed,
                "the units are written, loaded and checked, but the adb server could not be looked at, so no open phone was "
                "started",
                again,
            ) from None
        answers = _accepts(probe_host(), port)
        now = time.monotonic()
        if state in ("failed", "inactive"):  # no process is behind it: whatever answers on the port is not its
            raise _server_not_up(
                stopped, again, f"{unit} is {state} after it was started, so nothing it runs serves port {port}", state, answers
            )
        if state == "active" and answers:
            steady_since = now if steady_since is None else steady_since
            if now - steady_since >= settle:
                return
        else:
            steady_since = None
        if now >= deadline:
            raise _server_not_up(
                stopped, again,
                f"{unit} did not stay up with port {port} answering for {settle:g} s within {wait:g} s "
                f"of its start (it is {state or 'in no state that systemctl printed'}, and port {port} "
                f"{'answers' if answers else 'does not answer'})",
                state, answers,
            )
        time.sleep(min(SERVER_POLL, settle, deadline - now))  # never further apart than the settle: it is not overshot


@dataclass(frozen=True)
class Installed:
    """What install_units did: the unit files it wrote (the debug bridge's server's, then each phone's), and the locked phones
    it stopped (stop_uncapped_sessions)."""

    units: list[Path]
    stopped: list[str]


def install_units(cfg: Config, stop: StopPhone) -> Installed:
    """Write the units and have the manager load them, stopping (with `stop`, which must show the phone gone: see
    stop_uncapped_sessions, and phone.install_units for the one the commands use) each running locked phone whose loaded unit
    does not enforce its cap, and enable the debug bridge's server at login (the link only: nothing is started). The link is
    made before the one reload and the look after it, so that none changes after the last of them (see reload_and_look). There
    is no default for `stop`: a caller must say how a phone is stopped, since a zero exit from `systemctl stop` is not a phone
    that is gone."""
    # A locked phone's stale login link goes first, and durably, before anything else (even before the units are
    # rendered): systemd follows that link whatever its unit says, and removing it is always right. So an install that
    # is refused for any reason (an interpreter path systemd would misread, a byte that is not UTF-8), or that fails
    # or crashes anywhere after this point, still leaves no link that would boot a locked phone at the next login.
    drop_login_links(cfg)
    display = os.environ.get("DISPLAY")
    avd = avd_home()  # after the links: a refused value must not keep one from being removed
    # every unit is rendered (and the interpreter and the adb path checked) and encoded before any unit is touched
    units: dict[str, bytes] = {}
    # The debug bridge's server comes first: every phone's unit names it, so it is on disk before any of them is.
    try:
        units[ADB_SERVER_UNIT] = render_adb_server_unit(cfg).encode("utf-8")  # systemd reads unit files as UTF-8
    except UnicodeEncodeError:  # a path that starts with ~ is expanded from HOME, whose non-UTF-8 byte is a surrogate here
        raise PhoneError(
            Code.USAGE,
            "the unit for the adb server holds text that is not valid UTF-8, which systemd cannot read: fix android.adb, "
            "or the HOME it expands from, then run install-units again",
            {"unit": ADB_SERVER_UNIT},
        ) from None
    for phone in cfg.phones.values():
        text = render_unit(cfg, phone, sys.executable, display, avd)
        try:
            units[phone.unit_name] = text.encode("utf-8")  # systemd reads unit files as UTF-8
        except UnicodeEncodeError:  # a non-UTF-8 byte in an environment variable reaches the text as a surrogate
            raise PhoneError(
                Code.USAGE,
                f"the unit for phone {phone.name} holds text that is not valid UTF-8, which systemd cannot read: fix "
                "DISPLAY, ORION_PHONE_CONFIG, ORION_PHONE_STATE, ANDROID_AVD_HOME, or the path of the Python "
                "interpreter, then run install-units again",
                {"phone": phone.name, "unit": phone.unit_name},
            ) from None
    # Each unit is written whole or not at all, and the manager reloads after all of it.
    target = unit_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PhoneError(
            Code.USAGE,
            f"cannot create {target} ({exc.strerror or exc}{_at(exc)}); fix that, then run install-units again",
            {"path": str(target)},
        ) from None
    stopped: list[str] = []
    written: list[Path] = []
    try:
        # Last before the first write: whatever can still be refused has been, and a phone that cannot be stopped leaves
        # every unit as it was. A locked phone not confirmed to enforce its cap is stopped while its old unit is still on disk.
        stop_uncapped_sessions(cfg, stopped, stop)
        for unit_name, data in units.items():
            path = target / unit_name
            _write_unit(path, data)
            written.append(path)
        try:
            _sync_directory(target)  # the renames above are entries in this directory
        except OSError as exc:  # the units are written: this failure says so, as every one after the writes does
            raise step_failed(
                stopped,
                PhoneError(Code.USAGE, f"cannot sync {target} ({exc.strerror or exc})", {"path": str(target)}),
                "the units are written, but they may not survive a power loss until the sync succeeds",
                "install-units",
            ) from None
        # The adb server runs from login, so that it is there before the first lease, which talks to adb before it starts a
        # phone (see render_adb_server_unit). This makes the link and nothing else: install-units starts nothing, and the
        # enable does not reload the manager (it would, unless told not to, and a reload after the look below would load,
        # unseen, whatever a drop-in changed since: it can uncap a running locked session). It comes before the one explicit
        # reload, not after it: a link made without a reload leaves every unit marked as needing one until the next reload,
        # and a locked lease is refused for that, so a link made after the last reload would leave every lease refused.
        try:
            systemctl("enable", "--no-reload", ADB_SERVER_UNIT)
        except PhoneError as failed:
            raise step_failed(
                stopped, failed, "the units are written, but not loaded, and the adb server could not be enabled at login",
                "install-units",
            ) from None
        reload_and_look(cfg, stopped, stop)  # the last thing that changes anything: the manager loads it, and it is looked at
    except PhoneError as exc:
        if stopped:  # whatever failed, the data lists the sessions this install ended (after the writes, the message does too)
            # A list the error already carries is not replaced by another: this install's own is the order (the phones
            # stopped before the reload, then the look's), and a phone that only the error names comes after them.
            carried = exc.data.get("stopped")
            extra = [name for name in carried if name not in stopped] if isinstance(carried, list) else []
            exc.data = {**exc.data, "stopped": [*stopped, *extra]}
        raise
    return Installed(written, stopped)


def emulator_argv(cfg: Config, phone: PhoneConfig, has_display: bool) -> list[str]:
    argv = [
        str(cfg.sdk_root / "emulator" / "emulator"),
        "-avd", phone.avd_name,
        "-port", str(phone.console_port),
        "-no-boot-anim",
        "-no-audio",
        "-gpu", cfg.gpu,
        # Left to itself the emulator asks, in a dialog, before it reports a crash, and a phone starting again after one
        # would wait behind that dialog until someone answered it.
        "-crash-report-mode", "disabled",
        # Every phone cold-boots (see prepare_launch). A quick-boot snapshot holds the phone's disks as well as its
        # memory, so an open phone brought back from one after a crash came back as it was at its last save, its
        # sign-ins and installs since then gone, and the emulator cuts a save at a stop off after 20 seconds, often
        # before the end.
        "-no-snapshot",
    ]
    if not has_display:
        argv.append("-no-window")
    return argv


def prepare_launch(phone: PhoneConfig) -> Path:
    """Check the AVD home and return it, and delete the phone's snapshots. Every phone cold-boots (-no-snapshot), so a
    snapshot left on disk is only a way back to an old state: loading one, as a launch by hand without -no-snapshot
    would, restores the phone's disks as they were at its save. A locked phone's must go, or the phone does not start:
    one taken while it was unlocked would restore an unlocked phone. An open phone's goes if it can (a link there at
    least does); one that cannot be deleted only costs disk.

    The home is checked first, for every phone, and must be an existing directory: the emulator that follows would look
    for the phone somewhere else, as avdmanager does, if it were not. Setup makes the directory."""
    home = avd_home()
    try:
        there = home.is_dir()
    except OSError as exc:  # a directory this account cannot search
        raise PhoneError(
            Code.USAGE,
            f"cannot look at the AVD home {home} ({exc.strerror or exc})",
            {"phone": phone.name, "path": str(home)},
        ) from None
    if not there:
        raise PhoneError(
            Code.USAGE,
            f"the AVD home {home} is not an existing directory, so the emulator would look for phone {phone.name} "
            "somewhere else; run `orion-phone setup`, which makes it",
            {"phone": phone.name, "path": str(home)},
        )
    snapshots = home / f"{phone.avd_name}.avd" / "snapshots"
    try:
        shutil.rmtree(snapshots)
    except OSError as exc:
        # Only a directory that is gone is fine: rmtree stops at its first error, so a FileNotFoundError from an entry
        # that vanished mid-walk can leave the rest behind. A snapshot left on disk may hold an unlocked phone's memory.
        if isinstance(exc, FileNotFoundError) and not os.path.lexists(snapshots):
            return home
        if phone.tier != "locked":
            # An open phone never loads a snapshot, so one that cannot be deleted only costs disk. A link there goes,
            # though, which rmtree refuses to follow: a launch by hand would follow it to the snapshots it names. What
            # it points to is not the phone's to delete.
            with contextlib.suppress(OSError):
                if os.path.islink(snapshots):
                    os.unlink(snapshots)
            return home
        where = f" at {exc.filename}" if exc.filename else ""
        raise PhoneError(
            Code.USAGE,
            f"cannot delete {snapshots} ({exc.strerror or exc}{where}); a locked phone must keep no snapshots, because "
            "one taken while it was unlocked would restore an unlocked phone: delete it by hand, then try again",
            {"phone": phone.name, "path": str(snapshots)},
        ) from None
    return home


WATCH_INTERVAL = 15.0  # seconds between a locked phone's looks at its lease; ORION_PHONE_WATCH_INTERVAL tunes it
MAX_WATCH_INTERVAL = 300.0  # seconds: the longest the interval can be made, so that no lapse goes unseen for longer
STOP_GRACE = 30.0  # seconds a stopping emulator has to exit on SIGTERM before SIGKILL, and then to be reaped
UNIT_STOP_TIMEOUT = 90  # seconds systemd gives a locked unit to stop (TimeoutStopSec); a stop takes at most two graces
_WATCH_TICK = 0.1  # seconds between looks for the emulator's exit or a request to stop, in between the looks at the lease
ACTION_LOCK_WAIT = 180.0  # seconds a stop for a lapsed lease waits for an action in flight on the phone, then goes ahead
ACTION_LOCK_POLL = 0.2  # seconds between tries for the phone's action lock while the watch's stop, or install-units', waits for it


def lapse_reason(lease: Lease | None, now: float, invocation: str, started: float, bound: bool = False) -> str | None:
    """Why a locked phone's emulator, the one this run of its unit (`invocation`, which began at `started`, wall clock, before
    it launched the emulator) started, must not keep running under the lease on file (None: it may). It may only while there
    is a lease that has not expired and that belongs to this boot. One bound to this run is the session this boot belongs to.
    One bound to no boot is that session only while the watch has not yet seen a lease bound to this boot (`bound` False: the
    boot is in its starting window), and then only if it was taken no later than this run started: the lease flow writes a
    session's lease before it starts the unit and binds it just after, so the session's own boot starts after its lease. A
    lease bound to no boot that was taken after this boot started is a newer session's (a takeover writes its lease before it
    stops the boot it replaces), and if that command died or hung before the stop, this boot, possibly unlocked, would run on
    under it until it expired: it must stop. Once a lease bound to this boot has been seen (`bound` True: the caller keeps
    that, see _Boot), a lease bound to no boot is never this boot's session, whatever time it says: its session's lease was
    bound, and a lease with no boot after that is a newer session's. No lease, an expired one, one bound to another boot, or
    one bound to none that this boot cannot be the session of means no session owns this boot.

    Wall clock on both sides of the time rule (the lease records when it was taken, and a lease file is written by other
    processes), which is the only place a clock matters, and only before the first binding. A clock stepped back between a
    lease's write and the start of its own boot makes the watch stop that boot, which fails closed. A step back by more than
    the age of this boot, between its start and its binding (a boot whose binding never comes stays there), before a takeover
    writes its lease, would let that lease read as older than the boot, so the stop of the old boot would wait for the
    takeover command: the one case left that a clock decides."""
    if lease is None:
        return "no lease is held on the phone"
    if lease.expires_at <= now:
        return f"the lease of {lease.holder} has expired"
    if lease.invocation:
        if lease.invocation != invocation:
            return f"the lease of {lease.holder} is for another boot of the phone"
    elif bound:
        return (
            f"the lease of {lease.holder} is bound to no boot, though a lease was bound to this one: a newer session, "
            "which has not stopped this boot yet"
        )
    elif lease.acquired_at > started:
        return (
            f"the lease of {lease.holder} was taken after this boot started and is not bound to it: a newer session, "
            "which has not stopped this boot yet"
        )
    return None


def _note(message: str) -> None:
    """A line for the journal, which is where a unit's standard error goes: why a phone shut down. A line that cannot be
    written (a closed pipe) is lost: logging must never change whether the emulator is stopped. It goes straight to file
    descriptor 2, not through sys.stderr, whose buffer would keep the failed line and fail again as the process exits."""
    try:
        os.write(2, f"orion-phone: {message}\n".encode("utf-8", "replace"))
    except OSError:
        pass


@dataclass
class _Boot:
    """What the watch of one run of a locked unit knows of its own boot: its `invocation`, when it `started` (wall clock,
    before its emulator was launched), and whether a lease bound to it has been seen (`bound`, set the first time one is,
    never cleared: from then on a lease bound to no boot is a newer session's, whatever time it says)."""

    invocation: str
    started: float
    bound: bool = False


def _look_at_the_lease(leases: LeaseStore, name: str, boot: _Boot) -> str | None:
    """lapse_reason for the lease as it is on file now, read without the lease lock (a release holds that lock while it
    stops this very process, so a watcher that waited for it would not answer the stop). A lease that cannot be read, for
    whatever reason, is a reason to stop: nothing here keeps a phone running on a guess. A lease bound to this boot, even
    one that has expired, is the binding the latch waits for (a lease bound to another boot, or to none, is not)."""
    try:
        lease = leases.read(name)
    except Exception as exc:  # a damaged file (USAGE), one that cannot be opened (OSError), anything: fail closed
        return f"the lease on the phone cannot be read ({exc})"
    if lease is not None and lease.invocation and lease.invocation == boot.invocation:
        boot.bound = True
    return lapse_reason(lease, time.time(), boot.invocation, boot.started, boot.bound)


def _stop_emulator(child: subprocess.Popen) -> None:
    """Stop the emulator and reap it: SIGTERM, then SIGKILL once STOP_GRACE has passed, then wait for it to be gone. A
    child that is already gone is only reaped, so this is safe to run on every way out of the watch."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=STOP_GRACE)
        return
    except subprocess.TimeoutExpired:
        _note(f"the emulator ignored SIGTERM for {STOP_GRACE:g} s and is killed")
    child.kill()
    try:
        child.wait(timeout=STOP_GRACE)  # SIGKILL cannot be caught; this only runs out for a process the kernel cannot end
    except subprocess.TimeoutExpired:
        _note(f"the emulator (pid {child.pid}) is still there {STOP_GRACE:g} s after SIGKILL")


def _stop_for_a_lapse(leases: LeaseStore, name: str, boot: _Boot, child: subprocess.Popen, give_up: bool) -> str:
    """One try at the stop that a lapsed lease asks for, made under the phone's action lock like every other thing that
    shuts the phone down (release, a new session): the phone is not shut down under an action in flight. Returns `waiting`
    (an action holds the lock and the bound has not run out: try again), `kept` (the lease, read again once the lock is held,
    stands after all) or `stopped` (the emulator is stopped and reaped, with the lock held until it is gone).

    The lock is tried for, never waited on: a stop of this unit (release, a new session, the session cap) must be answered
    while the stop waits, and release() holds the lock as it stops the unit. With `give_up` (the bound ran out) the stop goes
    ahead without the lock, since an action that never ends must not keep a lapsed phone running; and so it does where the
    lock cannot be opened at all (waiting for it would be waiting for nothing). The lease is read again before any stop:
    under the lock when it is held, so that what is decided is what the lock's holders left."""
    with contextlib.ExitStack() as stack:
        try:
            held = stack.enter_context(leases.try_acting(name))
        except OSError as exc:  # no lock to wait for: fail closed, now
            _note(f"the action lock of phone {name} cannot be opened ({exc}), so it is not waited for")
            held = None
        if held is False and not give_up:
            return "waiting"
        why = _look_at_the_lease(leases, name, boot)  # now: the lease as the lock's holders left it
        if why is None:
            return "kept"
        if held is False:
            _note(
                f"stopping the emulator of phone {name} anyway: an action has held the phone for {ACTION_LOCK_WAIT:g} s, "
                f"and {why}"
            )
        else:
            _note(f"stopping the emulator of phone {name}: {why}")
        _stop_emulator(child)  # the lock, if it is held, is kept until the emulator is gone
        return "stopped"


def _exit_status(returncode: int) -> int:
    """The status `_run` exits with when the emulator exited on its own: its own, or 128 plus the signal that killed it."""
    return 128 - returncode if returncode < 0 else returncode


def supervise(cfg: Config, phone: PhoneConfig, argv: list[str]) -> int:
    """Run a locked phone's emulator as a child of this process and keep it only while its session's lease stands. Returns
    the status `_run` exits with.

    Without this, a lease that lapsed would leave a locked phone running, possibly unlocked, until its session cap or the
    next lease. Every WATCH_INTERVAL seconds (ORION_PHONE_WATCH_INTERVAL) the lease is read from the state directory this
    unit carries, and the emulator is stopped when lapse_reason says no session owns this boot, and when the lease file
    cannot be read. The first look comes one interval after the start, by which time the lease flow has bound the lease to
    this boot. A lease bound to no boot is this boot's own session only while no lease bound to this boot has been seen, and
    then only if it was taken before this run started (it records its start time for that): a takeover writes such a lease
    before it stops this boot, and one taken after the start, or any one after this boot has been bound, is that newer
    session's, which has not yet ended the boot.

    That stop is not made under an action in flight: like a release and a new session, which hold the phone's action lock
    while they stop the unit, it takes that lock first (see _stop_for_a_lapse), trying for it every ACTION_LOCK_POLL seconds
    for at most ACTION_LOCK_WAIT, never blocking on it, so that SIGTERM is answered while it waits. It then reads the lease
    again, and stops only if it still does not stand.

    A stop is SIGTERM to the emulator and, STOP_GRACE seconds later, SIGKILL, and `_run` then exits 0: an intended stop, so
    the unit ends inactive rather than failed. SIGTERM to this process (systemctl stop, the session cap) is the same stop.
    An emulator that exits on its own ends `_run` with its status. The emulator is reaped on every way out, an exception
    included; and if this process is itself killed, the unit's control group (KillMode=control-group) takes the emulator
    with it, as it did when the emulator was the unit's main process.
    """
    invocation = os.environ["INVOCATION_ID"]
    leases = LeaseStore(cfg.state_dir)
    interval = min(limit("ORION_PHONE_WATCH_INTERVAL", WATCH_INTERVAL), MAX_WATCH_INTERVAL)
    asked: list[int] = []  # the signals this process was sent: any of them means stop
    # wall clock, before the emulator exists (see lapse_reason: what a lease taken after it means, and what the latch adds)
    boot = _Boot(invocation, time.time())

    def on_signal(signum: int, frame: object) -> None:
        asked.append(signum)

    handled = (signal.SIGTERM, signal.SIGINT)
    saved = [signal.signal(sig, on_signal) for sig in handled]  # before the emulator starts: no request is missed
    try:
        _note(f"phone {phone.name}: its emulator runs only while its lease stands (looked at every {interval:g} s)")
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise PhoneError(
                Code.USAGE, f"cannot start the emulator {argv[0]}: {exc.strerror or exc}", {"phone": phone.name}
            ) from None
        try:
            next_look = time.monotonic() + interval
            lapsed: str | None = None  # why the lease did not stand at a look: the stop is then waiting for its turn
            give_up_at = next_try = 0.0
            told = False  # has the log said that the stop is waiting for an action
            while True:
                if asked:
                    _note(f"stopping the emulator of phone {phone.name}: this unit was told to stop (signal {asked[0]})")
                    return 0
                code = child.poll()
                if code is not None:
                    _note(f"the emulator of phone {phone.name} exited with status {code}")
                    return _exit_status(code)
                now = time.monotonic()
                if lapsed is None and now >= next_look:
                    lapsed = _look_at_the_lease(leases, phone.name, boot)
                    if lapsed:
                        give_up_at, next_try, told = now + ACTION_LOCK_WAIT, now, False  # the first try is at once
                    else:
                        next_look = now + interval
                if lapsed is not None and now >= next_try:
                    outcome = _stop_for_a_lapse(leases, phone.name, boot, child, now >= give_up_at)
                    if outcome == "stopped":
                        return 0
                    if outcome == "kept":  # the lease stands after all: back to looking at it
                        lapsed, next_look = None, now + interval
                    else:
                        if not told:  # said once, at the first try that finds the phone in use
                            told = True
                            _note(
                                f"phone {phone.name}: {lapsed}; waiting for an action in flight on it to end (at most "
                                f"{ACTION_LOCK_WAIT:g} s) before its emulator is stopped"
                            )
                        next_try = now + ACTION_LOCK_POLL
                time.sleep(min(_WATCH_TICK, max((next_look if lapsed is None else next_try) - time.monotonic(), 0.001)))
        finally:
            _stop_emulator(child)  # every way out, the emulator is stopped and reaped; a no-op when it exited by itself
    finally:
        for sig, previous in zip(handled, saved):
            signal.signal(sig, previous if previous is not None else signal.SIG_DFL)


def run_phone(cfg: Config, name: str, print_only: bool) -> Result:
    phone = cfg.phone(name)
    if not print_only and not os.environ.get("INVOCATION_ID"):  # systemd sets it for the processes it runs
        # A guard against starting a phone by hand, not a security boundary: any process could set the variable.
        if phone.tier == "locked":  # starting its unit by hand would only have it shut down at the watch's first look
            raise PhoneError(
                Code.USAGE,
                f"phone {phone.name} is locked, and a locked phone is started by a lease, not by hand: run "
                f"`orion-phone lease {phone.name} --holder NAME`, which starts its unit under a session cap and a lease "
                "(_run is what that unit runs, and a locked unit started with no lease shuts its phone down at the first "
                "look)",
                {"phone": phone.name, "unit": phone.unit_name},
            )
        raise PhoneError(
            Code.USAGE,
            f"run phones through their systemd units (systemctl --user start {phone.unit_name}); _run is what a unit "
            "runs, and a phone started any other way has no session cap",
        )
    # A locked phone's owner unlocks it on its screen: without a window it would be a lock screen nobody can see. The
    # emulator draws through X11 (Wayland desktops provide it through XWayland), so DISPLAY is what it needs. An open
    # phone has nothing to unlock and may run headless.
    if phone.tier == "locked" and not os.environ.get("DISPLAY"):
        raise PhoneError(
            Code.USAGE,
            f"phone {phone.name} is locked, so its owner unlocks it on its screen, and it needs an X11 display, but "
            "DISPLAY is not set here (a unit carries the DISPLAY of the shell that installed it); install the units "
            "from a desktop shell with DISPLAY set (orion-phone install-units), then lease it again",
            {"phone": phone.name, "unit": phone.unit_name},
        )
    home = prepare_launch(phone)
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    argv = emulator_argv(cfg, phone, has_display)
    if print_only:
        return Result(Code.OK, "emulator command line (not started)", {"argv": argv})
    if not Path(argv[0]).exists():
        raise PhoneError(Code.USAGE, f"emulator not found at {argv[0]}; run `orion-phone setup` first")
    os.environ["ANDROID_SDK_ROOT"] = str(cfg.sdk_root)
    os.environ["ANDROID_AVD_HOME"] = str(home)  # told, whatever the unit held: the emulator finds the phones here
    if phone.tier == "locked":  # its lease decides how long it runs: supervised, as a child, and `_run` exits with its end
        raise SystemExit(supervise(cfg, phone, argv))
    os.execv(argv[0], argv)  # an open phone's emulator is the unit's own process, and no lease ends it
    raise AssertionError("unreachable: execv replaces this process")
