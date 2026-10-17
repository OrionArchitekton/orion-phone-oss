"""One-time operator setup: SDK packages, phones, AVD settings, units. Licenses must already be accepted."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from orion_phone import services
from orion_phone.codes import Code, PhoneError, Result
from orion_phone.config import Config
from orion_phone.phone import install_units, reload_and_look


def _tool(cfg: Config, name: str) -> Path:
    path = cfg.sdk_root / "cmdline-tools" / "latest" / "bin" / name
    if not path.exists():
        raise PhoneError(
            Code.USAGE,
            f"{name} not found at {path}; install the Android command-line tools into "
            f"{cfg.sdk_root / 'cmdline-tools' / 'latest'} first (see README)",
        )
    return path


TOOL_TIMEOUT = 3600  # seconds one SDK tool call may take; the system image download is the long one


def _run(argv: list[str], stdin: str = "", env: dict[str, str] | None = None) -> None:
    tool = Path(argv[0]).name
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, errors="replace", timeout=TOOL_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        raise PhoneError(Code.USAGE, f"{tool} did not finish within {TOOL_TIMEOUT // 60} minutes") from None
    except OSError as exc:  # not executable, a directory, a broken interpreter line
        raise PhoneError(Code.USAGE, f"cannot run {tool}: {exc.strerror or exc}") from None
    if proc.returncode != 0:
        # both streams: the 23.0 sdkmanager prints a deprecation banner on stderr every time, so stderr is never empty
        detail = "\n".join(text.strip() for text in (proc.stderr, proc.stdout) if text.strip())[-800:]
        raise PhoneError(Code.USAGE, f"{tool} failed (exit {proc.returncode})" + (f": {detail}" if detail else ""))


def sdk_package(path: str) -> str:
    """sdkmanager (command-line tools 23+) lists and installs slash paths; avdmanager still takes semicolons."""
    return path.replace(";", "/")


def set_ini(path: Path, values: dict[str, str]) -> None:
    """Set these keys in an AVD's config.ini and leave every other line exactly as it was. The file comes from the Android
    tools and the system image, so a value may hold characters that `str.splitlines`, or the universal newlines of a text
    read, would take for the end of a line (U+2028, U+0085, a lone CR, a form feed): it is read as it stands and cut on a
    newline only, so such a value stays one line and cannot grow a setting line of its own."""
    if path.exists():
        with open(path, encoding="utf-8", newline="") as fh:
            lines = fh.read().split("\n")
        if lines[-1] == "":  # the file's last newline ends its last line: it does not start another
            lines.pop()
    else:
        lines = []
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip(" \t")
        if key in values:
            out.append(f"{key} = {values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{key} = {value}" for key, value in values.items() if key not in seen]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(out) + "\n")


def _make_avd_home(home: Path) -> None:
    """The AVD home must exist before avdmanager runs: the real tool ignores an ANDROID_AVD_HOME that is not an existing
    directory and puts the phone in its own default home instead. A new directory is owner-only, since it will hold the
    phones' data; one that is already there is left exactly as it is."""
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:  # permission denied, a file in the way, a full disk
        raise PhoneError(
            Code.USAGE,
            f"cannot make the AVD home {home} ({exc.strerror or exc}); fix that, or choose another with ANDROID_AVD_HOME",
            {"path": str(home)},
        ) from None


def run(cfg: Config) -> Result:
    # First, before any tool runs or anything is downloaded: the effective AVD home, checked (a bad one stops setup here).
    avd_home = services.avd_home()
    sdkmanager = _tool(cfg, "sdkmanager")
    avdmanager = _tool(cfg, "avdmanager")
    _make_avd_home(avd_home)
    _run([str(sdkmanager), f"--sdk_root={cfg.sdk_root}", "platform-tools", "emulator", sdk_package(cfg.system_image)])
    # avdmanager is told the home as setup resolved it, never the environment's own value: the real tool ignores one that
    # is not there, and cannot follow a `..` through a directory that is not there.
    tool_env = {**os.environ, "ANDROID_AVD_HOME": str(avd_home)}
    created = []
    for phone in cfg.phones.values():
        avd_dir = avd_home / f"{phone.avd_name}.avd"
        if not avd_dir.exists():
            _run(
                [str(avdmanager), "create", "avd", "-n", phone.avd_name, "-k", cfg.system_image, "-d", cfg.device],
                stdin="no\n",
                env=tool_env,
            )
            if not avd_dir.is_dir():  # it reported success and put the phone elsewhere, or nowhere
                raise PhoneError(
                    Code.USAGE,
                    f"avdmanager finished without an error but did not make {avd_dir}; look for {phone.avd_name} with "
                    "`avdmanager list avd`, and check ANDROID_AVD_HOME and ANDROID_USER_HOME",
                    {"phone": phone.name, "path": str(avd_dir)},
                )
            created.append(phone.name)
        set_ini(
            avd_dir / "config.ini",
            {"hw.keyboard": "yes", "hw.ramSize": str(cfg.ram_mb), "disk.dataPartition.size": cfg.data_partition},
        )
        services.prepare_launch(phone)
    installed = install_units(cfg)  # not services.install_units: a locked phone it stops must be shown gone
    stopped = list(installed.stopped)  # and the look below may stop more
    open_units = [phone.unit_name for phone in cfg.phones.values() if phone.tier == "open"]
    # Every login link is made before a reload, and a reload is always followed by the look at the running locked phones: a
    # link made without a reload (--no-reload, so that none comes after a look: a reload can load, unseen, what a drop-in
    # changed since, and uncap a session) leaves every unit saying NeedDaemonReload=yes until the next reload, and a locked
    # lease is refused for that. The install has made the server's link and reloaded, and the open phones' links come after
    # that reload, so setup reloads once more, and looks again, before it starts anything. The starts come last and change no
    # link. The server is started first and before any phone, whatever its tier: a lease talks to adb before it starts a
    # locked phone, so the server must already be running, and nothing a phone's own start does could start it in time.
    # Its start is no proof that it is up (its unit counts as started once adb is spawned, and adb may abort a moment later
    # because another server holds the port), so setup waits until it is before it starts an open phone.
    # A failure from here on says that the units are written and what to run again, and names the phones stopped so far.
    for unit in open_units:
        try:
            services.systemctl("enable", "--no-reload", unit)
        except PhoneError as failed:
            raise services.step_failed(
                stopped, failed, f"the units are written and loaded, but {unit} could not be enabled at login", "setup"
            ) from None
    reload_and_look(cfg, stopped, "setup")

    def start(unit: str) -> None:
        try:
            services.systemctl("start", unit)
        except PhoneError as failed:
            raise services.step_failed(
                stopped, failed, f"the units are written, loaded and checked, but {unit} could not be started", "setup"
            ) from None

    start(services.ADB_SERVER_UNIT)
    services.wait_for_adb_server(stopped, "setup")  # no open phone until the server is up (see there)
    for unit in open_units:
        start(unit)
    return Result(
        Code.OK,
        f"setup done; created {len(created)} phones" + services.stopped_note(stopped),
        {"created": created, "units": [str(u) for u in installed.units], "stopped": stopped},
    )
