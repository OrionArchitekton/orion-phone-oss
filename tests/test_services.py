"""Units carry the locked tier's session cap; locked launches never use snapshots."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orion_phone import config, services
from orion_phone import phone as phone_module
from orion_phone.codes import Code, PhoneError
from orion_phone.config import PhoneConfig


def completed(command, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """What `subprocess.run` hands back for a command that printed these: bytes, which the code decodes itself (a capture in
    text mode would translate line ends before anything could look at them)."""
    return subprocess.CompletedProcess(command, returncode, stdout.encode("utf-8"), stderr.encode("utf-8"))


def test_install_units_writes_one_unit_per_phone(phone_env):
    code, _ = phone_env.run("install-units")
    assert code == 0
    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    a = (units / "orion-phone-a.service").read_text()
    b = (units / "orion-phone-b.service").read_text()
    assert "Restart=on-failure" in a and "RuntimeMaxSec" not in a
    assert "Restart=no" in b and "RuntimeMaxSec=7200" in b
    assert "-P -m orion_phone _run b" in b
    assert "DISPLAY" not in a
    assert ["--user", "daemon-reload"] in phone_env.calls("systemctl")


def test_a_locked_unit_names_its_kill_mode_and_its_stop_time_and_an_open_unit_does_not(phone_env):
    assert phone_env.run("install-units")[0] == 0
    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    a = (units / "orion-phone-a.service").read_text().splitlines()
    b = (units / "orion-phone-b.service").read_text().splitlines()
    assert "KillMode=control-group" in b  # whatever the emulator started goes with the stop, as it always did by default
    assert f"TimeoutStopSec={services.UNIT_STOP_TIMEOUT}" in b  # the stop time the watch's grace is made to fit
    assert not any(line.startswith(("KillMode", "TimeoutStopSec")) for line in a)


def test_two_graces_fit_in_the_time_systemd_gives_a_locked_unit_to_stop():
    # a stop waits one grace for SIGTERM and, if it must, a second after SIGKILL; both end before systemd kills the unit
    assert 2 * services.STOP_GRACE < services.UNIT_STOP_TIMEOUT


def test_units_carry_the_installing_display(phone_env):
    phone_env.env["DISPLAY"] = ":0"
    phone_env.run("install-units")
    unit = (Path(phone_env.env["ORION_PHONE_UNIT_DIR"]) / "orion-phone-a.service").read_text()
    assert 'Environment="DISPLAY=:0"' in unit


def test_unit_values_escape_systemd_specifiers(phone_env, tmp_path):
    state = tmp_path / "state%h"
    phone_env.env["ORION_PHONE_STATE"] = str(state)
    assert phone_env.run("install-units")[0] == 0
    unit = (Path(phone_env.env["ORION_PHONE_UNIT_DIR"]) / "orion-phone-a.service").read_text()
    assert f'Environment="ORION_PHONE_STATE={tmp_path}/state%%h"' in unit


def test_locked_launch_purges_snapshots_and_disables_them(phone_env):
    phone_env.env["DISPLAY"] = ":0"  # a locked phone needs one: its owner unlocks it on its screen
    snapshots = Path(phone_env.env["ANDROID_AVD_HOME"]) / "orion-phone-b.avd" / "snapshots" / "default_boot"
    snapshots.mkdir(parents=True)
    code, out = phone_env.run("_run", "b", "--print")
    assert code == 0
    assert "-no-snapshot" in out["data"]["argv"]
    assert "-no-window" not in out["data"]["argv"]  # it has a window
    assert not snapshots.parent.exists()


def test_open_launch_cold_boots_without_its_old_snapshots_and_runs_headless_without_a_display(phone_env):
    # A quick-boot snapshot holds the phone's disks too: an open phone brought back from one after a crash came back as
    # it was at its last save, sign-ins and installs since then gone, and the emulator cuts a save at a stop off after
    # 20 seconds. So every phone cold-boots, and a snapshot left on disk goes: loading it by hand would roll the phone
    # back.
    snapshots = snapshots_of(phone_env, "a") / "default_boot"
    snapshots.mkdir(parents=True)
    code, out = phone_env.run("_run", "a", "--print")  # tests run without a display
    assert code == 0 and "-no-snapshot" in out["data"]["argv"], out
    assert "-no-window" in out["data"]["argv"]
    assert not snapshots.parent.exists()


def test_an_open_launch_removes_a_link_at_its_snapshots_and_leaves_what_it_points_to(phone_env, tmp_path):
    # rmtree refuses a link, and a launch by hand would follow one to the snapshots it names, so the link itself goes;
    # what it points to is not the phone's to delete.
    snapshots = snapshots_of(phone_env, "a")
    snapshots.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "default_boot").mkdir(parents=True)
    snapshots.symlink_to(elsewhere)
    code, out = phone_env.run("_run", "a", "--print")
    assert code == 0 and "-no-snapshot" in out["data"]["argv"], out
    assert not os.path.lexists(snapshots)
    assert (elsewhere / "default_boot").is_dir()


def test_every_launch_turns_off_the_emulators_crash_report_prompt(phone_env):
    # Left to itself, the emulator asks in a dialog before it reports a crash, and a phone starting again after one waits
    # behind that dialog until someone answers it, so no restart would be unattended.
    phone_env.env["DISPLAY"] = ":0"  # a locked phone needs one
    for name in ("a", "b"):
        code, out = phone_env.run("_run", name, "--print")
        argv = out["data"]["argv"]
        assert code == 0 and argv[argv.index("-crash-report-mode") + 1] == "disabled", out


def test_every_unit_keeps_its_phones_memory_out_of_crash_dumps(phone_env):
    # An open phone's memory holds the sessions of the accounts it is signed in to, and a locked phone's, once its owner
    # has unlocked it, what the lock protects. A dump runs to gigabytes (9.2 GB for one crash of an open phone, which
    # waited for it to be written before starting again). LimitCORE=1, not 0: a crash handler the kernel pipes dumps to
    # (systemd-coredump, WSL2's crash capture) takes one whatever the limit, except at 1, where the kernel drops it
    # (checked on WSL2: a crash under 0 was captured, under 1 it was not).
    assert phone_env.run("install-units")[0] == 0
    for name in ("a", "b"):
        assert "LimitCORE=1" in unit_text(phone_env, name).splitlines(), name


# A snapshot taken while a locked phone was unlocked holds that phone's memory, so a launch that could not delete one
# must stop and say so, never go on with the snapshot still there. Only a directory that is already gone is fine.


def snapshots_of(env, name: str) -> Path:
    return Path(env.env["ANDROID_AVD_HOME"]) / f"orion-phone-{name}.avd" / "snapshots"


def test_a_locked_launch_that_cannot_delete_a_snapshot_directory_stops_naming_it(phone_env, tmp_path):
    phone_env.env["DISPLAY"] = ":0"  # so that the snapshot is what stops the launch
    snapshots = snapshots_of(phone_env, "b")
    snapshots.parent.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    snapshots.symlink_to(tmp_path / "elsewhere")  # rmtree refuses a link: a stand-in for any directory it cannot delete
    code, out = phone_env.run("_run", "b", "--print")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(snapshots) in out["message"] and "no snapshots" in out["message"] and "by hand" in out["message"]
    assert snapshots.is_symlink()  # nothing was touched


def test_a_denied_snapshot_deletion_is_usage_naming_the_path(in_process, monkeypatch):
    snapshots = services.avd_home() / "orion-phone-b.avd" / "snapshots"

    def rmtree(path, ignore_errors=False, **kwargs):  # as shutil.rmtree is: ignore_errors swallows what it hits
        if not ignore_errors:
            raise PermissionError(13, "Permission denied", str(path) + "/default_boot")

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with pytest.raises(PhoneError) as caught:
        services.prepare_launch(config.load().phone("b"))
    assert caught.value.code is Code.USAGE
    assert str(snapshots) in caught.value.message and "Permission denied" in caught.value.message
    assert caught.value.data == {"phone": "b", "path": str(snapshots)}


def test_a_vanished_entry_that_leaves_the_directory_behind_is_not_a_missing_directory(in_process, monkeypatch):
    snapshots = services.avd_home() / "orion-phone-b.avd" / "snapshots"
    (snapshots / "default_boot").mkdir(parents=True)

    def rmtree(path, ignore_errors=False, **kwargs):  # rmtree stops at its first error and leaves the rest
        raise FileNotFoundError(2, "No such file or directory", str(path) + "/default_boot/ram.bin")

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with pytest.raises(PhoneError) as caught:
        services.prepare_launch(config.load().phone("b"))
    assert caught.value.code is Code.USAGE and str(snapshots) in caught.value.message


def test_a_locked_launch_with_no_snapshots_to_delete_is_fine(phone_env):
    phone_env.env["DISPLAY"] = ":0"
    assert not snapshots_of(phone_env, "b").exists()
    code, out = phone_env.run("_run", "b", "--print")
    assert code == 0 and "-no-snapshot" in out["data"]["argv"], out


def test_an_open_phones_launch_deletes_only_its_snapshots_and_goes_on_when_it_cannot(in_process, monkeypatch):
    asked = []

    def rmtree(path, ignore_errors=False, **kwargs):  # as shutil.rmtree is when denied: it raises
        asked.append(Path(path))
        raise PermissionError(13, "Permission denied", str(path) + "/default_boot")

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    home = services.prepare_launch(config.load().phone("a"))  # no error: it never loads a snapshot anyway
    assert asked == [home / "orion-phone-a.avd" / "snapshots"]  # its own snapshots, and nothing else


# Unit text is parsed by systemd, not by a shell: what it unescapes, what it expands, and what it never touches.

BS = chr(92)  # a backslash
NL = chr(10)
BAD_INTERPRETERS = {
    "space": "/opt/my python/bin/python",
    "tab": "/opt/my" + chr(9) + "python",
    "newline": "/opt/my" + NL + "python",
    "double quote": '/opt/my"python',
    "single quote": "/opt/my'python",
    "backslash": "/opt/my" + BS + "python",
    "dollar": "/opt/$HOME/python",
    "percent": "/opt/my%hpython",
    # what is neither blank nor one of those, yet is not text a unit file can carry: unit_unsafe refuses a control character
    "control character": "/opt/my" + chr(1) + "python",
    "escape character": "/opt/my" + chr(27) + "python",
    "delete character": "/opt/my" + chr(0x7F) + "python",
    "C1 control character": "/opt/my" + chr(0x9F) + "python",
}


def unit_text(env, phone: str) -> str:
    return (Path(env.env["ORION_PHONE_UNIT_DIR"]) / f"orion-phone-{phone}.service").read_text()


def test_only_open_units_can_be_enabled(phone_env):
    assert phone_env.run("install-units")[0] == 0
    a, b = unit_text(phone_env, "a"), unit_text(phone_env, "b")
    assert "[Install]" in a and "WantedBy=default.target" in a
    assert "[Install]" not in b and "WantedBy" not in b  # nothing may start a locked phone on its own


def test_a_percent_and_a_backslash_in_a_state_path_are_doubled_in_the_unit(phone_env, tmp_path):
    phone_env.env["ORION_PHONE_STATE"] = str(tmp_path / ("a%h" + BS + "b"))
    assert phone_env.run("install-units")[0] == 0
    want = f'Environment="ORION_PHONE_STATE={tmp_path}/a%%h{BS}{BS}b"'  # systemd unescapes both inside the quotes
    assert want in unit_text(phone_env, "a") and want in unit_text(phone_env, "b")


def test_the_exec_line_carries_the_interpreter_path_as_it_is(in_process):
    cfg = config.load()
    unit = services.render_unit(cfg, cfg.phone("a"), "/opt/py-3.11/bin/python3", None, Path("/data/avd"))
    assert "ExecStart=/opt/py-3.11/bin/python3 -P -m orion_phone _run a" + NL in unit  # systemd does not unescape $$ here


@pytest.mark.parametrize("path", BAD_INTERPRETERS.values(), ids=BAD_INTERPRETERS)
def test_install_units_refuses_an_interpreter_path_systemd_would_misread(phone_env, in_process, monkeypatch, path):
    monkeypatch.setattr(sys, "executable", path)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and "interpreter" in caught.value.message
    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    assert not units.exists() or not list(units.iterdir())  # nothing half installed
    assert phone_env.calls("systemctl") == []  # and no reload


# The debug bridge's server is a service of its own. adb starts its own server only when its first connect to loopback port
# 5037 is refused, and under WSL2's mirrored networking a connect to a port with nothing listening is dropped and hangs instead,
# so with no server running every adb call waits out its time limit (`adb timed out after 30s`). A lease on a locked phone
# talks to adb before it starts the phone's unit, so the phone's own start cannot be what starts the server: it runs from
# login, whatever the tiers. install-units writes the unit and enables it (the login link only, nothing is started); setup
# starts it, before the phones; every phone's unit also wants it and starts after it. A link made without a reload leaves
# every unit marked as needing one until the next reload (on a real host, systemd 255; the fake models it), and a locked lease
# is refused for that: so every link is made before a reload, and the look at the running locked phones follows each reload.


def server_unit_text(env) -> str:
    return (Path(env.env["ORION_PHONE_UNIT_DIR"]) / services.ADB_SERVER_UNIT).read_text()


def unit_section(text: str, name: str) -> list[str]:
    """The non-empty lines of one section of a unit file: from its [name] header to the next header, or the end."""
    lines = text.splitlines()
    start = lines.index(f"[{name}]") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    return [line for line in lines[start:end] if line]


def toml_string(text: str) -> str:
    """`text` as a TOML basic string: a backslash, a double quote, and every control character escaped (DEL too, which JSON
    leaves bare and TOML does not allow)."""
    escaped = "".join(
        "\\" + c if c in '"\\' else f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in text
    )
    return f'"{escaped}"'


def set_adb(env, path: str) -> None:
    """Make `path` the android.adb of the tests' config."""
    config_file = Path(env.env["ORION_PHONE_CONFIG"])
    text = config_file.read_text()
    changed = re.sub(r"^adb = .*$", lambda _: f"adb = {toml_string(path)}", text, flags=re.MULTILINE)
    assert changed != text
    config_file.write_text(changed)


def test_install_units_writes_a_unit_that_runs_the_adb_server_in_the_foreground(phone_env):
    assert phone_env.run("install-units")[0] == 0
    text = server_unit_text(phone_env)
    adb = phone_env.root / "bin" / "adb"  # the android.adb the tests' config names
    assert unit_section(text, "Service") == [
        "Type=simple", f"ExecStart={adb} -L tcp:localhost:5037 nodaemon server", "Restart=always", "RestartSec=2",
        "RestartPreventExitStatus=SIGABRT",
    ]
    assert [line.split("=")[0] for line in unit_section(text, "Unit")] == ["Description"]  # it wants nothing, waits for nothing
    assert [line for line in text.splitlines() if line.startswith("[")] == ["[Unit]", "[Service]", "[Install]"]


def test_the_adb_server_listens_on_loopback_port_5037_whatever_the_environment_says(phone_env):
    # The address is given on the command line, not left to ADB_SERVER_SOCKET or ANDROID_ADB_SERVER_PORT, which the user
    # manager's environment may hold; and never -a, which would have the server listen on every interface.
    assert phone_env.run("install-units")[0] == 0
    exec_line = next(line for line in unit_section(server_unit_text(phone_env), "Service") if line.startswith("ExecStart="))
    assert exec_line == f"ExecStart={phone_env.root / 'bin' / 'adb'} -L tcp:localhost:5037 nodaemon server"
    assert "-a" not in exec_line.split()


def test_the_adb_server_listens_where_every_adb_call_goes(phone_env):
    # One address, written once: the server's unit gives it to the server, and every adb call gives it to the client, so a
    # call is never sent to a place where the server is not. (An adb client left to the environment would follow whatever
    # ADB_SERVER_SOCKET, ANDROID_ADB_SERVER_ADDRESS or ANDROID_ADB_SERVER_PORT says: see test_adb_endpoint.)
    assert phone_env.run("install-units")[0] == 0
    exec_line = next(line for line in unit_section(server_unit_text(phone_env), "Service") if line.startswith("ExecStart="))
    words = exec_line.split()
    listens_at = words[words.index("-L") + 1]
    assert phone_env.run("status", "a")[0] == 0
    calls = phone_env.calls("adb_raw")
    assert calls and all(call[0] == "-L" for call in calls), calls
    assert {call[1] for call in calls} == {listens_at} == {services.ADB_SERVER_ADDRESS}


def test_the_adb_server_is_started_again_when_it_ends_but_not_after_an_abort(phone_env):
    # `adb kill-server` ends it with status 0, and Restart=always brings it back (on-failure would leave it down). When
    # another server holds the port, adb aborts every time: starting that again would loop for ever, a core dump each time.
    assert phone_env.run("install-units")[0] == 0
    service = unit_section(server_unit_text(phone_env), "Service")
    assert "Restart=always" in service and "RestartSec=2" in service
    assert "RestartPreventExitStatus=SIGABRT" in service


def test_the_adb_servers_unit_is_wanted_at_login_so_the_server_runs_whatever_the_tiers_are(phone_env):
    assert phone_env.run("install-units")[0] == 0
    assert unit_section(server_unit_text(phone_env), "Install") == ["WantedBy=default.target"]


def test_the_adb_servers_unit_carries_the_default_adb_path_when_the_config_names_none(phone_env):
    config_file = Path(phone_env.env["ORION_PHONE_CONFIG"])
    config_file.write_text(re.sub(r"^adb = .*\n", "", config_file.read_text(), flags=re.MULTILINE))
    assert phone_env.run("install-units")[0] == 0
    adb = phone_env.root / "sdk" / "platform-tools" / "adb"
    assert f"ExecStart={adb} -L tcp:localhost:5037 nodaemon server" in server_unit_text(phone_env)


def test_install_units_reports_the_adb_servers_unit_among_those_it_wrote(phone_env):
    code, out = phone_env.run("install-units")
    assert code == 0, out
    units = units_of(phone_env)
    named = (services.ADB_SERVER_UNIT, "orion-phone-a.service", "orion-phone-b.service")
    assert sorted(out["data"]["units"]) == sorted(str(units / name) for name in named), out
    assert out["message"].startswith("wrote 3 units"), out
    assert (out["data"]["stopped"], [path.name for path in sorted(units.glob("*.service"))]) == ([], sorted(named))


def test_install_units_links_the_adb_server_before_its_reload_without_a_reload_of_its_own_and_starts_nothing(phone_env):
    # A link made without a reload leaves every unit marked as needing one until the next reload, and a reload after the last
    # look would load, unseen, what a drop-in changed (it can uncap a session): so the link comes first, without a reload of its
    # own, and the one explicit reload, and the look after it, come after the link
    assert phone_env.run("install-units")[0] == 0
    calls = phone_env.calls("systemctl")
    enable = ["--user", "enable", "--no-reload", services.ADB_SERVER_UNIT]  # the link, and no reload of its own
    assert calls.count(enable) == 1 and calls.index(enable) < calls.index(["--user", "daemon-reload"]), calls
    assert not [call for call in calls if "--now" in call or call[1] in ("start", "restart")]  # nothing is started
    assert [call for call in calls if services.ADB_SERVER_UNIT in call] == [enable]  # and nothing else is asked of it


def test_install_units_asks_systemctl_in_this_order(phone_env):
    assert phone_env.run("install-units")[0] == 0
    assert phone_env.calls("systemctl") == [
        STATE_AND_CAP_B,  # the look before the writes
        ["--user", "enable", "--no-reload", services.ADB_SERVER_UNIT],  # the link, before the reload that loads the units
        DAEMON_RELOAD,
        STATE_CAP_EXTRA_RELOAD_B,  # the look after the reload, which reads that no reload is owed
    ]


def test_a_locked_lease_right_after_an_install_that_made_the_servers_link_is_not_refused_for_a_reload_owed(phone_env):
    # the upgrade: Rollout's first step runs install-units, which makes the adb server's link
    assert phone_env.run("install-units")[0] == 0
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "starting"), out  # not USAGE


def test_a_failed_enable_of_the_adb_server_says_the_units_are_written_and_how_to_go_on(phone_env):
    phone_env.set_fake("systemctl_fail", "enable")
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    message = out["message"]
    assert "enable" in message and services.ADB_SERVER_UNIT in message, out  # systemctl's own words are kept
    assert "the units are written, but not loaded" in message and "run install-units again" in message, out
    assert out["data"]["unit"] == services.ADB_SERVER_UNIT
    assert DAEMON_RELOAD not in phone_env.calls("systemctl")  # the link comes first: nothing was reloaded
    written = sorted(path.name for path in units_of(phone_env).glob("*.service"))
    assert written == sorted([services.ADB_SERVER_UNIT, UNIT_A, UNIT_B])


def test_an_enable_that_times_out_is_reported_the_same_way(phone_env):
    phone_env.set_fake("systemctl_hang", "enable")
    phone_env.env["ORION_PHONE_SYSTEMCTL_TIMEOUT"] = "0.5"
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "timed out" in out["message"] and "the units are written, but not loaded" in out["message"], out
    assert "run install-units again" in out["message"], out


def test_a_failed_enable_keeps_the_list_of_the_phones_the_install_stopped(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)  # b runs from a unit without its cap: the install stops it first
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("systemctl_fail", "enable")
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert out["data"]["stopped"] == ["b"], out  # the sessions this install ended are named, in the data
    assert "not confirmed to enforce their session cap and core limits: b" in out["message"], out  # and in the words


@pytest.mark.parametrize("phone, installable", [("a", True), ("b", False)], ids=["open", "locked"])
def test_every_phones_unit_wants_the_adb_server_and_starts_after_it(phone_env, phone, installable):
    assert phone_env.run("install-units")[0] == 0
    unit = unit_text(phone_env, phone)
    head = unit_section(unit, "Unit")
    assert f"Wants={services.ADB_SERVER_UNIT}" in head and f"After={services.ADB_SERVER_UNIT}" in head
    assert not [line for line in unit_section(unit, "Service") if line.startswith(("Wants", "After"))]  # [Unit] only
    assert ("[Install]" in unit) is installable  # a locked phone's unit still has none: nothing may start it on its own


def test_the_adb_server_has_a_unit_name_no_phone_can_have():
    # a phone's unit is orion-phone-<name>.service and a name is 1 to 16 lowercase letters or digits: no hyphen, so the
    # server's name (a hyphen after the prefix) can never be a phone's
    named = re.fullmatch(r"orion-phone-(.+)\.service", services.ADB_SERVER_UNIT)
    assert named and not config.NAME_RE.fullmatch(named.group(1))


def test_a_phone_named_adb_and_the_adb_server_get_distinct_unit_files(phone_env):
    config_file = Path(phone_env.env["ORION_PHONE_CONFIG"])
    config_file.write_text(config_file.read_text() + '\n[phones.adb]\ntier = "open"\nconsole_port = 5560\n')
    assert phone_env.run("install-units")[0] == 0
    phone_unit = units_of(phone_env) / "orion-phone-adb.service"
    server_unit = units_of(phone_env) / services.ADB_SERVER_UNIT
    assert phone_unit.is_file() and server_unit.is_file()
    assert "_run adb" in phone_unit.read_text() and "nodaemon" not in phone_unit.read_text()
    assert "nodaemon server" in server_unit.read_text() and "_run" not in server_unit.read_text()


BAD_ADB_PATHS = {  # what a unit cannot carry in ExecStart; a newline never gets this far (the config refuses it)
    name: path.replace("python", "adb") for name, path in BAD_INTERPRETERS.items() if name != "newline"
}


@pytest.mark.parametrize("path", BAD_ADB_PATHS.values(), ids=BAD_ADB_PATHS)
def test_an_adb_path_systemd_would_misread_is_refused_naming_android_adb_and_nothing_is_written(phone_env, path):
    set_adb(phone_env, path)
    stale = link_as_enable_does(phone_env, "b")  # a locked phone's login link goes first, even from a refused install
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "android.adb" in out["message"], out
    assert not stale.is_symlink(), "a refused install left a locked phone's login link"
    assert not list(units_of(phone_env).glob("*.service"))  # not the server's unit, and not a phone's: nothing half installed
    assert phone_env.calls("systemctl") == []  # and no reload


def test_an_adb_path_that_is_not_utf8_is_usage_naming_android_adb_and_writes_nothing(phone_env, in_process):
    # the byte 0xff in a path that starts with ~ (expanded from HOME) reaches the config as a lone surrogate
    cfg = dataclasses.replace(config.load(), adb=Path("/opt/" + chr(0xDCFF) + "adb"))
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(cfg)
    assert caught.value.code is Code.USAGE
    assert "UTF-8" in caught.value.message and "android.adb" in caught.value.message
    assert not list(units_of(phone_env).glob("*.service")) and phone_env.calls("systemctl") == []


def test_a_temp_file_left_beside_the_adb_servers_unit_is_removed_by_the_next_install(phone_env):
    units = units_of(phone_env)
    units.mkdir(parents=True)
    leftover = units / f"{services.ADB_SERVER_UNIT}.tmp"  # what a crash between the write and the rename leaves
    leftover.write_text("[Unit]\nDescription=half of a unit")
    assert phone_env.run("install-units")[0] == 0  # the server's unit goes through the same whole-or-nothing write
    assert not leftover.exists()
    assert unit_section(server_unit_text(phone_env), "Service")[1].endswith(" nodaemon server")


def test_run_refuses_outside_a_systemd_unit(phone_env):
    snapshots = Path(phone_env.env["ANDROID_AVD_HOME"]) / "orion-phone-b.avd" / "snapshots" / "default_boot"
    snapshots.mkdir(parents=True)
    code, out = phone_env.run("_run", "b")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert snapshots.exists()  # a refused launch changes nothing


def test_run_of_a_locked_phone_outside_a_unit_points_to_the_lease_and_not_to_starting_the_unit_by_hand(phone_env):
    code, out = phone_env.run("_run", "b")  # a locked unit started by hand has no lease: it would shut itself down at once
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "orion-phone lease b" in out["message"] and "no lease" in out["message"], out
    assert "systemctl --user start" not in out["message"], out


def test_run_of_an_open_phone_outside_a_unit_still_points_to_its_unit(phone_env):
    code, out = phone_env.run("_run", "a")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "systemctl --user start orion-phone-a.service" in out["message"], out
    assert "orion-phone lease" not in out["message"], out


def run_as_a_unit(env, phone: str, *, base: dict[str, str] | None = None, **more_env: str) -> subprocess.CompletedProcess:
    """`orion-phone _run PHONE` as systemd runs it (it sets INVOCATION_ID), on a stand-in emulator that logs the
    arguments it was started with to emulator.log beside the test's other logs, and prints them. It also logs the
    ANDROID_AVD_HOME it was started with (or "unset") to emulator.avd. The process starts from `base` (the test's own
    environment when not given), plus more_env."""
    emulator = env.root / "sdk" / "emulator" / "emulator"
    emulator.parent.mkdir(exist_ok=True)
    emulator.write_text(
        "#!/bin/sh" + NL
        + f'echo "$@" >> "{env.root}/emulator.log"' + NL
        + f'echo "${{ANDROID_AVD_HOME-unset}}" >> "{env.root}/emulator.avd"' + NL
        + 'echo "emulator $@"' + NL
    )
    emulator.chmod(0o755)
    unit_env = {**(env.env if base is None else base), "INVOCATION_ID": "0123456789abcdef0123456789abcdef", **more_env}
    return subprocess.run(
        [sys.executable, "-m", "orion_phone", "--json", "_run", phone], capture_output=True, text=True, env=unit_env, timeout=60
    )


def launches(env) -> list[str]:
    log = env.root / "emulator.log"
    return log.read_text().splitlines() if log.exists() else []


def avd_homes_seen(env) -> list[str]:
    """The ANDROID_AVD_HOME each stand-in emulator was started with, in order ("unset" when it had none)."""
    log = env.root / "emulator.avd"
    return log.read_text().splitlines() if log.exists() else []


def test_run_inside_a_systemd_unit_starts_the_emulator(phone_env):
    proc = run_as_a_unit(phone_env, "a")
    assert proc.returncode == 0, proc.stderr
    assert "emulator -avd orion-phone-a -port 5554" in proc.stdout


# A locked phone is unlocked by its owner on its screen, so a locked phone that started without a window would be a
# lock screen nobody can see. An open phone has nothing to unlock and may run headless.


def test_a_locked_phone_is_never_started_without_a_display(phone_env):
    proc = run_as_a_unit(phone_env, "b")  # neither DISPLAY nor WAYLAND_DISPLAY: a unit installed from a plain shell
    out = json.loads(proc.stdout)
    assert (proc.returncode, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    assert "locked" in message and "unlocks it on its screen" in message and "DISPLAY" in message
    assert "orion-phone install-units" in message and "lease it again" in message
    assert launches(phone_env) == []  # no emulator was started


def test_a_locked_phones_command_line_is_refused_without_a_display_too(phone_env):
    code, out = phone_env.run("_run", "b", "--print")  # what it prints must be what a launch would do
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "needs an X11 display" in out["message"]


def test_a_locked_phone_with_only_a_wayland_display_is_refused_too(phone_env):
    proc = run_as_a_unit(phone_env, "b", WAYLAND_DISPLAY="wayland-0")  # the emulator draws through X11
    assert (proc.returncode, json.loads(proc.stdout)["code"]) == (2, "USAGE")
    assert launches(phone_env) == []


def test_a_locked_phone_with_a_display_starts_with_a_window(phone_env):
    proc = run_as_a_unit(phone_env, "b", DISPLAY=":0")
    assert proc.returncode == 0, proc.stderr
    (arguments,) = launches(phone_env)
    assert "-avd orion-phone-b -port 5556" in arguments and "-no-snapshot" in arguments
    assert "-no-window" not in arguments


def test_an_open_phone_still_starts_headless_without_a_display(phone_env):
    proc = run_as_a_unit(phone_env, "a")
    assert proc.returncode == 0, proc.stderr
    (arguments,) = launches(phone_env)
    assert "-avd orion-phone-a" in arguments and "-no-window" in arguments


EXPLANATIONS = {
    "systemctl missing": (FileNotFoundError(2, "No such file or directory"), Code.USAGE),
    "systemctl not executable": (PermissionError(13, "Permission denied"), Code.USAGE),
    "systemctl hung": (subprocess.TimeoutExpired(["systemctl"], 120), Code.PHONE_DOWN),
}


@pytest.mark.parametrize("error, code", EXPLANATIONS.values(), ids=EXPLANATIONS)
def test_every_way_systemctl_can_fail_to_run_is_a_phone_error_naming_the_unit(monkeypatch, error, code):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PhoneError) as caught:
        services.systemctl("stop", "orion-phone-b.service")
    assert caught.value.code is code
    assert caught.value.data == {"unit": "orion-phone-b.service"}


def test_systemctl_waits_longer_than_systemds_own_stop_timeout(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs)
        return completed(command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("ORION_PHONE_SYSTEMCTL_TIMEOUT", raising=False)
    services.systemctl("stop", "orion-phone-b.service")
    assert seen["timeout"] == 120  # systemd gives a unit 90 s to stop before it kills it


@pytest.mark.parametrize("value, expected", [("2.5", 2.5), ("30", 30.0), ("0.01", 0.01)])
def test_a_time_limit_can_be_set_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("ORION_PHONE_SOME_LIMIT", value)
    assert services.limit("ORION_PHONE_SOME_LIMIT", 7.0) == expected


@pytest.mark.parametrize("value", ["", "0", "-1", "nan", "inf", "-inf", "1e999", "soon"])
def test_a_bad_time_limit_never_turns_the_limit_off(monkeypatch, value):
    monkeypatch.setenv("ORION_PHONE_SOME_LIMIT", value)
    assert services.limit("ORION_PHONE_SOME_LIMIT", 7.0) == 7.0


def test_a_time_limit_is_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("ORION_PHONE_SOME_LIMIT", raising=False)
    assert services.limit("ORION_PHONE_SOME_LIMIT", 7.0) == 7.0


# Which run of the unit is this: the InvocationID names a boot, and only a running unit has one that counts.

SHOW_OUTPUT = {
    "active with an id": ("ActiveState=active" + NL + "InvocationID=abc123" + NL, "abc123"),
    "the same two lines the other way round": ("InvocationID=abc123" + NL + "ActiveState=active" + NL, "abc123"),
    "active with no id": ("ActiveState=active" + NL + "InvocationID=" + NL, ""),
    "inactive, with its last id kept": ("ActiveState=inactive" + NL + "InvocationID=abc123" + NL, ""),
    "failed, with its last id kept": ("ActiveState=failed" + NL + "InvocationID=abc123" + NL, ""),
    "still activating": ("ActiveState=activating" + NL + "InvocationID=abc123" + NL, ""),
    "already deactivating": ("ActiveState=deactivating" + NL + "InvocationID=abc123" + NL, ""),
    "no state line": ("InvocationID=abc123" + NL, ""),
    "nothing at all": ("", ""),
}


@pytest.mark.parametrize("output, expected", SHOW_OUTPUT.values(), ids=SHOW_OUTPUT)
def test_invocation_id_is_the_id_of_a_running_unit_only(monkeypatch, output, expected):
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        return completed(command, 0, output)

    monkeypatch.setattr(subprocess, "run", run)
    assert services.invocation_id("orion-phone-b.service") == expected
    assert [c[:3] for c in asked] == [["systemctl", "--user", "show"]] and asked[0][-1] == "orion-phone-b.service"


# What reaches a unit file: DISPLAY comes from the shell that runs install-units, so it is checked like the paths are.

HOSTILE_DISPLAYS = {
    "a newline starts a new directive": ":0" + NL + "ExecStartPre=/bin/true",
    "a carriage return": ":0" + chr(13) + "ExecStartPre=/bin/true",
    "a double quote ends the value": ':0" "LD_PRELOAD=/tmp/x.so',
    "an escape character": ":0" + chr(27) + "[2J",
    "a delete character": ":0" + chr(127),
    "a line separator": ":0" + chr(0x2028) + "RuntimeMaxUSec=infinity",  # a unit file does not break there, show output does
    "a paragraph separator": ":0" + chr(0x2029) + "RuntimeMaxUSec=infinity",
    "a next line character": ":0" + chr(0x85) + "RuntimeMaxUSec=infinity",
}


@pytest.mark.parametrize("display", HOSTILE_DISPLAYS.values(), ids=HOSTILE_DISPLAYS)
def test_a_display_that_could_forge_unit_lines_is_refused(phone_env, display):
    phone_env.env["DISPLAY"] = display
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "DISPLAY" in out["message"]
    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    assert not units.exists() or not list(units.iterdir())  # nothing half installed
    assert phone_env.calls("systemctl") == []


@pytest.mark.parametrize(
    "display, written",
    [(":0", ":0"), ("localhost:10.0", "localhost:10.0"), ("host name:0", "host name:0"), ("a%h:0", "a%%h:0")],
)
def test_an_ordinary_display_is_carried_into_the_units(phone_env, display, written):
    phone_env.env["DISPLAY"] = display
    assert phone_env.run("install-units")[0] == 0
    assert f'Environment="DISPLAY={written}"' in unit_text(phone_env, "a")


# A time limit can be raised to a day and no further: no value may overflow a subprocess timeout.


@pytest.mark.parametrize("value", ["86400", "86401", "1e30", "1e300"])
def test_a_time_limit_is_held_to_a_day(monkeypatch, value):
    monkeypatch.setenv("ORION_PHONE_SOME_LIMIT", value)
    assert services.limit("ORION_PHONE_SOME_LIMIT", 7.0) == 86400.0


def test_an_enormous_systemctl_timeout_reaches_subprocess_as_a_day(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs)
        return completed(command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("ORION_PHONE_SYSTEMCTL_TIMEOUT", "1e30")
    services.systemctl("stop", "orion-phone-b.service")
    assert seen["timeout"] == 86400


# The emulator's console and adb ports are looked at on loopback; the environment may move the look to another loopback
# address, which is how tests keep away from a real phone's ports.


@pytest.mark.parametrize("value", ["127.0.0.2", "127.1.2.3", "::1"])
def test_the_probe_host_can_be_another_loopback_address(monkeypatch, value):
    monkeypatch.setenv("ORION_PHONE_PROBE_HOST", value)
    assert services.probe_host() == value


@pytest.mark.parametrize("value", ["", "localhost", "10.0.0.5", "8.8.8.8", "example.com", "127.0.0.1 ", "not an address"])
def test_the_probe_host_stays_on_loopback_whatever_the_environment_says(monkeypatch, value):
    monkeypatch.setenv("ORION_PHONE_PROBE_HOST", value)
    assert services.probe_host() == "127.0.0.1"


def test_the_probe_host_is_loopback_by_default(monkeypatch):
    monkeypatch.delenv("ORION_PHONE_PROBE_HOST", raising=False)
    assert services.probe_host() == "127.0.0.1"


@pytest.mark.parametrize("ports, expected", [((), None), ((5556,), 5556), ((5557,), 5557), ((5556, 5557), 5556)])
def test_answering_port_names_the_first_of_the_two_ports_that_answers(listening, tmp_path, ports, expected):
    cfg = config.parse({"phones": {"b": {"tier": "locked", "console_port": 5556}}}, tmp_path / "c.toml", tmp_path / "s")
    if ports:
        listening(*ports)
    assert services.answering_port(cfg.phone("b")) == expected


@pytest.mark.parametrize("ports, expected", [((), False), ((5556,), True), ((5557,), False), ((5556, 5557), True)])
def test_the_console_answers_only_when_its_own_port_does(listening, tmp_path, ports, expected):
    # `adb emu kill` goes to the console port, so that is the port to look at: answering_port would say 5557 for the adb port
    cfg = config.parse({"phones": {"b": {"tier": "locked", "console_port": 5556}}}, tmp_path / "c.toml", tmp_path / "s")
    if ports:
        listening(*ports)
    assert services.console_answers(cfg.phone("b")) is expected


def test_the_console_is_looked_for_at_the_probe_address_and_at_127_0_0_1_otherwise(monkeypatch):
    monkeypatch.delenv("ORION_PHONE_PROBE_HOST", raising=False)
    with socket.socket() as sock:  # a port of this test's own: never one a real phone could be using
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert services.console_answers(PhoneConfig("x", "locked", port, 120)) is True


def test_answering_port_looks_at_127_0_0_1_unless_told_otherwise(monkeypatch):
    monkeypatch.delenv("ORION_PHONE_PROBE_HOST", raising=False)
    with socket.socket() as sock:  # a port of this test's own: never one a real phone could be using
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert services.answering_port(PhoneConfig("x", "locked", port, 120)) == port


def login_links(env) -> Path:
    return Path(env.env["ORION_PHONE_UNIT_DIR"]) / "default.target.wants"


def link_as_enable_does(env, phone: str, target: str | None = None) -> Path:
    """What `systemctl --user enable` leaves for an open unit: a link in default.target.wants (the fake leaves none)."""
    link = login_links(env) / f"orion-phone-{phone}.service"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(Path(env.env["ORION_PHONE_UNIT_DIR"]) / (target or link.name))
    return link


@pytest.mark.parametrize("target", [None, "gone.service"], ids=["unit file present", "unit file gone"])
def test_a_phone_that_became_locked_loses_its_login_link_and_nothing_else_does(phone_env, target):
    stale = link_as_enable_does(phone_env, "b", target)  # b was open when it was enabled; the config now says locked
    kept = link_as_enable_does(phone_env, "a")  # a is still open
    other = login_links(phone_env) / "unrelated.service"
    other.symlink_to(Path(phone_env.env["ORION_PHONE_UNIT_DIR"]) / "unrelated.service")
    assert phone_env.run("install-units")[0] == 0
    assert not stale.is_symlink()  # systemd would still follow it, [Install] section or not
    assert kept.is_symlink() and other.is_symlink()


def test_a_login_link_that_cannot_be_removed_stops_install_units_before_the_reload(phone_env, in_process, monkeypatch):
    stale = link_as_enable_does(phone_env, "b")

    def refuse(self, missing_ok=False):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and str(stale) in caught.value.message
    assert caught.value.data == {"unit": "orion-phone-b.service"}
    assert phone_env.calls("systemctl") == []  # nothing was reloaded over a link that may still start the phone


# The order of install-units: a locked phone's login link goes first, and durably; then each unit is written whole or
# not at all; only then is systemd told. A partial install must never leave a link that boots a locked phone at login,
# nor a unit that is only the first part of its text (a locked unit cut before Restart=no and RuntimeMaxSec=).


def units_of(env) -> Path:
    return Path(env.env["ORION_PHONE_UNIT_DIR"])


def test_a_failed_unit_write_leaves_no_login_link_for_a_locked_phone(phone_env):
    stale = link_as_enable_does(phone_env, "b")  # b was open when it was enabled; the config now says locked
    units = units_of(phone_env)
    (units / "orion-phone-b.service").mkdir()  # nothing can be renamed over a directory: the install fails at b
    code, out = phone_env.run("install-units")
    assert not stale.is_symlink(), "b's login link survived a failed install: it would boot at the next login"
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "orion-phone-b.service" in out["message"]
    assert ["--user", "daemon-reload"] not in phone_env.calls("systemctl")  # and nothing was reloaded
    assert (units / "orion-phone-a.service").read_text().startswith("[Unit]")  # the unit written before it is whole


def test_a_unit_is_never_left_half_written(phone_env, in_process, monkeypatch, short_write):
    units = units_of(phone_env)
    assert phone_env.run("install-units")[0] == 0
    before = {path.name: path.read_bytes() for path in units.glob("*.service")}
    monkeypatch.setenv("DISPLAY", ":0")  # every unit would now carry one more line, so rewriting a unit is a longer write

    def install():  # the forked child counts an error it reports as a PhoneError as a refusal too
        try:
            phone_module.install_units(config.load())
        except PhoneError as exc:
            raise OSError(exc.message) from None

    assert short_write(len(before["orion-phone-a.service"]) + 10, install) == "raised"  # the disk gives out mid-write
    assert {path.name: path.read_bytes() for path in units.glob("*.service")} == before  # every unit is still whole
    assert sorted(path.name for path in units.iterdir()) == sorted(before)  # and no half-written temp file is left


@pytest.mark.parametrize("mode", [0o644, 0o444], ids=["an ordinary leftover", "a read-only leftover"])
def test_a_temp_file_left_by_a_crash_is_never_a_unit_and_is_removed_by_the_next_install(phone_env, mode):
    units = units_of(phone_env)
    units.mkdir(parents=True)
    leftover = units / "orion-phone-a.service.tmp"  # what a crash between the write and the rename leaves
    leftover.write_text("[Unit]\nDescription=half of a unit")
    leftover.chmod(mode)  # a read-only one cannot be opened for writing again, but it can be removed
    assert phone_env.run("install-units")[0] == 0
    assert not leftover.exists()  # the next install removed it first, and made its own temp file
    assert (units / "orion-phone-a.service").read_text().startswith("[Unit]\nDescription=orion-phone a")


def sync_spy(monkeypatch, events: list) -> None:
    """Record, in order, every unlink, file sync, directory sync (told apart by os.fstat of the descriptor), and rename."""
    real_fsync, real_replace, real_unlink = os.fsync, os.replace, os.unlink

    def fsync(fd):
        info = os.fstat(fd)
        events.append(("fsync directory" if stat.S_ISDIR(info.st_mode) else "fsync file", info.st_ino))
        real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", Path(src).name, Path(dst).name))
        real_replace(src, dst)

    def unlink(path, *args, **kwargs):
        events.append(("unlink", Path(path).name))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)


def test_login_links_go_first_and_are_synced_the_units_are_synced_and_the_link_and_the_reload_come_last(
    phone_env, in_process, monkeypatch
):
    link_as_enable_does(phone_env, "b")
    events: list[tuple] = []
    sync_spy(monkeypatch, events)
    monkeypatch.setattr(services, "systemctl", lambda *args: events.append(("systemctl", *args)))
    phone_module.install_units(config.load())
    assert [event[0] for event in events] == [
        "unlink", "fsync directory",  # b's login link, then the directory that held it
        "unlink", "fsync file", "replace", "unlink", "fsync file", "replace", "unlink", "fsync file", "replace",
        # each unit (the adb server's, then a's and b's): its temp name cleared, written in full and synced, renamed into
        "fsync directory", "systemctl", "systemctl",  # place; the unit directory; only then the link, and the one reload
    ]
    assert events[0] == ("unlink", "orion-phone-b.service")
    assert events[1][1] == login_links(phone_env).stat().st_ino
    assert [event[1] for event in events if event[0] == "unlink"] == [
        "orion-phone-b.service", f"{services.ADB_SERVER_UNIT}.tmp", "orion-phone-a.service.tmp", "orion-phone-b.service.tmp",
    ]
    assert [event[1:] for event in events if event[0] == "replace"] == [  # a temp file whose name is not a unit's
        (f"{services.ADB_SERVER_UNIT}.tmp", services.ADB_SERVER_UNIT),  # the one the phones' units name goes first
        ("orion-phone-a.service.tmp", "orion-phone-a.service"),
        ("orion-phone-b.service.tmp", "orion-phone-b.service"),
    ]
    assert events[-3][1] == units_of(phone_env).stat().st_ino
    assert events[-2] == ("systemctl", "enable", "--no-reload", services.ADB_SERVER_UNIT)  # the link, before the reload
    assert events[-1] == ("systemctl", "daemon-reload")  # which is the last thing that changes anything


def test_install_units_with_no_login_links_at_all_still_syncs_what_it_wrote(phone_env, in_process, monkeypatch):
    events: list[tuple] = []
    sync_spy(monkeypatch, events)
    monkeypatch.setattr(services, "systemctl", lambda *args: None)
    phone_module.install_units(config.load())  # no default.target.wants exists: nothing to remove, nothing to sync there
    assert [event[0] for event in events] == [
        "unlink", "fsync file", "replace", "unlink", "fsync file", "replace", "unlink", "fsync file", "replace",
        "fsync directory",  # three units: the adb server's, a's and b's
    ]


@pytest.mark.parametrize("failing", ["login", "units"], ids=["the login-link directory", "the unit directory"])
def test_a_directory_that_cannot_be_synced_is_usage_naming_it_and_nothing_is_reloaded(phone_env, in_process, monkeypatch, failing):
    link_as_enable_does(phone_env, "b")
    directory = login_links(phone_env) if failing == "login" else units_of(phone_env)
    real_fsync = os.fsync

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and info.st_ino == directory.stat().st_ino:
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and str(directory) in caught.value.message
    calls = phone_env.calls("systemctl")
    assert ["--user", "daemon-reload"] not in calls  # nothing was reloaded
    if failing == "login":
        assert calls == []  # it failed before anything was read


@pytest.mark.parametrize("umask, mode", [(0o077, 0o600), (0o002, 0o664)])
def test_a_new_unit_gets_the_mode_any_new_file_gets(phone_env, umask, mode):
    previous = os.umask(umask)
    try:
        assert phone_env.run("install-units")[0] == 0
    finally:
        os.umask(previous)
    modes = {stat.S_IMODE(path.stat().st_mode) for path in units_of(phone_env).glob("*.service")}
    assert modes == {mode}  # 0o666 less the umask, as write_text gave: neither broader nor narrower


def test_a_rewritten_unit_keeps_its_own_mode(phone_env):
    unit = units_of(phone_env) / "orion-phone-a.service"
    assert phone_env.run("install-units")[0] == 0
    unit.chmod(0o640)
    assert phone_env.run("install-units")[0] == 0  # under the usual umask a fresh file would be 0o644: broader
    assert stat.S_IMODE(unit.stat().st_mode) == 0o640


# Nothing that stands at a unit's temp name is ever reused, truncated, or followed: it is removed first, and the temp
# file is made fresh. A link there would otherwise carry the write to another file (or to the live unit itself, cut
# in place), and a pipe would make the open wait for a reader for ever.


def put_the_locked_phone_first(env) -> None:
    """Make b the first unit install-units writes: the config lists phones in the order they are written."""
    path = Path(env.env["ORION_PHONE_CONFIG"])
    head, phones = path.read_text().split("[phones.a]")
    a_block, b_block = ("[phones.a]" + phones).split("[phones.b]")
    path.write_text(head + "[phones.b]" + b_block + "\n" + a_block)


def test_a_temp_name_that_links_to_an_outside_file_never_writes_through_to_it(phone_env):
    units = units_of(phone_env)
    assert phone_env.run("install-units")[0] == 0
    victim = phone_env.root / "victim.txt"
    victim.write_text("precious\n")
    victim.chmod(0o600)
    (units / "orion-phone-b.service").chmod(0o644)  # a mode the victim lacks: a unit made from it would show it
    (units / "orion-phone-b.service.tmp").symlink_to(victim)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (0, "OK"), out
    assert victim.read_text() == "precious\n" and stat.S_IMODE(victim.stat().st_mode) == 0o600
    unit = units / "orion-phone-b.service"
    assert not unit.is_symlink() and unit.read_text().startswith("[Unit]\nDescription=orion-phone b")


def test_a_temp_name_that_links_to_the_live_unit_never_lets_a_failed_write_cut_it(phone_env, in_process, short_write):
    put_the_locked_phone_first(phone_env)  # so that the write that fails is the locked unit's
    units = units_of(phone_env)
    assert phone_env.run("install-units")[0] == 0
    unit = units / "orion-phone-b.service"
    before = unit.read_bytes()
    (units / "orion-phone-b.service.tmp").symlink_to(unit.name)  # a temp name that leads to the live unit

    def install():
        try:
            phone_module.install_units(config.load())
        except PhoneError as exc:
            raise OSError(exc.message) from None

    # the disk gives out just before Restart=no: a write that went through the link would leave the unit cut there
    assert short_write(before.index(b"Restart=no"), install) == "raised"
    assert unit.read_bytes() == before and not unit.is_symlink()


def test_a_pipe_at_the_temp_name_does_not_hang_install_units(phone_env):
    units = units_of(phone_env)
    assert phone_env.run("install-units")[0] == 0
    os.mkfifo(units / "orion-phone-a.service.tmp")  # opening it for writing waits for a reader for ever
    code, out = phone_env.run("install-units", timeout=15)
    assert (code, out["code"]) == (0, "OK"), out
    assert not (units / "orion-phone-a.service.tmp").exists() and (units / "orion-phone-a.service").is_file()


def test_a_directory_at_the_temp_name_is_usage_naming_it_and_leaves_the_unit_alone(phone_env):
    units = units_of(phone_env)
    assert phone_env.run("install-units")[0] == 0
    unit = units / "orion-phone-a.service"
    before = unit.read_bytes()
    temp = units / "orion-phone-a.service.tmp"
    temp.mkdir()
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(temp) in out["message"]  # the file at fault, not only the unit it was for
    assert unit.read_bytes() == before and not unit.is_symlink()


# Units are rendered and encoded before any unit is touched: text that is not UTF-8 (a surrogate, which is what a
# non-UTF-8 byte in DISPLAY or in a path becomes) is refused up front, with no temp file made and no unit changed. A
# locked phone's login link is another matter: removing it is always right, so even a refused install removes it.


@pytest.mark.parametrize("variable", ["DISPLAY", "ORION_PHONE_STATE", "ANDROID_AVD_HOME"])
def test_text_that_is_not_utf8_is_usage_and_changes_no_unit_but_still_disarms_the_login_link(phone_env, variable):
    units = units_of(phone_env)
    assert phone_env.run("install-units")[0] == 0
    before = {path.name: path.read_bytes() for path in units.glob("*.service")}
    stale = link_as_enable_does(phone_env, "b")  # b is locked: this link would boot it at the next login
    phone_env.env[variable] = phone_env.env.get(variable, "") + "\udcff"  # the byte 0xff, as os.environ holds it
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "UTF-8" in out["message"] and "phone a" in out["message"]
    assert variable in out["message"]  # the message names the variable to fix among its candidates
    assert not stale.is_symlink(), "a refused install left a locked phone's login link"
    assert {path.name: path.read_bytes() for path in units.glob("*.service")} == before
    assert sorted(path.name for path in units.iterdir()) == sorted([*before, "default.target.wants"])  # no temp file


def test_an_interpreter_path_systemd_would_misread_is_refused_but_the_login_link_still_goes(phone_env, in_process, monkeypatch):
    stale = link_as_enable_does(phone_env, "b")
    monkeypatch.setattr(sys, "executable", "/opt/my python/bin/python")  # refused when the units are rendered
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and "interpreter" in caught.value.message
    assert not stale.is_symlink(), "a refused install left a locked phone's login link"
    assert not list(units_of(phone_env).glob("*.service"))  # and nothing else was written
    assert phone_env.calls("systemctl") == []


def test_a_unit_directory_that_cannot_be_created_is_usage_naming_it(phone_env, in_process, monkeypatch):
    def refuse(self, mode=0o777, parents=False, exist_ok=False):
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(Path, "mkdir", refuse)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and str(units_of(phone_env)) in caught.value.message
    assert phone_env.calls("systemctl") == []


# The temp file is created, never opened: a name that is taken when the open comes (the removal before it can be raced)
# is refused. O_NOFOLLOW would stop a link on its own, but not a second name for a file (a hard link), so both are
# planted: a link to an outside file, and a hard link to it.


@pytest.mark.parametrize("plant", [os.symlink, os.link], ids=["a symlink", "a hard link"])
def test_a_name_taken_at_the_temp_name_after_it_was_cleared_is_refused_not_written_through(
    phone_env, in_process, monkeypatch, plant
):
    units = units_of(phone_env)
    assert phone_env.run("install-units")[0] == 0
    victim = phone_env.root / "victim.txt"
    victim.write_text("precious\n")
    unit = units / "orion-phone-a.service"
    before = unit.read_bytes()
    temp = units / "orion-phone-a.service.tmp"
    real_open = os.open

    def racing_open(path, flags, mode=0o777, *args, **kwargs):
        if Path(path) == temp and not os.path.lexists(path):  # another actor takes the name in the window
            plant(victim, path)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(services, "systemctl", lambda *args: None)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and str(temp) in caught.value.message
    assert victim.read_text() == "precious\n"  # never written through
    assert unit.read_bytes() == before and not os.path.lexists(temp)


# A lookup that fails is as much a refusal as a removal that fails: whether a link is there is not known.


def test_a_login_link_that_cannot_be_looked_up_is_usage_naming_it(phone_env, in_process, monkeypatch):
    stale = link_as_enable_does(phone_env, "b")
    real_is_symlink = Path.is_symlink

    def is_symlink(self):  # what lstat in a directory that cannot be searched does on Python 3.11
        if self == stale:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and str(stale) in caught.value.message
    assert caught.value.data == {"unit": "orion-phone-b.service"}
    assert not list(units_of(phone_env).glob("*.service")) and phone_env.calls("systemctl") == []


# The AVD home decides where the phones' data lives (disk images, snapshots). The Android tools read ANDROID_AVD_HOME as
# it stands, and the real ones ignore one that is not an existing directory, so the tool works out the effective home once
# (ANDROID_AVD_HOME, else ANDROID_USER_HOME/avd, else ~/.android/avd, as the tools do), checks it (absolute where a variable
# gives it, resolved, outside any git working tree, fit to be written into a unit), and hands that value on: setup makes the
# directory and runs avdmanager with it, the units always name it, and a launch refuses a home that is not there.

AVD_COMMANDS = {
    "install-units": ("install-units",),
    "setup": ("setup",),
    "a locked phone's launch": ("_run", "b", "--print"),
    "an open phone's launch": ("_run", "a", "--print"),
}
BAD_AVD_HOMES = {
    "relative": "relative/avd",
    "a bare name": "avd",
    "the working directory": ".",
    "empty": "",
    "a literal tilde": "~/avd",
}


def make_repo(root: Path, form: str) -> Path:
    """root becomes a git working tree in one of the two shapes the check knows: a repository (a .git directory with a
    HEAD) or a linked worktree (.git is a pointer file)."""
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    if form == "repository":
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n")
    else:
        git.write_text("gitdir: /elsewhere/.git/worktrees/some-repo\n")
    return root


def assert_avd_home_refused(env, code: int, out: dict, variable: str = "ANDROID_AVD_HOME") -> None:
    """The command stopped with USAGE naming the variable, before any tool ran or any unit was written."""
    assert (code, out["code"]) == (2, "USAGE"), out
    assert variable in out["message"], out
    assert all(env.calls(tool) == [] for tool in ("sdkmanager", "avdmanager", "systemctl")), "a tool ran"
    units = units_of(env)
    assert not units.exists() or not list(units.iterdir())  # nothing written


def unit_environment(env, phone: str) -> dict[str, str]:
    """What systemd gives the unit's process from its Environment lines: each is one quoted assignment, with % and
    backslash doubled, so reading it back undoes that."""
    found = {}
    for line in unit_text(env, phone).splitlines():
        if line.startswith('Environment="') and line.endswith('"'):
            key, value = line[len('Environment="'):-1].split("=", 1)
            found[key] = value.replace(BS + BS, BS).replace("%%", "%")
    return found


def default_home(env) -> Path:
    return Path(env.env["HOME"]) / ".android" / "avd"


# Every command that uses the home goes through the one check. The values are tried in full on install-units; the others
# each need to be seen refusing, so they get two (the check is the same code, and test_config pins what a repository is).
NOT_ABSOLUTE_CASES = [
    pytest.param(command, value, id=f"{value_id}-{command_id}")
    for command_id, command in AVD_COMMANDS.items()
    for value_id, value in BAD_AVD_HOMES.items()
    if command_id == "install-units" or value_id in ("relative", "empty")
]


@pytest.mark.parametrize("command, value", NOT_ABSOLUTE_CASES)
def test_an_avd_home_that_is_not_absolute_is_refused_before_anything_runs(phone_env, tmp_path, monkeypatch, command, value):
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)  # where a relative home would put the phones' data
    phone_env.env["DISPLAY"] = ":0"  # a locked launch is refused without one, so that must not be what stops this
    phone_env.env["ANDROID_AVD_HOME"] = value
    code, out = phone_env.run(*command)
    assert_avd_home_refused(phone_env, code, out)
    assert "absolute" in out["message"], out
    assert list(here.iterdir()) == []  # no phone data in the working directory


GIT_TREE_CASES = [
    pytest.param(command, "repository", "above", id=f"{command_id}")
    for command_id, command in AVD_COMMANDS.items()
] + [
    pytest.param(AVD_COMMANDS["install-units"], form, where, id=f"{form}-{where}")
    for form in ("repository", "worktree")
    for where in ("at", "above")
    if (form, where) != ("repository", "above")  # that one is the case every command has above
]


@pytest.mark.parametrize("command, form, where", GIT_TREE_CASES)
def test_an_avd_home_inside_a_git_working_tree_is_refused_before_anything_runs(phone_env, tmp_path, command, form, where):
    repo = make_repo(tmp_path / "some-repo", form)
    phone_env.env["DISPLAY"] = ":0"
    phone_env.env["ANDROID_AVD_HOME"] = str(repo if where == "at" else repo / "deeper" / "avd")
    code, out = phone_env.run(*command)
    assert_avd_home_refused(phone_env, code, out)
    assert "is inside a git working tree" in out["message"], out
    assert [p.name for p in repo.iterdir()] == [".git"]  # no phone data in the repository


@pytest.mark.parametrize("command", [AVD_COMMANDS["install-units"], AVD_COMMANDS["setup"]], ids=["install-units", "setup"])
def test_an_avd_home_that_is_a_link_into_a_git_working_tree_is_refused_too(phone_env, tmp_path, command):
    repo = make_repo(tmp_path / "some-repo", "repository")
    (repo / "avd").mkdir()
    link = tmp_path / "avd-link"
    link.symlink_to(repo / "avd")  # the link itself is outside the repository; where it leads is not
    phone_env.env["ANDROID_AVD_HOME"] = str(link)
    code, out = phone_env.run(*command)
    assert_avd_home_refused(phone_env, code, out)
    assert "is inside a git working tree" in out["message"], out
    assert list((repo / "avd").iterdir()) == []


def test_an_avd_home_behind_a_symlink_loop_is_usage(phone_env, tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    try:
        loop.resolve()
    except (OSError, RuntimeError):
        pass
    else:
        pytest.skip("this Python resolves symlink loops without raising")
    phone_env.env["ANDROID_AVD_HOME"] = str(loop)
    code, out = phone_env.run("install-units")
    assert_avd_home_refused(phone_env, code, out)
    assert "cannot resolve" in out["message"], out


def test_a_dot_git_that_cannot_be_inspected_refuses_the_avd_home_as_unchecked(in_process, tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    (sandbox / ".git").mkdir(parents=True)  # no HEAD: not a repository, so the failed check is the only problem
    monkeypatch.setenv("ANDROID_AVD_HOME", str(sandbox / "avd"))
    real_lstat = os.lstat

    def denied(path, *args, **kwargs):
        if Path(path).name == "HEAD":
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(config.os, "lstat", denied)
    with pytest.raises(PhoneError) as caught:
        services.avd_home()
    assert caught.value.code is Code.USAGE and "ANDROID_AVD_HOME" in caught.value.message
    assert "could not be checked" in caught.value.message and "Permission denied" in caught.value.message
    assert "is inside a git working tree" not in caught.value.message  # refusing is right; claiming a repository is not


# The home is written into the units, so what a unit file cannot carry is refused, as DISPLAY is: by the check itself, so
# that setup stops before it downloads anything, and again by render_unit, which writes the line.

HOSTILE_AVD_SUFFIXES = {
    "a newline starts a new directive": NL + "ExecStartPre=/bin/true",
    "a carriage return": chr(13) + "ExecStartPre=/bin/true",
    "a double quote ends the value": '" "LD_PRELOAD=/tmp/x.so',
    "an escape character": chr(27) + "[2J",
    "a delete character": chr(127),
    "a line separator": chr(0x2028) + "RuntimeMaxUSec=infinity",
    "a paragraph separator": chr(0x2029) + "RuntimeMaxUSec=infinity",
    "a next line character": chr(0x85) + "RuntimeMaxUSec=infinity",
}
QUOTED = HOSTILE_AVD_SUFFIXES["a double quote ends the value"]


@pytest.mark.parametrize("suffix", HOSTILE_AVD_SUFFIXES.values(), ids=HOSTILE_AVD_SUFFIXES)
def test_an_avd_home_that_could_forge_unit_lines_is_refused(phone_env, tmp_path, suffix):
    phone_env.env["ANDROID_AVD_HOME"] = str(tmp_path / "avd") + suffix
    code, out = phone_env.run("install-units")
    assert_avd_home_refused(phone_env, code, out)
    assert "control character, a line or paragraph separator, or a double quote" in out["message"], out


@pytest.mark.parametrize("command", [c for k, c in AVD_COMMANDS.items() if k != "install-units"], ids=["setup", "locked launch", "open launch"])
def test_a_quoted_avd_home_is_refused_by_every_command_before_it_does_anything(phone_env, tmp_path, command):
    phone_env.env["DISPLAY"] = ":0"
    phone_env.env["ANDROID_AVD_HOME"] = str(tmp_path / "avd") + QUOTED
    code, out = phone_env.run(*command)
    assert_avd_home_refused(phone_env, code, out)  # for setup: before sdkmanager downloads, before an AVD is made
    assert "control character, a line or paragraph separator, or a double quote" in out["message"], out
    assert not (tmp_path / "avd").exists() and not any(tmp_path.glob("avd*"))


@pytest.mark.parametrize("suffix", HOSTILE_AVD_SUFFIXES.values(), ids=HOSTILE_AVD_SUFFIXES)
def test_render_unit_itself_refuses_a_home_a_unit_file_cannot_carry(in_process, tmp_path, suffix):
    cfg = config.load()
    with pytest.raises(PhoneError) as caught:
        services.render_unit(cfg, cfg.phone("a"), "/opt/py-3.11/bin/python3", None, Path(str(tmp_path / "avd") + suffix))
    assert caught.value.code is Code.USAGE and "ANDROID_AVD_HOME" in caught.value.message


def test_a_refused_avd_home_still_disarms_a_locked_phones_login_link(phone_env):
    stale = link_as_enable_does(phone_env, "b")  # b was open when it was enabled; the config now says locked
    phone_env.env["ANDROID_AVD_HOME"] = "relative/avd"
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert not stale.is_symlink(), "a refused install left a locked phone's login link"


# The effective home is what the Android tools would use: ANDROID_AVD_HOME, else ANDROID_USER_HOME/avd, else
# ~/.android/avd. Each is held to the same rules, and a variable that does not decide is not looked at.


def test_the_avd_home_is_the_default_when_no_variable_is_set(in_process, monkeypatch):
    monkeypatch.delenv("ANDROID_AVD_HOME")
    assert services.avd_home() == Path(os.environ["HOME"]) / ".android" / "avd"


def test_the_avd_home_follows_android_user_home_when_no_avd_home_is_set(in_process, monkeypatch, tmp_path):
    monkeypatch.delenv("ANDROID_AVD_HOME")
    monkeypatch.setenv("ANDROID_USER_HOME", str(tmp_path / "user"))
    assert services.avd_home() == tmp_path / "user" / "avd"


def test_an_avd_home_wins_over_android_user_home(in_process, monkeypatch, tmp_path):
    monkeypatch.setenv("ANDROID_USER_HOME", str(tmp_path / "user"))  # ANDROID_AVD_HOME is the fixture's, and is set
    assert services.avd_home() == Path(os.environ["ANDROID_AVD_HOME"])


def test_a_set_avd_home_is_returned_resolved(in_process, monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path / "link") + "/")
    assert services.avd_home() == real


@pytest.mark.parametrize("value", ["relative/user", "", "~/user"], ids=["relative", "empty", "a literal tilde"])
def test_an_android_user_home_that_is_not_absolute_is_refused_when_it_decides_the_avd_home(phone_env, tmp_path, monkeypatch, value):
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)  # where an empty or relative one would put the phones' data
    phone_env.env.pop("ANDROID_AVD_HOME")
    phone_env.env["ANDROID_USER_HOME"] = value
    code, out = phone_env.run("install-units")
    assert_avd_home_refused(phone_env, code, out, "ANDROID_USER_HOME")
    assert "absolute" in out["message"], out


def test_an_android_user_home_inside_a_git_working_tree_is_refused_when_it_decides_the_avd_home(phone_env, tmp_path):
    repo = make_repo(tmp_path / "some-repo", "repository")
    phone_env.env.pop("ANDROID_AVD_HOME")
    phone_env.env["ANDROID_USER_HOME"] = str(repo / "user")
    code, out = phone_env.run("install-units")
    assert_avd_home_refused(phone_env, code, out, "ANDROID_USER_HOME")
    assert "is inside a git working tree" in out["message"], out


def test_a_default_avd_home_inside_a_git_working_tree_is_refused_and_names_the_way_out(phone_env, tmp_path):
    repo = make_repo(tmp_path / "some-repo", "repository")
    phone_env.env.pop("ANDROID_AVD_HOME")
    phone_env.env["HOME"] = str(repo)  # as when a home directory is itself a repository
    code, out = phone_env.run("install-units")
    assert_avd_home_refused(phone_env, code, out)  # the way out is the variable, so it is named
    assert "is inside a git working tree" in out["message"], out


def test_an_android_user_home_that_does_not_decide_is_not_looked_at(phone_env):
    phone_env.env["ANDROID_USER_HOME"] = "relative/user"  # ANDROID_AVD_HOME is set, so this one is not used
    assert phone_env.run("install-units")[0] == 0


# The units always name the home, so setup and every launch use one directory whatever the environment of the moment is.
# A good value is carried resolved, and escaped like the other values.


def test_a_custom_avd_home_is_written_into_every_unit(phone_env, tmp_path):
    home = tmp_path / "phones" / "avd"
    phone_env.env["ANDROID_AVD_HOME"] = str(home)
    assert phone_env.run("install-units")[0] == 0
    for name in ("a", "b"):
        assert f'Environment="ANDROID_AVD_HOME={home}"' in unit_text(phone_env, name)


@pytest.mark.parametrize("how", ["a symlink", "dot-dot"])
def test_the_unit_carries_the_resolved_avd_home(phone_env, tmp_path, how):
    real = tmp_path / "disk2" / "avd"
    real.mkdir(parents=True)
    if how == "a symlink":
        given = tmp_path / "avd-link"
        given.symlink_to(real)
    else:
        given = tmp_path / "disk1" / ".." / "disk2" / "avd"  # disk1 need not exist
    phone_env.env["ANDROID_AVD_HOME"] = str(given)
    assert phone_env.run("install-units")[0] == 0
    unit = unit_text(phone_env, "a")
    assert f'Environment="ANDROID_AVD_HOME={real}"' in unit and str(given) not in unit


def test_a_percent_and_a_backslash_in_an_avd_home_are_doubled_in_the_unit(phone_env, tmp_path):
    phone_env.env["ANDROID_AVD_HOME"] = str(tmp_path / ("a%h" + BS + "b"))
    assert phone_env.run("install-units")[0] == 0
    want = f'Environment="ANDROID_AVD_HOME={tmp_path}/a%%h{BS}{BS}b"'  # systemd unescapes both inside the quotes
    assert want in unit_text(phone_env, "a") and want in unit_text(phone_env, "b")


def test_with_no_avd_variable_set_the_units_name_the_default_home_and_phones_live_there(phone_env):
    phone_env.env.pop("ANDROID_AVD_HOME")
    phone_env.env["DISPLAY"] = ":0"
    home = default_home(phone_env)
    assert phone_env.run("install-units")[0] == 0
    for name in ("a", "b"):
        assert f'Environment="ANDROID_AVD_HOME={home}"' in unit_text(phone_env, name)  # pinned, not left to a fallback
    snapshots = home / "orion-phone-b.avd" / "snapshots" / "default_boot"
    snapshots.mkdir(parents=True)
    code, out = phone_env.run("_run", "b", "--print")
    assert code == 0, out
    assert not snapshots.parent.exists()  # a locked launch purged them in that home


def test_units_name_the_android_user_homes_avd_directory(phone_env, tmp_path):
    phone_env.env.pop("ANDROID_AVD_HOME")
    phone_env.env["ANDROID_USER_HOME"] = str(tmp_path / "user")
    assert phone_env.run("install-units")[0] == 0
    assert f'Environment="ANDROID_AVD_HOME={tmp_path}/user/avd"' in unit_text(phone_env, "a")


# What a launch does with the home: it uses the one the units name, refuses one that is not there (the emulator would
# look somewhere else, as avdmanager does), and tells the emulator which it is.


def test_a_locked_launch_purges_the_snapshots_of_the_custom_avd_home_not_the_default(phone_env, tmp_path):
    phone_env.env["DISPLAY"] = ":0"
    home = tmp_path / "phones"
    phone_env.env["ANDROID_AVD_HOME"] = str(home)
    custom = home / "orion-phone-b.avd" / "snapshots" / "default_boot"
    default = default_home(phone_env) / "orion-phone-b.avd" / "snapshots" / "default_boot"
    custom.mkdir(parents=True)
    default.mkdir(parents=True)
    code, out = phone_env.run("_run", "b", "--print")
    assert code == 0, out
    assert not custom.parent.exists()
    assert default.exists()  # another home's snapshots are none of its business


@pytest.mark.parametrize("phone", ["a", "b"], ids=["open", "locked"])
@pytest.mark.parametrize("shape", ["missing", "a file"])
def test_a_launch_refuses_an_avd_home_that_is_not_an_existing_directory(phone_env, tmp_path, phone, shape):
    home = tmp_path / "phones"
    if shape == "a file":
        home.write_text("")
    phone_env.env["ANDROID_AVD_HOME"] = str(home)
    phone_env.env["DISPLAY"] = ":0"
    code, out = phone_env.run("_run", phone, "--print")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(home) in out["message"] and "not an existing directory" in out["message"] and "setup" in out["message"], out
    assert home.is_file() if shape == "a file" else not home.exists()  # and the refusal made nothing


@pytest.mark.parametrize("phone", ["a", "b"], ids=["open", "locked"])
def test_a_unit_whose_avd_home_is_gone_never_starts_its_emulator(phone_env, tmp_path, phone):
    proc = run_as_a_unit(phone_env, phone, DISPLAY=":0", ANDROID_AVD_HOME=str(tmp_path / "gone"))
    assert launches(phone_env) == [] and avd_homes_seen(phone_env) == [], "an emulator was started"
    out = json.loads(proc.stdout)
    assert (proc.returncode, out["code"]) == (2, "USAGE"), out
    assert "not an existing directory" in out["message"], out


def test_the_emulator_is_told_the_avd_home_even_by_a_unit_that_does_not_name_it(phone_env):
    home = default_home(phone_env)
    home.mkdir(parents=True)
    base = {key: value for key, value in phone_env.env.items() if key != "ANDROID_AVD_HOME"}  # a unit from before they did
    proc = run_as_a_unit(phone_env, "a", base=base)
    assert proc.returncode == 0, proc.stderr
    assert avd_homes_seen(phone_env) == [str(home)]  # told, not left to find the default itself


def test_a_unit_launches_in_the_avd_home_setup_used(phone_env, tmp_path):
    home = tmp_path / "phones" / "avd"  # a directory that does not exist yet: setup makes it
    phone_env.env["ANDROID_AVD_HOME"] = str(home)
    phone_env.env["DISPLAY"] = ":0"
    assert phone_env.run("setup")[0] == 0
    assert (home / "orion-phone-b.avd" / "config.ini").is_file()  # setup made the phone there
    snapshots = home / "orion-phone-b.avd" / "snapshots" / "default_boot"
    snapshots.mkdir(parents=True)
    # systemd starts the unit with the unit's own Environment lines, not with the variables of the shell that installed it
    shell_only = ("ANDROID_AVD_HOME", "DISPLAY")
    base = {key: value for key, value in phone_env.env.items() if key not in shell_only}
    proc = run_as_a_unit(phone_env, "b", base=base, **unit_environment(phone_env, "b"))
    assert proc.returncode == 0, proc.stderr
    assert not snapshots.parent.exists()  # the launch looked where setup made the phone
    assert avd_homes_seen(phone_env) == [str(home)]  # and the emulator was told the same


@pytest.mark.parametrize("phone", ["a", "b"], ids=["open", "locked"])
def test_a_unit_whose_avd_home_now_lies_in_a_repository_never_starts_its_emulator(phone_env, tmp_path, phone):
    repo = make_repo(tmp_path / "some-repo", "repository")  # a repository can appear around a home after the install
    proc = run_as_a_unit(phone_env, phone, DISPLAY=":0", ANDROID_AVD_HOME=str(repo / "avd"))
    assert launches(phone_env) == [] and avd_homes_seen(phone_env) == [], "an emulator was started"
    out = json.loads(proc.stdout)
    assert (proc.returncode, out["code"]) == (2, "USAGE"), out
    assert "ANDROID_AVD_HOME" in out["message"] and "is inside a git working tree" in out["message"], out


# Only what touches a phone's data reads the variables: an agent's environment may hold any value without breaking it.


@pytest.mark.parametrize("command", [("status", "a"), ("lease", "a", "--holder", "agent-1")], ids=["status", "lease"])
@pytest.mark.parametrize("variable", ["ANDROID_AVD_HOME", "ANDROID_USER_HOME"])
def test_commands_that_never_touch_phone_data_ignore_a_bad_avd_variable(phone_env, command, variable):
    phone_env.env.pop("ANDROID_AVD_HOME")
    phone_env.env[variable] = "relative/avd"
    code, out = phone_env.run(*command)
    assert code == 0, out


# What a new locked session asks the service manager about the phone's unit: `systemctl --user show -p RuntimeMaxUSec
# -p RuntimeRandomizedExtraUSec -p NeedDaemonReload -p Environment UNIT`. The answers below are what systemd 255 printed
# on a real host (transient units with these caps, with and without extra run time, and one with no cap). The extra run
# time is read because RuntimeMaxSec plus RuntimeRandomizedExtraSec lets a session outlast the cap: only `0` passes. The
# environment is read for the state directory the unit watches for its lease (see below). Anything this cannot read with
# certainty is a problem, never a pass.

STATE_DIR = Path("/state/orion")  # the state directory the command under test uses


def cap_problem(monkeypatch, output: str, minutes: int = 120, state_dir: Path = STATE_DIR):
    """unit_cap_problem for a locked phone b with this cap, when systemctl prints `output`; and what it was asked."""
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        return completed(command, 0, output)

    monkeypatch.setattr(subprocess, "run", run)
    return services.unit_cap_problem(PhoneConfig("b", "locked", 5556, minutes), state_dir), asked


def show_output(
    cap: str | None = "2h", reload: str | None = "no", extra: str | None = "0",
    environment: str | None = f"ORION_PHONE_STATE={STATE_DIR}", load: str | None = "loaded",
    kill: str | None = "control-group", stop: str | None = "1min 30s", send: str | None = "yes", restart: str | None = "no",
    core: str | None = "1", core_soft: str | None = "1",
) -> str:
    """What `systemctl --user show` prints for the eleven properties, in systemd's own order; None leaves one out."""
    lines = [
        ("Restart", restart), ("TimeoutStopUSec", stop), ("RuntimeMaxUSec", cap), ("RuntimeRandomizedExtraUSec", extra),
        ("Environment", environment), ("LimitCORE", core), ("LimitCORESoft", core_soft), ("KillMode", kill),
        ("SendSIGKILL", send), ("LoadState", load), ("NeedDaemonReload", reload),
    ]
    return "".join(f"{name}={value}\n" for name, value in lines if value is not None)


def test_the_loaded_unit_is_read_with_exactly_the_eleven_properties(monkeypatch):
    problem, asked = cap_problem(monkeypatch, show_output())
    assert problem is None
    assert asked == [
        [
            "systemctl", "--user", "show", "-p", "LoadState", "-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec",
            "-p", "KillMode", "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "Restart", "-p", "NeedDaemonReload",
            "-p", "Environment", "-p", "LimitCORE", "-p", "LimitCORESoft", "orion-phone-b.service",
        ]
    ]


REAL_CAPS = {120: "2h", 5: "5min", 25: "25min", 90: "1h 30min", 1440: "1d", 121: "2h 1min"}  # minutes: as systemd 255 prints


@pytest.mark.parametrize("minutes, printed", REAL_CAPS.items(), ids=[f"{m} minutes" for m in REAL_CAPS])
def test_the_cap_systemd_prints_for_a_configured_cap_passes(monkeypatch, minutes, printed):
    assert cap_problem(monkeypatch, show_output(cap=printed), minutes)[0] is None


@pytest.mark.parametrize(
    "output",
    [
        (  # the order is not relied on
            "Restart=no\nSendSIGKILL=yes\nKillMode=control-group\nTimeoutStopUSec=90s\nLoadState=loaded\n"
            "Environment=ORION_PHONE_STATE=/state/orion\nNeedDaemonReload=no\nRuntimeRandomizedExtraUSec=0\nRuntimeMaxUSec=2h\n"
            "LimitCORESoft=1\nLimitCORE=1\n"
        ),
        (  # nor are extra lines
            "Id=orion-phone-b.service\nRestart=no\nTimeoutStopUSec=1min 30s\nRuntimeMaxUSec=2h\n"
            "RuntimeRandomizedExtraUSec=0\nEnvironment=ORION_PHONE_STATE=/state/orion\nKillMode=control-group\n"
            "SendSIGKILL=yes\nLoadState=loaded\nNeedDaemonReload=no\nActiveState=inactive\nLimitCORE=1\nLimitCORESoft=1\n"
        ),
        (
            "Restart=no\r\nTimeoutStopUSec=1min 30s\r\nRuntimeMaxUSec=2h\r\nRuntimeRandomizedExtraUSec=0\r\n"
            "Environment=ORION_PHONE_STATE=/state/orion\r\nLimitCORE=1\r\nLimitCORESoft=1\r\nKillMode=control-group\r\n"
            "SendSIGKILL=yes\r\nLoadState=loaded\r\nNeedDaemonReload=no\r\n"
        ),
        show_output(cap="120min"),  # another spelling of the same cap
        show_output(cap="7200s"),
        show_output(cap="1h 60min"),
        show_output(cap="1h60min"),
    ],
    ids=["swapped", "extra lines", "CRLF", "120min", "7200s", "1h 60min", "1h60min"],
)
def test_the_cap_is_read_by_what_it_says_not_by_how_it_is_laid_out(monkeypatch, output):
    assert cap_problem(monkeypatch, output)[0] is None


WRONG_UNITS = {
    "no cap": (show_output(cap="infinity"), ["no session cap", "infinity"]),
    "a shorter cap": (show_output(cap="1h"), ["its session cap is 1h", "120 minutes"]),
    "a longer cap": (show_output(cap="1d"), ["its session cap is 1d", "120 minutes"]),
    "a cap one second over": (show_output(cap="2h 1s"), ["its session cap is 2h 1s"]),
    "a cap one minute under": (show_output(cap="1h 59min"), ["its session cap is 1h 59min"]),
    "a pending reload": (show_output(reload="yes"), ["not reloaded", "NeedDaemonReload=yes"]),
    "no cap and a pending reload": (show_output(cap="infinity", reload="yes"), ["no session cap", "NeedDaemonReload=yes"]),
}


@pytest.mark.parametrize("output, mentions", WRONG_UNITS.values(), ids=WRONG_UNITS)
def test_a_unit_that_is_not_the_capped_one_is_a_problem_that_says_what(monkeypatch, output, mentions):
    problem, _ = cap_problem(monkeypatch, output)
    assert problem is not None and all(mention in problem for mention in mentions), problem


# What stopping the unit does. The session cap, the lease watch and a release all end a phone by stopping its unit, so the stop
# must reach every process the unit started (KillMode=control-group; on systemd 255 `process` kills the supervisor and leaves
# the emulator running, `none` kills nothing, and in both the unit then reads as stopped), and the stop must be given the time
# the watch's own stop can take: a grace for SIGTERM and a second grace after SIGKILL, so two graces, and a stop that is
# never forced (infinity) is no stop at all. A unit that names neither has the manager's defaults, control-group and 90 s
# (`1min 30s`), which pass. These are what the probe printed (transient units given each setting).

NOT_CONTROL_GROUP = ["process", "mixed", "none", "Control-Group", "controlgroup", "whatever"]


@pytest.mark.parametrize("mode", NOT_CONTROL_GROUP)
def test_a_kill_mode_other_than_control_group_is_a_problem_that_says_so(monkeypatch, mode):
    problem, _ = cap_problem(monkeypatch, show_output(kill=mode))
    assert problem is not None and f"KillMode={mode}" in problem and "control-group" in problem, problem
    assert "could not read" not in problem, problem  # it was read: this is what it said


def test_the_kill_mode_the_units_are_written_with_passes(monkeypatch):
    assert cap_problem(monkeypatch, show_output(kill="control-group"))[0] is None


SHORT_STOPS = ["1us", "999ms", "1s", "30s", "59s", "59s 999ms", "59s 999ms 999us"]
LONG_ENOUGH_STOPS = ["1min", "60s", "60000ms", "1min 30s", "90s", "61s", "2min", "1h", "1d"]


@pytest.mark.parametrize("stop", SHORT_STOPS)
def test_a_stop_time_shorter_than_two_graces_is_a_problem_that_says_so(monkeypatch, stop):
    problem, _ = cap_problem(monkeypatch, show_output(stop=stop))
    assert problem is not None and f"TimeoutStopUSec={stop}" in problem and "60 s" in problem, problem
    assert "could not read" not in problem, problem


@pytest.mark.parametrize("stop", LONG_ENOUGH_STOPS)
def test_a_stop_time_of_at_least_two_graces_passes(monkeypatch, stop):
    assert cap_problem(monkeypatch, show_output(stop=stop))[0] is None


def test_a_stop_that_nothing_ever_forces_is_a_problem_that_says_so(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(stop="infinity"))
    assert problem is not None and "TimeoutStopUSec=infinity" in problem and "never forced" in problem, problem
    assert "could not read" not in problem, problem


@pytest.mark.parametrize("grace", [20.0, 30.0, 45.5])
def test_the_stop_time_needed_is_two_graces_whatever_a_grace_is(monkeypatch, grace):
    import fake_systemctl

    monkeypatch.setattr(services, "STOP_GRACE", grace)  # read when asked, not copied: the proof follows the constant
    needed = round(2 * grace * 1000)  # milliseconds
    seconds, millis = divmod(needed, 1000)
    just_enough = f"{seconds}s {millis}ms" if millis else f"{seconds}s"
    one_ms_short = f"{(needed - 1) // 1000}s {(needed - 1) % 1000}ms"
    assert cap_problem(monkeypatch, show_output(stop=just_enough))[0] is None, just_enough
    problem, _ = cap_problem(monkeypatch, show_output(stop=one_ms_short))
    assert problem is not None and f"TimeoutStopUSec={one_ms_short}" in problem, problem
    assert fake_systemctl.timespan(round(2 * grace)) is not None


def test_a_kill_mode_and_a_stop_time_are_both_told_beside_a_wrong_cap_not_instead_of_it(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(cap="1h", kill="none", stop="10s", reload="yes"))
    assert problem is not None
    for mention in ("not reloaded", "its session cap is 1h", "KillMode=none", "TimeoutStopUSec=10s"):
        assert mention in problem, problem


# A stop that runs out of its time is only forced if SendSIGKILL says so. Probed on systemd 255 (transient units, and a process
# that ignores SIGTERM under TimeoutStopSec=2): with the default `SendSIGKILL=yes` the stop ended after the two seconds with
# nothing left running; with `SendSIGKILL=no` it ended with every process of the unit still running, and the unit read as
# stopped. So only `yes` passes, and what is not `yes`, or cannot be read, is a problem.

NOT_YES = ["no", "No", "YES", "true", "1", "whatever"]


@pytest.mark.parametrize("send", NOT_YES)
def test_a_send_sigkill_other_than_yes_is_a_problem_that_says_so(monkeypatch, send):
    problem, _ = cap_problem(monkeypatch, show_output(send=send))
    assert problem is not None and f"SendSIGKILL={send}" in problem and "must be yes" in problem, problem
    assert "could not read" not in problem, problem  # it was read: this is what it said


def test_the_send_sigkill_the_units_have_by_default_passes(monkeypatch):
    assert cap_problem(monkeypatch, show_output(send="yes"))[0] is None


def test_a_send_sigkill_of_no_is_told_beside_the_kill_mode_and_the_stop_time(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(kill="process", stop="10s", send="no"))
    assert problem is not None
    for mention in ("KillMode=process", "TimeoutStopUSec=10s", "SendSIGKILL=no"):
        assert mention in problem, problem


# A crash of a locked phone's emulator must leave no dump of the phone's memory, which holds what the lock protects once its
# owner has unlocked it. The unit is written with LimitCORE=1, which sets both limits to 1, and only that keeps the dump from a
# crash handler the kernel pipes dumps to (systemd-coredump, WSL2's crash capture): such a handler gets one whatever the soft
# limit, 0 included, except at 1 (checked on WSL2: a crash under 0 was captured, under 1 it was not). A hard limit above 1
# would let the process raise its soft limit again. A unit that names none has the manager's defaults, LimitCORE=infinity and
# LimitCORESoft=0 (probed on systemd 255), which fail, as every unit written before LimitCORE does.

NOT_ONE_CORE = {
    "the manager's defaults, as a unit written before LimitCORE": ("infinity", "0"),
    "no limit at all": ("infinity", "infinity"),
    "a limit of 0, which a piped dump ignores": ("0", "0"),
    "a hard limit the soft one could be raised to": ("infinity", "1"),
    "a soft limit of 0 under a hard limit of 1": ("1", "0"),
    "a limit of 2": ("2", "2"),
}


@pytest.mark.parametrize("hard, soft", NOT_ONE_CORE.values(), ids=NOT_ONE_CORE)
def test_a_core_limit_other_than_1_is_a_problem_that_says_so(monkeypatch, hard, soft):
    problem, _ = cap_problem(monkeypatch, show_output(core=hard, core_soft=soft))
    assert problem is not None and "must be 1" in problem, problem
    for name, value in (("LimitCORE", hard), ("LimitCORESoft", soft)):
        if value != "1":
            assert f"{name}={value}," in problem, problem
    assert "could not read" not in problem, problem  # it was read: this is what it said


def test_the_core_limit_the_units_are_written_with_passes(monkeypatch):
    assert cap_problem(monkeypatch, show_output(core="1", core_soft="1"))[0] is None


@pytest.mark.parametrize("missing, name", [("core", "LimitCORE"), ("core_soft", "LimitCORESoft")])
def test_a_core_limit_that_systemctl_does_not_print_is_a_problem(monkeypatch, missing, name):
    problem, _ = cap_problem(monkeypatch, show_output(**{missing: None}))
    assert problem is not None and f"could not read {name} from systemctl" in problem, problem


# A locked phone starts only at the beginning of a session, by this tool's own cold boot, and its run is over when its unit
# ends: the unit is written with Restart=no, which is what keeps the manager from starting it again by itself (a boot that no
# lease asked for). A drop-in can change that, so the loaded Restart is read, and only `no` passes (what a unit that names none
# has too); another value, or none that can be read, is a problem. Read on systemd 255: `Restart=no` for a locked phone's unit.

NOT_NO = ["on-failure", "on-abnormal", "on-abort", "on-success", "on-watchdog", "always", "No", "NO", "no ", "whatever"]


@pytest.mark.parametrize("restart", NOT_NO)
def test_a_restart_other_than_no_is_a_problem_that_says_so(monkeypatch, restart):
    problem, _ = cap_problem(monkeypatch, show_output(restart=restart))
    assert problem is not None and f"Restart={restart}" in problem and "must be no" in problem, problem
    assert "could not read" not in problem, problem  # it was read: this is what it said


def test_the_restart_the_locked_units_are_written_with_passes(monkeypatch):
    assert cap_problem(monkeypatch, show_output(restart="no"))[0] is None


@pytest.mark.parametrize("output", [show_output(restart=None), show_output(restart="")], ids=["missing", "empty"])
def test_a_restart_that_cannot_be_read_is_a_problem_never_a_pass(monkeypatch, output):
    problem, _ = cap_problem(monkeypatch, output)
    assert problem is not None and "could not read Restart from systemctl" in problem, problem


def test_a_restart_is_told_beside_the_other_things_that_are_wrong_with_the_unit(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(cap="1h", restart="always", reload="yes"))
    assert problem is not None
    for mention in ("its session cap is 1h", "Restart=always", "NeedDaemonReload=yes"):
        assert mention in problem, problem


# A unit systemd has not loaded proves nothing about a session: what `show` prints for it is systemd's defaults (no cap, an
# empty environment), not a unit's own settings. A name no unit file was ever made for (a first install, or a file that was
# removed) is `not-found` (probed on systemd 255: exit 0, LoadState=not-found); anything else but `loaded` is refused the
# same way, and the problem says that it is the unit that is missing, not that its cap or its state directory is wrong.

NOT_LOADED = {
    "not found": ("not-found", ["no unit named orion-phone-b.service", "LoadState=not-found", "not installed"]),
    "masked": ("masked", ["has not loaded", "LoadState=masked"]),
    "an error": ("error", ["has not loaded", "LoadState=error"]),
    "a bad setting": ("bad-setting", ["has not loaded", "LoadState=bad-setting"]),
    "a stub": ("stub", ["has not loaded", "LoadState=stub"]),
    "something it does not know": ("whatever", ["has not loaded", "LoadState=whatever"]),
}


@pytest.mark.parametrize("load, mentions", NOT_LOADED.values(), ids=NOT_LOADED)
def test_a_unit_that_is_not_loaded_is_a_problem_that_says_so_and_only_that(monkeypatch, load, mentions):
    # what systemd prints for a unit it does not have: no cap, no environment, nothing pending
    problem, _ = cap_problem(monkeypatch, show_output(cap="infinity", environment="", load=load))
    assert problem is not None and all(mention in problem for mention in mentions), problem
    assert "session cap" not in problem and "state directory" not in problem and "could not read" not in problem, problem


def test_a_loaded_unit_is_not_told_to_be_not_loaded(monkeypatch):
    assert cap_problem(monkeypatch, show_output(load="loaded"))[0] is None


@pytest.mark.parametrize("load", [None, ""], ids=["no LoadState line", "an empty LoadState"])
def test_a_load_state_that_is_not_there_is_a_problem_never_a_pass(monkeypatch, load):
    problem, _ = cap_problem(monkeypatch, show_output(load=load))
    assert problem is not None and "could not read LoadState" in problem, problem


# The right cap is not enough: with RuntimeRandomizedExtraSec the manager stops the unit at the cap plus a random extra
# span, so a session may run that much longer than the config says. The unit file install-units writes sets none, but a
# drop-in or `systemctl set-property` can, and neither is what the cap check looked at before.

EXTRA_TIME = {
    "a day of extra time": ("1d", ["RuntimeRandomizedExtraUSec=1d", "outlast its cap"]),
    "an hour and a half": ("1h 30min", ["RuntimeRandomizedExtraUSec=1h 30min"]),
    "ninety seconds": ("1min 30s", ["RuntimeRandomizedExtraUSec=1min 30s"]),
    "one second": ("1s", ["RuntimeRandomizedExtraUSec=1s"]),
    "one microsecond": ("1us", ["RuntimeRandomizedExtraUSec=1us"]),
    "unlimited": ("infinity", ["RuntimeRandomizedExtraUSec=infinity", "outlast its cap"]),
}


@pytest.mark.parametrize("extra, mentions", EXTRA_TIME.values(), ids=EXTRA_TIME)
def test_a_unit_that_may_run_past_its_cap_is_a_problem_that_says_how_long(monkeypatch, extra, mentions):
    problem, _ = cap_problem(monkeypatch, show_output(extra=extra))  # the cap itself is exactly right
    assert problem is not None and all(mention in problem for mention in mentions), problem
    assert "could not read" not in problem, problem  # it was read: this is what it said


def test_extra_time_is_reported_beside_a_wrong_cap_not_instead_of_it(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(cap="1h", extra="1d", reload="yes"))
    assert problem is not None
    for mention in ("not reloaded", "its session cap is 1h", "RuntimeRandomizedExtraUSec=1d"):
        assert mention in problem, problem


def test_no_extra_time_is_the_text_zero(monkeypatch):
    assert cap_problem(monkeypatch, show_output(extra="0"))[0] is None  # what systemd 255 prints when there is none


UNREADABLE_ANSWERS = {
    "no output at all": "",
    "no cap line": show_output(cap=None),
    "no reload line": show_output(reload=None),
    "no load state line": show_output(load=None),
    "an empty load state": show_output(load=""),
    "no kill mode line": show_output(kill=None),
    "an empty kill mode": show_output(kill=""),
    "no SendSIGKILL line": show_output(send=None),
    "an empty SendSIGKILL": show_output(send=""),
    "no stop time line": show_output(stop=None),
    "an empty stop time": show_output(stop=""),
    "a stop time in words": show_output(stop="soon"),
    "a stop time with no unit": show_output(stop="90"),
    "a stop time of zero": show_output(stop="0"),  # printed for TimeoutStopSec=0, which is no span this reads with certainty
    "a stop time in capitals": show_output(stop="1MIN"),
    "a stop time with trailing junk": show_output(stop="2min junk"),
    "an empty cap": show_output(cap=""),
    "words": show_output(cap="forever"),
    "a number with no unit": show_output(cap="7200"),
    "a fraction": show_output(cap="2.0h"),
    "a negative": show_output(cap="-2h"),
    "trailing junk": show_output(cap="2h junk"),
    "a unit it does not know": show_output(cap="2 hours"),
    "a hex number": show_output(cap="0x1c20"),
    "digits that are not ASCII": show_output(cap=chr(0xFF12) + "h"),
    "an upper-case unit": show_output(cap="2H"),
    "a reload answer that is neither": show_output(reload="maybe"),
    "an empty reload answer": show_output(reload=""),
    "a reload answer in capitals": show_output(reload="NO"),
    "no extra time line": show_output(extra=None),
    "an empty extra time": show_output(extra=""),
    "extra time in words": show_output(extra="soon"),
    "a number that is not zero and has no unit": show_output(extra="1"),
    "zero spelled with a unit": show_output(extra="0s"),  # systemd prints zero as 0: another spelling is not taken for it
    "zero with trailing junk": show_output(extra="0 junk"),
    "two zeros": show_output(extra="00"),
    "a fraction of extra time": show_output(extra="0.5h"),
    "negative extra time": show_output(extra="-1h"),
    "extra time in capitals": show_output(extra="INFINITY"),
    "extra time with a unit it does not know": show_output(extra="1 hour"),
}


@pytest.mark.parametrize("output", UNREADABLE_ANSWERS.values(), ids=UNREADABLE_ANSWERS)
def test_an_answer_that_cannot_be_read_with_certainty_is_a_problem_never_a_pass(monkeypatch, output):
    problem, _ = cap_problem(monkeypatch, output)
    assert problem is not None and "could not read" in problem, problem


def test_a_systemd_that_prints_no_extra_time_is_refused_and_the_problem_says_why(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(extra=None))  # systemd before 250 has no such property
    assert problem is not None
    assert "RuntimeRandomizedExtraUSec" in problem and "got nothing" in problem, problem
    assert "systemd before 250" in problem and "never taken as zero" in problem, problem


# What `systemctl show` prints is read as exact records: a line is `name=value` and ends at a newline, with at most one carriage
# return before it (a CR LF line end), and nothing else is trimmed from the name or the value. Probed on systemd 255 with
# transient units (a default one, environments with a trailing space, tab, carriage return and backslash, an empty environment
# value, caps with and without spaces): no line has a carriage return, a blank at either end, or a blank around the `=`, so a
# blank there is not systemd's. A property whose record is not exact is unreadable, and unreadable is refused.


def shown(monkeypatch, output: str, *names: str) -> dict:
    def run(command, **kwargs):
        return completed(command, 0, output)

    monkeypatch.setattr(subprocess, "run", run)
    return services._show("orion-phone-b.service", *names)


@pytest.mark.parametrize(
    "output, expected",
    [
        ("A=1\n", {"A": "1"}),
        ("A=1\r\n", {"A": "1"}),  # a CR LF line end: one carriage return is part of the line end
        ("A=1\r\r\n", {"A": "1\r"}),  # a second is the value's own
        ("A=1\r\r\r\n", {"A": "1\r\r"}),
        ("A=1 \n", {"A": "1 "}),
        ("A=1\t\n", {"A": "1\t"}),
        ("A= 1\n", {"A": " 1"}),
        ("A=\t1\n", {"A": "\t1"}),
        ("A=\n", {"A": ""}),
        ("A=\r\n", {"A": ""}),
        (" A=1\n", {" A": "1"}),  # not a record of A: its name is another name
        ("\tA=1\n", {"\tA": "1"}),
        ("A =1\n", {"A ": "1"}),
        ("A\t=1\n", {"A\t": "1"}),
        ("A=1=2\n", {"A": "1=2"}),
        ("no equals sign\n", {}),
        ("=1\n", {}),
    ],
    ids=lambda value: repr(value),
)
def test_a_record_is_taken_exactly_as_printed_less_one_carriage_return(monkeypatch, output, expected):
    assert shown(monkeypatch, output) == expected


PROPERTIES_READ = ["LoadState", "RuntimeMaxUSec", "RuntimeRandomizedExtraUSec", "KillMode", "TimeoutStopUSec", "SendSIGKILL",
                   "Restart", "NeedDaemonReload", "Environment"]


def with_line_end(output: str, name: str, ending: str) -> str:
    """`output` with the line of `name` ending in `ending` (before its newline)."""
    lines = output.split("\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(name + "="))
    lines[index] += ending
    return "\n".join(lines)


@pytest.mark.parametrize("name", PROPERTIES_READ)
def test_a_property_whose_line_ends_in_two_carriage_returns_is_not_read_as_it_would_be_with_none(monkeypatch, name):
    assert cap_problem(monkeypatch, show_output())[0] is None
    assert cap_problem(monkeypatch, with_line_end(show_output(), name, "\r"))[0] is None  # a CR LF line end: one is fine
    problem, _ = cap_problem(monkeypatch, with_line_end(show_output(), name, "\r\r"))
    assert problem is not None, f"{name} with two carriage returns was read as if it had none"


@pytest.mark.parametrize("name", PROPERTIES_READ)
@pytest.mark.parametrize("ending", [" ", "\t", "\r\r", " \r"], ids=["a space", "a tab", "two carriage returns", "a space and a CR"])
def test_a_property_with_a_blank_after_its_value_is_not_read_as_it_would_be_without(monkeypatch, name, ending):
    problem, _ = cap_problem(monkeypatch, with_line_end(show_output(), name, ending))
    assert problem is not None, f"{name} with {ending!r} after it was read as if it had nothing"


BLANKS_AROUND = {
    "a space before the cap": show_output(cap=" 2h"),
    "a tab before the cap": show_output(cap="\t2h"),
    "a space after the cap": show_output(cap="2h "),
    "a tab after the cap": show_output(cap="2h\t"),
    "two carriage returns after the cap": show_output(cap="2h\r\r"),
    "a space before the extra time": show_output(extra=" 0"),
    "a space after the extra time": show_output(extra="0 "),
    "a space before the load state": show_output(load=" loaded"),
    "a space after the load state": show_output(load="loaded "),
    "a space before the reload state": show_output(reload=" no"),
    "a space after the reload state": show_output(reload="no "),
    "a space before the kill mode": show_output(kill=" control-group"),
    "a space after the kill mode": show_output(kill="control-group "),
    "a space before the stop time": show_output(stop=" 1min 30s"),
    "a space after the stop time": show_output(stop="1min 30s "),
    "a space before SendSIGKILL": show_output(send=" yes"),
    "a space after SendSIGKILL": show_output(send="yes "),
}


@pytest.mark.parametrize("output", BLANKS_AROUND.values(), ids=BLANKS_AROUND)
def test_a_blank_around_a_value_is_refused_not_trimmed_into_a_match(monkeypatch, output):
    problem, _ = cap_problem(monkeypatch, output)
    assert problem is not None, problem


NAMES_WITH_BLANKS = {
    "a space before the name": " RuntimeMaxUSec=2h",
    "a tab before the name": "\tRuntimeMaxUSec=2h",
    "a space before the equals sign": "RuntimeMaxUSec =2h",
    "a tab before the equals sign": "RuntimeMaxUSec\t=2h",
}


@pytest.mark.parametrize("line", NAMES_WITH_BLANKS.values(), ids=NAMES_WITH_BLANKS)
def test_a_name_with_a_blank_in_it_is_another_name_and_the_cap_is_unreadable(monkeypatch, line):
    output = show_output().replace("RuntimeMaxUSec=2h", line)
    problem, _ = cap_problem(monkeypatch, output)
    assert problem is not None and "could not read RuntimeMaxUSec" in problem and "got nothing" in problem, problem


def test_the_exact_record_beside_a_malformed_one_is_still_the_one_that_is_read(monkeypatch):
    output = show_output().replace("RuntimeMaxUSec=2h", " RuntimeMaxUSec=infinity\nRuntimeMaxUSec=2h")
    assert cap_problem(monkeypatch, output)[0] is None  # the line that is not a record of the cap says nothing about it


def test_a_unit_whose_state_line_carries_a_second_carriage_return_is_not_active(monkeypatch):
    def run(command, **kwargs):
        return completed(command, 0, "ActiveState=active\r\r\nInvocationID=" + "a" * 32 + "\r\r\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert services.invocation_id("orion-phone-b.service") == ""  # `active\r` is not `active`: not a boot a session can stand on


# The capture is bytes, decoded as UTF-8 with replacement and no newline translation: a capture in text mode turns `2h` and two
# carriage returns before the newline into `2h` and an empty line before anything looks at it, which defeats the exact records
# `_show` reads. The tests below go from the fake systemctl through the CLI (lone and repeated carriage returns), and a direct
# one looks at what reaches the reader and at how the capture is asked for.


def test_systemctl_output_reaches_the_reader_as_printed_with_no_newline_translation(monkeypatch):
    raw = b"A=1\r\r\nB=2\rC=3\r\nD=\xff\n"
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, raw, b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert services._systemctl("show", "orion-phone-b.service") == "A=1\r\r\nB=2\rC=3\r\nD=" + chr(0xFFFD) + "\n"
    for text_mode in ("text", "universal_newlines", "encoding", "errors"):
        assert not seen.get(text_mode), f"the capture was asked for with {text_mode}, which translates or decodes for us"


def test_a_failure_decodes_standard_error_the_same_way(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, b"", b"Failed \xff\r\n"))
    with pytest.raises(PhoneError) as caught:
        services._systemctl("stop", "orion-phone-b.service")
    assert caught.value.message.endswith("failed: Failed " + chr(0xFFFD)), caught.value.message


ODD_LINE_ENDS = {
    "two carriage returns and a newline": "\r\r\n",
    "a lone carriage return": "\r",
    "two lone carriage returns": "\r\r",
    "a carriage return after the newline": "\n\r",
    "a newline and a carriage return and a newline": "\r\n\r",
}


@pytest.mark.parametrize("ending", ODD_LINE_ENDS.values(), ids=ODD_LINE_ENDS)
def test_a_lease_whose_unit_is_read_with_odd_line_ends_is_refused_not_read_as_if_clean(phone_env, ending):
    phone_env.set_fake("show_line_end", ending)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "install-units" in out["message"], out
    assert ["--user", "start", UNIT_B] not in phone_env.calls("systemctl")  # never started on a read that is not exact


def test_a_lease_whose_unit_is_read_with_crlf_line_ends_is_started_as_with_newlines(phone_env):
    phone_env.set_fake("show_line_end", "\r\n")  # one carriage return is part of a line end
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert ["--user", "start", UNIT_B] in phone_env.calls("systemctl")


@pytest.mark.parametrize("ending", ODD_LINE_ENDS.values(), ids=ODD_LINE_ENDS)
def test_install_units_stops_a_running_locked_phone_whose_unit_is_read_with_odd_line_ends(phone_env, ending):
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("show_line_end", ending)
    code, out = phone_env.run("install-units")
    assert code == 0 and set(out["data"]["stopped"]) == {"b"}, out  # not known to be capped: stopped, as one that cannot be read
    assert UNIT_B not in active_units(phone_env)


def test_install_units_leaves_a_running_capped_phone_alone_when_lines_end_in_crlf(phone_env):
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("show_line_end", "\r\n")
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert UNIT_B in active_units(phone_env)


# A carriage return is part of a line end only immediately before a newline. The last fragment of the output, after its last
# newline, was not ended by one, so a carriage return at its end is not a line end and stays in the value: an output that stops
# short of its newline, with a carriage return where the newline should be, is not read as a clean line. The last line is made
# any of the properties the lease reads, through the fake systemctl, and by a direct call that is the reviewer's own case.

LEASE_PROPERTIES = [
    "RuntimeMaxUSec", "RuntimeRandomizedExtraUSec", "KillMode", "TimeoutStopUSec", "SendSIGKILL", "Restart", "LoadState",
    "Environment", "NeedDaemonReload",
]


@pytest.mark.parametrize("name", LEASE_PROPERTIES)
def test_a_lease_whose_last_property_ends_in_a_carriage_return_with_no_newline_is_refused(phone_env, name):
    phone_env.set_fake("show_last", name)  # printed last
    phone_env.set_fake("show_last_end", "\r")  # and cut short: a carriage return where the newline should be
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "install-units" in out["message"], out
    assert ["--user", "start", UNIT_B] not in phone_env.calls("systemctl")  # never started on a read that is not exact


@pytest.mark.parametrize("name", LEASE_PROPERTIES)
def test_a_lease_whose_last_property_ends_in_crlf_is_started_as_with_newlines(phone_env, name):
    phone_env.set_fake("show_last", name)
    phone_env.set_fake("show_last_end", "\r\n")  # the carriage return is before a newline: a line end
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert ["--user", "start", UNIT_B] in phone_env.calls("systemctl")


def test_install_units_stops_a_running_locked_phone_whose_cap_is_cut_short_by_a_carriage_return(phone_env):
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("show_last", "RuntimeMaxUSec")
    phone_env.set_fake("show_last_end", "\r")
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out  # the cap cannot be read: not left alone as a capped phone
    assert UNIT_B not in active_units(phone_env)


def test_install_units_leaves_a_running_capped_phone_alone_when_its_cap_ends_in_crlf(phone_env):
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("show_last", "RuntimeMaxUSec")
    phone_env.set_fake("show_last_end", "\r\n")
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert UNIT_B in active_units(phone_env)


def shown(monkeypatch, output: str) -> dict[str, str]:
    monkeypatch.setattr(services, "_systemctl", lambda *args: output)
    return services._show("orion-phone-b.service", "KillMode", "RuntimeMaxUSec")


@pytest.mark.parametrize(
    "output, expected",
    [
        ("KillMode=control-group\nRuntimeMaxUSec=2h\n", "2h"),
        ("KillMode=control-group\r\nRuntimeMaxUSec=2h\r\n", "2h"),
        ("KillMode=control-group\nRuntimeMaxUSec=2h\r\n", "2h"),
        ("KillMode=control-group\nRuntimeMaxUSec=2h\r", "2h\r"),
        ("KillMode=control-group\r\nRuntimeMaxUSec=2h\r", "2h\r"),
        ("KillMode=control-group\nRuntimeMaxUSec=2h\r\r", "2h\r\r"),
        ("KillMode=control-group\nRuntimeMaxUSec=2h\r\r\n", "2h\r"),
    ],
    ids=["newlines", "CR LF", "CR LF last", "last ends in a lone CR", "CR LF then a last CR", "last ends in two CRs", "two CRs and a newline"],
)
def test_the_last_property_keeps_a_carriage_return_that_no_newline_follows(monkeypatch, output, expected):
    assert shown(monkeypatch, output)["RuntimeMaxUSec"] == expected


def clean_unit_output(state_dir, last: str, ending: str) -> str:
    """What a loaded locked unit with the configured cap prints for the lease's read, with `last` printed last and ended by
    `ending`, and every other line ended by a newline."""
    values = {
        "Restart": "no", "TimeoutStopUSec": "1min 30s", "RuntimeMaxUSec": "2h", "RuntimeRandomizedExtraUSec": "0",
        "Environment": f"ORION_PHONE_STATE={state_dir}", "LimitCORE": "1", "LimitCORESoft": "1", "KillMode": "control-group",
        "SendSIGKILL": "yes", "LoadState": "loaded", "NeedDaemonReload": "no",
    }
    lines = [f"{name}={value}" for name, value in values.items() if name != last] + [f"{last}={values[last]}"]
    return "\n".join(lines) + ending


@pytest.mark.parametrize("name", ["RuntimeMaxUSec", "Environment"])
def test_the_proof_of_a_loaded_unit_does_not_read_a_last_line_cut_short_by_a_carriage_return_as_whole(phone_env, in_process, monkeypatch, name):
    cfg = config.load()
    phone = cfg.phones["b"]
    monkeypatch.setattr(services, "_systemctl", lambda *args: clean_unit_output(cfg.state_dir, name, "\n"))
    assert services.unit_cap_problem(phone, cfg.state_dir) is None  # the control: the same text, newline ended, is a pass
    monkeypatch.setattr(services, "_systemctl", lambda *args: clean_unit_output(cfg.state_dir, name, "\r\n"))
    assert services.unit_cap_problem(phone, cfg.state_dir) is None  # a carriage return before the newline is a line end
    monkeypatch.setattr(services, "_systemctl", lambda *args: clean_unit_output(cfg.state_dir, name, "\r"))
    problem = services.unit_cap_problem(phone, cfg.state_dir)
    assert problem is not None and name in problem, problem  # a carriage return with no newline is part of the value


# `not_installed` is asked after a stop failed, to tell a unit that was never installed from any other reason a stop can fail.
# It is yes only when systemd says it has no unit by that name and that nothing runs under it; anything less is no, and a
# look that cannot be made is no (the stop's own failure then stands).


def not_installed(monkeypatch, output: str, returncode: int = 0) -> bool:
    def run(command, **kwargs):
        return completed(command, returncode, output, "Failed to connect to bus" if returncode else "")

    monkeypatch.setattr(subprocess, "run", run)
    return services.not_installed("orion-phone-b.service")


@pytest.mark.parametrize(
    "output, expected",
    [
        ("LoadState=not-found\nActiveState=inactive\n", True),  # as probed
        ("ActiveState=inactive\nLoadState=not-found\n", True),  # whatever the order
        ("LoadState=not-found\nActiveState=failed\n", True),  # no process behind it either
        ("LoadState=not-found\nActiveState=active\n", False),  # runs though its file is gone: a unit that can be stopped
        ("LoadState=not-found\nActiveState=deactivating\n", False),
        ("LoadState=not-found\n", False),  # whether anything runs is not known
        ("LoadState=not-found\nActiveState=\n", False),
        ("LoadState=loaded\nActiveState=inactive\n", False),  # it is installed
        ("LoadState=masked\nActiveState=inactive\n", False),
        ("ActiveState=inactive\n", False),  # whether it is there is not known
        ("LoadState=\nActiveState=inactive\n", False),
        ("LoadState=not-found\nLoadState=not-found\nActiveState=inactive\n", False),  # a property printed twice is refused
        ("", False),
    ],
)
def test_a_unit_is_not_installed_only_when_systemd_has_none_and_nothing_runs_under_it(monkeypatch, output, expected):
    assert not_installed(monkeypatch, output) is expected


def test_a_look_that_cannot_be_made_does_not_say_the_unit_is_not_installed(monkeypatch):
    assert not_installed(monkeypatch, "LoadState=not-found\nActiveState=inactive\n", returncode=1) is False


def test_systemctl_failing_to_answer_is_an_error_not_a_pass(monkeypatch):
    def run(command, **kwargs):
        return completed(command, 1, "", "Failed to connect to bus")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PhoneError) as caught:
        services.unit_cap_problem(PhoneConfig("b", "locked", 5556, 120), STATE_DIR)
    assert caught.value.code is Code.PHONE_DOWN and caught.value.data == {"unit": "orion-phone-b.service"}


# The fake systemctl is only worth having while it prints what real systemd does. These are the probe's own results.

PROBED_SYSTEMD_255 = {  # RuntimeMaxSec in seconds (None: not set) -> RuntimeMaxUSec as systemd 255 printed it
    7200: "2h", 300: "5min", 1500: "25min", 5400: "1h 30min", 7260: "2h 1min", 86400: "1d", 45: "45s", None: "infinity",
}
PROBED_EXTRA_SYSTEMD_255 = {  # RuntimeRandomizedExtraSec in seconds -> RuntimeRandomizedExtraUSec as systemd 255 printed it
    0: "0", 90: "1min 30s", 3600: "1h", 86400: "1d",
}


def test_the_fake_systemctl_prints_a_cap_as_real_systemd_255_does():
    import fake_systemctl

    assert {cap: fake_systemctl.timespan(cap) for cap in PROBED_SYSTEMD_255} == PROBED_SYSTEMD_255


def test_the_fake_systemctl_prints_extra_time_as_real_systemd_255_does():
    import fake_systemctl

    assert {extra: fake_systemctl.timespan(extra) for extra in PROBED_EXTRA_SYSTEMD_255} == PROBED_EXTRA_SYSTEMD_255
    state = {"loaded": {"orion-phone-b.service": {"cap": 7200, "reload_pending": False, "extra": 86400}}}
    shown = fake_systemctl.show(state, ["-p", "RuntimeRandomizedExtraUSec", "orion-phone-b.service"])
    assert shown == "RuntimeRandomizedExtraUSec=1d\n"
    assert fake_systemctl.show({}, ["-p", "RuntimeRandomizedExtraUSec", "orion-phone-b.service"]) == "RuntimeRandomizedExtraUSec=0\n"


def test_the_fake_systemctl_prints_the_properties_in_systemds_own_order():
    import fake_systemctl

    asked = [
        "-p", "NeedDaemonReload", "-p", "InvocationID", "-p", "Environment", "-p", "RuntimeRandomizedExtraUSec",
        "-p", "RuntimeMaxUSec", "-p", "ActiveState", "-p", "KillMode", "-p", "TimeoutStopUSec", "-p", "LoadState",
        "-p", "SendSIGKILL", "orion-phone-b.service",
    ]
    state = {"loaded": {"orion-phone-b.service": {"cap": 7200, "reload_pending": False, "environment_text": "A=1 B=2"}}}
    shown = fake_systemctl.show(state, asked).splitlines()
    assert shown == [  # as probed, on a unit with the manager's defaults for how it is stopped
        "TimeoutStopUSec=1min 30s", "RuntimeMaxUSec=2h", "RuntimeRandomizedExtraUSec=0", "Environment=A=1 B=2",
        "KillMode=control-group", "SendSIGKILL=yes", "LoadState=loaded", "ActiveState=inactive", "NeedDaemonReload=no",
        "InvocationID=",
    ]


PROBED_KILL_MODES_SYSTEMD_255 = ["control-group", "process", "mixed", "none"]  # KillMode as set: printed as set
PROBED_STOP_TIMEOUTS_SYSTEMD_255 = {  # TimeoutStopSec in seconds -> TimeoutStopUSec as printed (90 s is the manager's default)
    30: "30s", 59: "59s", 60: "1min", 90: "1min 30s", 3600: "1h",
}


PROBED_SEND_SIGKILL_SYSTEMD_255 = ["yes", "no"]  # SendSIGKILL as set, printed as set; a unit that names none prints yes


def test_the_fake_systemctl_prints_send_sigkill_as_real_systemd_255_does():
    import fake_systemctl

    for value in PROBED_SEND_SIGKILL_SYSTEMD_255:
        state = {"loaded": {"orion-phone-b.service": {"cap": 7200, "reload_pending": False, "send_sigkill": value}}}
        assert fake_systemctl.show(state, ["-p", "SendSIGKILL", "orion-phone-b.service"]) == f"SendSIGKILL={value}\n"
    state = {"loaded": {"orion-phone-b.service": {"cap": 7200, "reload_pending": False}}}
    assert fake_systemctl.show(state, ["-p", "SendSIGKILL", "orion-phone-b.service"]) == "SendSIGKILL=yes\n"  # the default
    assert fake_systemctl.DEFAULT_SEND_SIGKILL == "yes"


def test_the_fake_systemctl_prints_a_stop_time_and_a_kill_mode_as_real_systemd_255_does():
    import fake_systemctl

    assert {seconds: fake_systemctl.timespan(seconds) for seconds in PROBED_STOP_TIMEOUTS_SYSTEMD_255} == PROBED_STOP_TIMEOUTS_SYSTEMD_255
    for mode in PROBED_KILL_MODES_SYSTEMD_255:
        state = {"loaded": {"orion-phone-b.service": {"cap": 7200, "reload_pending": False, "kill_mode": mode, "stop_timeout": 59}}}
        shown = fake_systemctl.show(state, ["-p", "KillMode", "-p", "TimeoutStopUSec", "orion-phone-b.service"])
        assert shown == f"TimeoutStopUSec=59s\nKillMode={mode}\n"
    assert fake_systemctl.DEFAULT_STOP_TIMEOUT == 90 and fake_systemctl.DEFAULT_KILL_MODE == "control-group"


# What systemd 255 did for a unit it has no file for, probed with a transient name nothing had ever made: `show` exits 0 and
# prints the unit's defaults (and `LoadState=not-found`), `stop` exits 5 ("not loaded"), `start` exits 5 ("not found"), and
# `enable --now` exits 1 ("does not exist"). The fake models exactly that, so that a test of a first install is one.

PROBED_NOT_FOUND_SYSTEMD_255 = [  # in the order systemd printed them, whatever order they were asked for
    "TimeoutStopUSec=1min 30s", "RuntimeMaxUSec=infinity", "RuntimeRandomizedExtraUSec=0", "Environment=",
    "KillMode=control-group", "SendSIGKILL=yes", "LoadState=not-found", "ActiveState=inactive", "NeedDaemonReload=no",
    "InvocationID=",
]
PROBED_NOT_FOUND_VERBS = {  # verb words -> (exit status, standard error), for the unit name {unit}
    "stop": (5, "Failed to stop {unit}: Unit {unit} not loaded.\n"),
    "start": (5, "Failed to start {unit}: Unit {unit} not found.\n"),
    "enable --now": (1, "Failed to enable unit: Unit file {unit} does not exist.\n"),
    "disable": (1, "Failed to disable unit: Unit file {unit} does not exist.\n"),
}


def test_the_fake_systemctl_prints_a_unit_it_has_not_found_as_real_systemd_255_does():
    import fake_systemctl

    asked = []
    for name in (
        "InvocationID", "NeedDaemonReload", "ActiveState", "LoadState", "SendSIGKILL", "KillMode", "Environment",
        "RuntimeRandomizedExtraUSec", "RuntimeMaxUSec", "TimeoutStopUSec",
    ):
        asked += ["-p", name]
    state = {"loaded": {"orion-phone-b.service": {"cap": 7200, "reload_pending": True, "not_found": True}}}  # the cap is no one's
    shown = fake_systemctl.show(state, [*asked, "orion-phone-b.service"]).split("\n")
    assert shown == [*PROBED_NOT_FOUND_SYSTEMD_255, ""]


# Probed on a real host (systemd 255) with a throwaway unit that has an [Install] section: phone b went from
# NeedDaemonReload=no to yes after `enable --no-reload`, and so did phone a; a daemon-reload brought both back to no;
# `disable --no-reload` made both yes again. The flag belongs to the manager, not to a unit. The fake models exactly that.


def test_the_fake_systemctl_marks_every_unit_for_a_reload_after_a_link_change_without_one_as_real_systemd_255_does(phone_env):
    def systemctl(*words: str) -> str:
        proc = subprocess.run(["systemctl", "--user", *words], env=phone_env.env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def owed() -> dict[str, str]:
        return {unit: systemctl("show", "-p", "NeedDaemonReload", "--value", unit).strip() for unit in (UNIT_A, UNIT_B)}

    assert owed() == {UNIT_A: "no", UNIT_B: "no"}
    systemctl("enable", "--no-reload", "throwaway.service")
    assert owed() == {UNIT_A: "yes", UNIT_B: "yes"}  # every unit, not the one that was linked
    systemctl("daemon-reload")
    assert owed() == {UNIT_A: "no", UNIT_B: "no"}
    systemctl("disable", "--no-reload", "throwaway.service")
    assert owed() == {UNIT_A: "yes", UNIT_B: "yes"}  # a disable does it too
    phone_env.set_fake("reload_ineffective", True)  # a reload that loaded nothing leaves it owed
    systemctl("daemon-reload")
    assert owed() == {UNIT_A: "yes", UNIT_B: "yes"}
    phone_env.set_fake("reload_ineffective", False)
    systemctl("daemon-reload")
    assert owed() == {UNIT_A: "no", UNIT_B: "no"}
    phone_env.set_loaded_unit(UNIT_B, not_found=True)  # a unit the manager has no file for is marked like any other
    systemctl("enable", "--no-reload", "throwaway.service")
    assert owed() == {UNIT_A: "yes", UNIT_B: "yes"}


def test_the_fake_systemctl_fails_a_verb_for_one_unit_and_a_reload_from_the_nth_on(phone_env):
    def run(*words: str):
        return subprocess.run(["systemctl", "--user", *words], env=phone_env.env, capture_output=True, text=True)

    phone_env.set_fake("systemctl_fail_on", ["start", UNIT_A])
    assert run("start", UNIT_A).returncode == 1 and run("start", UNIT_B).returncode == 0  # that unit only
    assert run("enable", "--no-reload", UNIT_A).returncode == 0  # and that verb only
    phone_env.set_fake("fail_reload_from", 2)
    assert [run("daemon-reload").returncode for _ in range(3)] == [0, 1, 1]  # the first works, the second and after do not


@pytest.mark.parametrize("verb", PROBED_NOT_FOUND_VERBS)
def test_the_fake_systemctl_refuses_a_unit_it_has_not_found_as_real_systemd_255_does(phone_env, verb):
    phone_env.set_loaded_unit("orion-phone-b.service", not_found=True)
    proc = subprocess.run(
        ["systemctl", "--user", *verb.split(), "orion-phone-b.service"], env=phone_env.env, capture_output=True, text=True
    )
    status, message = PROBED_NOT_FOUND_VERBS[verb]
    assert (proc.returncode, proc.stderr, proc.stdout) == (status, message.format(unit="orion-phone-b.service"), "")
    assert "orion-phone-b.service" not in json.loads(phone_env.fake_state.read_text()).get("invocations", {})


def test_the_fake_systemctl_stops_a_unit_that_runs_though_its_file_is_gone(phone_env):
    phone_env.set_loaded_unit("orion-phone-b.service", not_found=True)
    phone_env.start_unit_by_hand("orion-phone-b.service")  # a process that outlived its unit file
    proc = subprocess.run(
        ["systemctl", "--user", "stop", "orion-phone-b.service"], env=phone_env.env, capture_output=True, text=True
    )
    assert (proc.returncode, proc.stderr) == (0, "")
    assert "orion-phone-b.service" not in json.loads(phone_env.fake_state.read_text()).get("invocations", {})


def test_a_reload_that_finds_the_unit_file_loads_the_unit_and_one_without_a_file_stays_not_found(phone_env):
    import fake_systemctl

    for unit in ("orion-phone-a.service", "orion-phone-b.service"):
        phone_env.set_loaded_unit(unit, not_found=True)
    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    units.mkdir()
    (units / "orion-phone-b.service").write_text("[Service]\nRuntimeMaxSec=7200\n")  # only b has been written
    subprocess.run(["systemctl", "--user", "daemon-reload"], env=phone_env.env, check=True)
    state = json.loads(phone_env.fake_state.read_text())
    ask = lambda unit: fake_systemctl.show(state, ["-p", "LoadState", unit]).strip()  # noqa: E731
    assert (ask("orion-phone-a.service"), ask("orion-phone-b.service")) == ("LoadState=not-found", "LoadState=loaded")


@pytest.mark.parametrize("seconds", [7200, 300, 1500, 5400, 86400, 7260], ids=lambda s: f"{s} s")
def test_what_the_fake_prints_for_a_cap_is_read_back_by_the_reader_as_that_cap(monkeypatch, seconds):
    import fake_systemctl

    state = {"loaded": {"orion-phone-b.service": {
        "cap": seconds, "reload_pending": False, "environment_text": f"ORION_PHONE_STATE={STATE_DIR}"}}}
    asked = [
        "-p", "LoadState", "-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec", "-p", "KillMode",
        "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "Restart", "-p", "NeedDaemonReload", "-p", "Environment",
        "-p", "LimitCORE", "-p", "LimitCORESoft", "orion-phone-b.service",
    ]
    assert cap_problem(monkeypatch, fake_systemctl.show(state, asked), seconds // 60)[0] is None


@pytest.mark.parametrize("seconds", [1, 90, 3600, 86400], ids=lambda s: f"{s} s")
def test_what_the_fake_prints_for_extra_time_is_read_back_by_the_reader_as_extra_time(monkeypatch, seconds):
    import fake_systemctl

    state = {"loaded": {"orion-phone-b.service": {
        "cap": 7200, "reload_pending": False, "extra": seconds, "environment_text": f"ORION_PHONE_STATE={STATE_DIR}"}}}
    asked = [
        "-p", "LoadState", "-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec", "-p", "KillMode",
        "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "Restart", "-p", "NeedDaemonReload", "-p", "Environment",
        "-p", "LimitCORE", "-p", "LimitCORESoft", "orion-phone-b.service",
    ]
    problem, _ = cap_problem(monkeypatch, fake_systemctl.show(state, asked), 120)
    assert problem is not None and f"RuntimeRandomizedExtraUSec={fake_systemctl.timespan(seconds)}" in problem, problem


# A locked phone's unit watches the leases in the state directory it was installed with (ORION_PHONE_STATE in its
# environment). A command that uses another state directory would have every boot it starts stopped at the watch's first
# look, because no lease is held there. So the unit's state directory is read with the rest, and a unit that watches
# another one is a problem. systemd prints the Environment property as space separated assignments, each in double quotes
# with backslash escapes when it holds a space, a quote, a backslash, a dollar sign or a backtick. These are the probe's
# own results (a transient unit given each of these values, on systemd 255).

BS = chr(92)  # a backslash
HAIRY = "/data/my phones/50%/a%hb/back" + BS + "slash/state"  # a space, a % (and a %h), a backslash
PROBED_ENVIRONMENT = [  # (the assignment the unit carries, as systemctl printed it)
    ("P=plain", "P=plain"),
    ("S=a b", '"S=a b"'),
    ("C=100%", "C=100%"),
    ("D=%%", "D=%%"),
    ("X=50%/a%hb", "X=50%/a%hb"),
    ("B=back" + BS + "slash", '"B=back' + BS + BS + 'slash"'),
    ("BB=two" + BS + BS + "back", '"BB=two' + BS * 4 + 'back"'),
    ('Q=say "hi"', '"Q=say ' + BS + '"hi' + BS + '""'),
    ("A=it's", '"A=it' + "'" + 's"'),
    ("V=$HOME and ${X}", '"V=' + BS + "$HOME and " + BS + '${X}"'),
    ("T=a`b`", '"T=a' + BS + "`b" + BS + '`"'),
    ("E=a=b", "E=a=b"),
    ("Z=", "Z="),
    ("U=caf" + chr(0xE9), "U=caf" + chr(0xE9)),
    ("L= lead and trail ", '"L= lead and trail "'),
    ("H=" + HAIRY, '"H=/data/my phones/50%/a%hb/back' + BS + BS + 'slash/state"'),
    ("TAB=a" + chr(9) + "b", '"TAB=a' + BS + 't' + 'b"'),
    # quoted for these too (probed one character at a time): ( and * as the review found, and the rest of the set
    ("P=a(b)", '"P=a(b)"'),
    ("S=a*b", '"S=a*b"'),
    ("AMP=a&b", '"AMP=a&b"'),
    ("PIPE=a|b", '"PIPE=a|b"'),
    ("SEMI=a;b", '"SEMI=a;b"'),
    ("LT=a<b", '"LT=a<b"'),
    ("GT=a>b", '"GT=a>b"'),
    ("QM=a?b", '"QM=a?b"'),
    ("LB=a[b", '"LB=a[b"'),
    ("BANG=a!b", '"BANG=a!b"'),
    ("DEL=a" + chr(127) + "b", '"DEL=a' + BS + '177b"'),
    ("BEL=a" + chr(7) + "b", '"BEL=a' + BS + 'ab"'),
    ("ESC=a" + chr(27) + "b", '"ESC=a' + BS + '033b"'),
    ("FF=a" + chr(12) + "b", '"FF=a' + BS + 'fb"'),
    # and bare for these (probed), however odd: a percent sign, # ] ^ { } ~ +, and what is not ASCII, line and paragraph
    # separators among it, which systemd prints as they are
    ("HASH=a#b", "HASH=a#b"),
    ("RB=a]b", "RB=a]b"),
    ("CARET=a^b", "CARET=a^b"),
    ("BRACES=a{b}", "BRACES=a{b}"),
    ("TILDE=a~b", "TILDE=a~b"),
    ("PLUS=a+b", "PLUS=a+b"),
    ("NEL=a" + chr(0x85) + "b", "NEL=a" + chr(0x85) + "b"),
    ("LS=a" + chr(0x2028) + "b", "LS=a" + chr(0x2028) + "b"),
    ("PS=a" + chr(0x2029) + "b", "PS=a" + chr(0x2029) + "b"),
    ("NBSP=a" + chr(0xA0) + "b", "NBSP=a" + chr(0xA0) + "b"),
]


def test_the_fake_systemctl_prints_an_environment_as_real_systemd_255_does():
    import fake_systemctl

    assert [(raw, fake_systemctl.quote_assignment(raw)) for raw, _ in PROBED_ENVIRONMENT] == PROBED_ENVIRONMENT


def test_the_whole_probed_line_is_read_back_into_the_assignments_it_came_from():
    line = " ".join(printed for _, printed in PROBED_ENVIRONMENT)
    assert services._parse_environment(line) == dict(raw.split("=", 1) for raw, _ in PROBED_ENVIRONMENT)


@pytest.mark.parametrize("raw, printed", PROBED_ENVIRONMENT, ids=[raw.split("=", 1)[0] for raw, _ in PROBED_ENVIRONMENT])
def test_each_probed_assignment_is_read_back_by_itself(raw, printed):
    name, value = raw.split("=", 1)
    assert services._parse_environment(printed) == {name: value}


def test_an_empty_environment_has_no_variables_and_the_last_of_a_repeated_one_wins():
    assert services._parse_environment("") == {}
    assert services._parse_environment("A=1 B=2 A=3") == {"A": "3", "B": "2"}  # systemd merges these itself; so does this


UNPARSABLE_ENVIRONMENTS = {
    "a quote that is never closed": '"A=a b',
    "a quote in the middle of a bare word": 'A="a b"',
    "text right after a closing quote": '"A=a b"c',
    "a backslash that ends the line": '"A=a' + BS,
    "an escape systemd does not print": '"A=a' + BS + 'qb"',
    "a backslash in a bare word": "A=a" + BS + "b",
    "an octal escape past ASCII": '"A=a' + BS + '377b"',
    "a tab where a space should be": "A=1" + chr(9) + "B=2",
    "a space before the first assignment": " A=1",
    "a space after the last assignment": "A=1 ",
    "two spaces between assignments": "A=1  B=2",
    "only a space": " ",
    "a space after a quoted assignment": '"A=a b" ',
    "a carriage return after the last assignment": "A=1" + chr(13),
}


@pytest.mark.parametrize("text", UNPARSABLE_ENVIRONMENTS.values(), ids=UNPARSABLE_ENVIRONMENTS)
def test_an_environment_that_is_not_in_the_format_systemd_prints_is_not_guessed_at(text):
    assert services._parse_environment(text) is None


@pytest.mark.parametrize(
    "named",
    [
        f"{STATE_DIR}",
        f"{STATE_DIR}/",
        "/state/./x/../orion",
        "/state//orion",
    ],
    ids=["the same directory", "a trailing slash", "dots in the path", "a doubled slash"],
)
def test_a_unit_that_watches_this_commands_state_directory_passes_however_it_spells_it(monkeypatch, named):
    problem, _ = cap_problem(monkeypatch, show_output(environment=f"ORION_PHONE_STATE={named}"))
    assert problem is None, problem


def test_a_unit_carrying_other_variables_too_passes_and_the_last_state_directory_counts(monkeypatch):
    environment = f"ORION_PHONE_CONFIG=/c/config.toml ORION_PHONE_STATE=/wrong ANDROID_AVD_HOME=/avd ORION_PHONE_STATE={STATE_DIR}"
    assert cap_problem(monkeypatch, show_output(environment=environment))[0] is None
    reversed_ = f"ORION_PHONE_STATE={STATE_DIR} ORION_PHONE_STATE=/wrong"
    problem, _ = cap_problem(monkeypatch, show_output(environment=reversed_))
    assert problem is not None and "/wrong" in problem, problem


def test_a_state_directory_with_a_space_a_percent_and_a_backslash_is_the_same_directory_as_itself(monkeypatch):
    import fake_systemctl

    printed = fake_systemctl.quote_assignment("ORION_PHONE_STATE=" + HAIRY)
    assert cap_problem(monkeypatch, show_output(environment=printed), state_dir=Path(HAIRY))[0] is None
    problem, _ = cap_problem(monkeypatch, show_output(environment=printed))  # but not this command's /state/orion
    assert problem is not None and HAIRY in problem and "/state/orion" in problem, problem


def test_two_spellings_of_one_directory_through_a_link_are_one_directory(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert cap_problem(monkeypatch, show_output(environment=f"ORION_PHONE_STATE={link}"), state_dir=real)[0] is None
    assert cap_problem(monkeypatch, show_output(environment=f"ORION_PHONE_STATE={real}"), state_dir=link)[0] is None


def test_a_unit_that_watches_another_state_directory_is_a_problem_that_names_both(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(environment="ORION_PHONE_STATE=/install-time/state"))
    assert problem is not None
    for mention in ("watches the state directory", "/install-time/state", "this command uses", "/state/orion"):
        assert mention in problem, problem


STATE_UNREADABLE = {
    "no environment line": (show_output(environment=None), "could not read Environment"),
    "an environment in a format not seen": (show_output(environment='ORION_PHONE_STATE="/a b'), "could not read Environment"),
    "an environment with no state directory": (show_output(environment="ORION_PHONE_CONFIG=/c"), "names no state directory"),
    "no environment at all": (show_output(environment=""), "names no state directory"),
    "an empty state directory": (show_output(environment="ORION_PHONE_STATE="), "names no state directory"),
    "a state directory that is not absolute": (show_output(environment="ORION_PHONE_STATE=state/orion"), "not an absolute path"),
}


@pytest.mark.parametrize("output, mention", STATE_UNREADABLE.values(), ids=STATE_UNREADABLE)
def test_an_environment_that_does_not_say_which_state_directory_is_watched_is_a_problem_never_a_pass(
    monkeypatch, output, mention
):
    problem, _ = cap_problem(monkeypatch, output)
    assert problem is not None and mention in problem, problem


def test_a_state_directory_problem_is_reported_beside_a_cap_problem_not_instead_of_it(monkeypatch):
    problem, _ = cap_problem(monkeypatch, show_output(cap="1h", environment="ORION_PHONE_STATE=/elsewhere"))
    assert problem is not None and "its session cap is 1h" in problem and "/elsewhere" in problem, problem


# systemd prints a value in a unit's environment raw when it holds U+0085, U+2028 or U+2029 (it escapes every ASCII control
# character, and these are not ones). `str.splitlines` breaks on all three, so such a value could forge a line of show
# output of its own: probed on a real unit, X=a<U+2028>RuntimeMaxUSec=2h in the environment of a unit whose real line is
# RuntimeMaxUSec=infinity made the cap read as 2h. Show output is split on newlines only, and a property printed twice,
# which no honest output does, is refused.

SEPARATORS = {
    "a line separator": chr(0x2028), "a paragraph separator": chr(0x2029), "a next line character": chr(0x85),
}


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_value_that_holds_a_line_separator_cannot_forge_a_line_of_show_output(monkeypatch, separator):
    forged = f"ORION_PHONE_STATE={STATE_DIR} X=a{separator}RuntimeMaxUSec=2h"
    problem, _ = cap_problem(monkeypatch, show_output(cap="infinity", environment=forged))  # the unit really has no cap
    assert problem is not None and "no session cap" in problem, problem


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_line_separator_in_an_environment_value_is_part_of_that_value_and_nothing_else(monkeypatch, separator):
    environment = f"ORION_PHONE_STATE={STATE_DIR} X=a{separator}b"
    assert cap_problem(monkeypatch, show_output(environment=environment))[0] is None  # the unit is right, and is read so


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_separator_in_the_state_directory_value_is_shown_escaped_not_as_a_line_break(monkeypatch, separator):
    problem, _ = cap_problem(monkeypatch, show_output(environment=f"ORION_PHONE_STATE=/state{separator}elsewhere"))
    assert problem is not None and "watches the state directory" in problem and "\n" not in problem, problem
    assert separator not in problem, problem


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_state_directory_value_that_ends_in_a_separator_is_not_the_state_directory(monkeypatch, separator):
    # systemd prints the separator raw at the end of the line; trimming "whitespace" off the line would take it off
    problem, _ = cap_problem(monkeypatch, show_output(environment=f"ORION_PHONE_STATE={STATE_DIR}{separator}"))
    assert problem is not None and "watches the state directory" in problem, problem


DUPLICATED = {
    "the cap": "RuntimeMaxUSec=infinity" + NL + "RuntimeMaxUSec=2h" + NL,
    "the environment": "Environment=ORION_PHONE_STATE=/state/orion" + NL + "Environment=ORION_PHONE_STATE=/state/orion" + NL,
    "the reload state": "NeedDaemonReload=no" + NL + "NeedDaemonReload=no" + NL,
    "a property that was not asked for": show_output() + "Id=a" + NL + "Id=b" + NL,
}


@pytest.mark.parametrize("output", DUPLICATED.values(), ids=DUPLICATED)
def test_a_property_printed_twice_is_refused_since_one_of_them_may_be_forged(monkeypatch, output):
    with pytest.raises(PhoneError) as caught:
        cap_problem(monkeypatch, output)
    assert caught.value.code is Code.USAGE and "more than once" in caught.value.message, caught.value.message
    assert caught.value.data == {"unit": "orion-phone-b.service"}


def test_invocation_id_refuses_a_property_printed_twice(monkeypatch):
    output = "ActiveState=active" + NL + "InvocationID=aaa" + NL + "InvocationID=bbb" + NL
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: completed(command, 0, output))
    with pytest.raises(PhoneError) as caught:
        services.invocation_id("orion-phone-b.service")
    assert caught.value.code is Code.USAGE and "more than once" in caught.value.message


def test_install_units_refuses_a_read_that_prints_a_property_twice_and_writes_nothing(phone_env, in_process, monkeypatch):
    real = services._systemctl

    def twice(*args):
        out = real(*args)
        return out + out if args[0] == "show" else out

    monkeypatch.setattr(services, "_systemctl", twice)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and "more than once" in caught.value.message
    assert not list(units_of(phone_env).glob("*.service")) and ["--user", "daemon-reload"] not in phone_env.calls("systemctl")


# The runbook's Rollback block is run top to bottom by an operator, so it is run here as written, in a HOME of its own with
# a fake systemctl and a fake avdmanager. avdmanager finds the phones only in the AVD home the units name, so the block
# must read it from a unit before it removes the units; a unit writes % and backslash doubled, which the block undoes.

RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "runbooks" / "phone-services.md"
HAIRY_AVD_HOME = "/data/my phones/50%/a%hb/back" + BS + "slash/avd"  # a space, a % (as a specifier too), a backslash


def rollback_block() -> str:
    section = RUNBOOK.read_text().split("\n## Rollback\n", 1)[1]
    return re.search(r"```bash\n(.*?)\n```", section, re.DOTALL).group(1)


def run_rollback(
    tmp_path: Path, avd: Path | None, missing: tuple[str, ...] = (), unnamed_in: tuple[str, ...] = (),
    exported: str | None = None, flags: tuple[str, ...] = (),
) -> dict:
    """Run the runbook's Rollback block in a HOME of its own, whose two units name `avd` (for None, they are as units were
    before they named one; `unnamed_in` lists the phones whose unit does not name it although `avd` is given), and the three
    units' files are all there except those named in `missing` (removed by hand, or never installed: their units may still be
    loaded and running). The open phone's and the server's units have the login link that `enable` made, which a disable
    removes when the unit's file is there. `exported` is the ANDROID_AVD_HOME the operator's shell has exported (none by
    default) and `flags` are options for bash (`-e`). Returns what the fakes saw and what is left."""
    block = rollback_block()
    for operand in re.findall(r"\brm\s+(?:-\w+\s+)?(\S+)", block):
        assert operand.startswith("~/"), f"the block removes {operand}: it would not stay inside the test's HOME"
    home = tmp_path / "home"
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    cfg = config.parse(
        {"phones": {"a": {"tier": "open", "console_port": 5554}, "b": {"tier": "locked", "console_port": 5556}}},
        Path("/etc/orion-phone/config.toml"),
        Path("/var/lib/orion-phone"),
    )
    for name in ("a", "b"):
        unit = services.render_unit(cfg, cfg.phone(name), "/usr/bin/python3", None, avd or Path("/unused"))
        if avd is None or name in unnamed_in:
            unit = "\n".join(line for line in unit.splitlines() if "ANDROID_AVD_HOME" not in line) + NL
        (units / f"orion-phone-{name}.service").write_text(unit)
    (units / services.ADB_SERVER_UNIT).write_text(services.render_adb_server_unit(cfg))  # the third unit
    wants = units / "default.target.wants"  # the login links `enable` made: the open phone's, and the server's
    wants.mkdir()
    for linked in ("orion-phone-a.service", services.ADB_SERVER_UNIT):
        (wants / linked).symlink_to(units / linked)
    for gone in missing:
        (units / gone).unlink()  # its link, if it has one, dangles now
    state = home / ".local" / "state" / "orion-phone"
    state.mkdir(parents=True)
    (state / "audit.txt").write_text("a record" + NL)
    tools = home / "Android" / "Sdk" / "cmdline-tools" / "latest" / "bin"
    tools.mkdir(parents=True)
    avdmanager = tools / "avdmanager"
    avdmanager.write_text(  # printf, not echo: a shell's echo may fold a doubled backslash, the very thing under test
        "#!/bin/sh" + NL
        + 'printf "home=%s units=%s args=%s\\n" "${ANDROID_AVD_HOME-unset}" '
        + f'"$(ls {units}/orion-phone-*.service 2>/dev/null | wc -l)" "$*" >> "{home}/avdmanager.log"' + NL
    )
    avdmanager.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(  # as systemd does a disable that names a missing unit file: it fails whole, and disables none
        "#!/bin/sh" + NL + f'printf "%s\\n" "$*" >> "{home}/systemctl.log"' + NL
        + 'if [ "$2" = stop ]; then' + NL  # a loaded unit stops even when its file is gone: a running unit outlives its file
        + f'  echo "$3" >> "{home}/stopped.log"' + NL
        + f'  printf "%s: %s\\n" "$3" "$(ls {units} | tr "\\n" " ")" >> "{home}/at-stop.log"' + NL  # the files there then
        + "fi" + NL
        + 'if [ "$2" = disable ]; then' + NL
        + '  for unit in "$@"; do' + NL
        + '    case "$unit" in *.service)' + NL
        + f'      [ -e "{units}/$unit" ] || {{ echo "Failed to disable unit: Unit file $unit does not exist." >&2; '
        + "exit 1; };;" + NL
        + "    esac" + NL
        + "  done" + NL
        + '  for unit in "$@"; do case "$unit" in *.service) echo "$unit" >> ' + f'"{home}/disabled.log"; '
        + f'rm -f "{wants}/$unit";; esac; done' + NL  # a disable that works removes the unit's login link
        + "fi" + NL + "exit 0" + NL
    )
    systemctl.chmod(0o755)
    shell_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)}
    if exported is not None:
        shell_env["ANDROID_AVD_HOME"] = exported
    proc = subprocess.run(["bash", *flags, "-c", block], env=shell_env, capture_output=True, text=True, timeout=60)

    def lines(name: str) -> list[str]:
        log = home / name
        return log.read_text().splitlines() if log.exists() else []

    return {
        "returncode": proc.returncode, "stderr": proc.stderr, "avdmanager": lines("avdmanager.log"),
        "systemctl": lines("systemctl.log"), "units_left": sorted(p.name for p in units.glob("*.service")), "state_left": state.exists(),
        "units_at_stop": lines("at-stop.log"), "stopped": lines("stopped.log"), "disabled": lines("disabled.log"),
        "links_left": sorted(p.name for p in wants.iterdir()),  # a link whose unit file is gone counts: iterdir lists it
    }


def test_the_runbooks_rollback_reads_the_avd_home_from_a_unit_before_it_removes_the_units(tmp_path):
    saw = run_rollback(tmp_path, Path(HAIRY_AVD_HOME))
    assert saw["returncode"] == 0, saw["stderr"]
    assert saw["avdmanager"] == [  # all three units are still there when the phones are deleted, and the home is exactly theirs
        f"home={HAIRY_AVD_HOME} units=3 args=delete avd -n orion-phone-a",
        f"home={HAIRY_AVD_HOME} units=3 args=delete avd -n orion-phone-b",
    ]
    assert saw["systemctl"] == [  # every unit, the server's too, is stopped, each on a line of its own, and only then disabled
        "--user stop orion-phone-a.service", "--user stop orion-phone-b.service", f"--user stop {services.ADB_SERVER_UNIT}",
        "--user disable orion-phone-a.service", "--user disable orion-phone-b.service",
        f"--user disable {services.ADB_SERVER_UNIT}",
        "--user daemon-reload",
    ]  # a disable reloads the manager, so every unit is stopped before the first one
    assert saw["links_left"] == []  # no login link is left
    assert saw["stopped"] == ["orion-phone-a.service", "orion-phone-b.service", services.ADB_SERVER_UNIT]
    assert saw["disabled"] == ["orion-phone-a.service", "orion-phone-b.service", services.ADB_SERVER_UNIT]
    assert all(  # each is stopped while the unit files are there: a running unit outlives its file, so it is stopped first
        {"orion-phone-a.service", "orion-phone-b.service", services.ADB_SERVER_UNIT} <= set(line.split(": ")[1].split())
        for line in saw["units_at_stop"]
    ) and len(saw["units_at_stop"]) == 3
    assert saw["units_left"] == [] and saw["state_left"] is False  # and then the units (the server's too) and the state go


UNITS_GONE = {
    "the server's": (services.ADB_SERVER_UNIT,),
    "phone a's": ("orion-phone-a.service",),
    "phone b's": ("orion-phone-b.service",),
    "both phones'": ("orion-phone-a.service", "orion-phone-b.service"),
    "all three": ("orion-phone-a.service", "orion-phone-b.service", services.ADB_SERVER_UNIT),
}


@pytest.mark.parametrize("gone", UNITS_GONE.values(), ids=UNITS_GONE)
def test_the_runbooks_rollback_stops_and_disables_every_other_unit_when_a_units_file_is_already_gone(tmp_path, gone):
    # A disable that names a unit file that is not there fails: it removes no link, and a `--now` with it never stops anything,
    # nor does a disable that has several units on its line do anything for the others. Any unit's file may be gone (removed by
    # hand, which the runbook allows, or never installed after an upgrade) while the unit runs, so every unit has a stop of its
    # own, which reaches a loaded unit without its file, and a disable of its own: the others are still stopped and disabled,
    # and the block goes on.
    saw = run_rollback(tmp_path, Path(HAIRY_AVD_HOME), missing=gone)
    every = ["orion-phone-a.service", "orion-phone-b.service", services.ADB_SERVER_UNIT]
    assert saw["stopped"] == every, saw["stderr"]  # all of them, the ones with no file too
    assert saw["disabled"] == [unit for unit in every if unit not in gone], saw["stderr"]
    for unit in gone:
        assert f"{unit} does not exist" in saw["stderr"]  # that disable failed, and said so
    assert saw["links_left"] == [], saw  # and its login link, which a failed disable leaves, is removed by its path
    assert saw["units_left"] == [] and saw["state_left"] is False  # and the rest of the block went on
    assert saw["systemctl"][-1] == "--user daemon-reload", saw  # to the reload, which the removal of the units calls for
    assert len(saw["avdmanager"]) == (0 if {"orion-phone-a.service", "orion-phone-b.service"} <= set(gone) else 2)  # see below


def test_the_runbooks_rollback_leaves_the_tools_own_home_alone_for_a_unit_that_names_none(tmp_path):
    saw = run_rollback(tmp_path, None)  # a unit written before units named the AVD home
    assert saw["returncode"] == 0, saw["stderr"]
    assert [line.split(" units=")[0] for line in saw["avdmanager"]] == ["home=unset"] * 2  # an empty value would mean the cwd
    assert saw["units_left"] == [] and saw["state_left"] is False


def test_the_runbooks_rollback_reloads_the_manager_when_no_unit_file_is_left_to_remove(tmp_path):
    # The unit files may all be gone already (removed by hand, which the runbook allows). The glob then matches nothing, and an
    # `rm` without -f fails on the pattern itself, so the `&&` after it never reached the reload that tells the manager.
    every = ("orion-phone-a.service", "orion-phone-b.service", services.ADB_SERVER_UNIT)
    saw = run_rollback(tmp_path, Path(HAIRY_AVD_HOME), missing=every)
    assert saw["units_left"] == [] and saw["state_left"] is False
    assert saw["systemctl"][-1] == "--user daemon-reload", saw["systemctl"]


def test_the_runbooks_rollback_says_not_to_run_it_under_set_e_and_that_is_why(tmp_path):
    assert "without `set -e`" in rollback_block()  # in the block itself, where an operator copies it from
    # Under `bash -e` the block ends at the first line that fails (here the disable of the unit whose file is gone) and leaves
    # the phones, the units and the state: every line must run, even after an earlier one has failed.
    saw = run_rollback(tmp_path, Path(HAIRY_AVD_HOME), missing=("orion-phone-a.service",), flags=("-e",))
    assert saw["returncode"] != 0 and saw["avdmanager"] == [] and saw["state_left"] is True and saw["units_left"], saw


# The phones are deleted from the AVD home their units name, and the block reads it from the FIRST phone unit file that names
# one, trying each in turn: the unit file of the first phone may be gone (removed by hand, which the runbook allows). Units
# that all exist and name none are from before units named it: the block exports nothing then, so avdmanager follows the
# operator's own environment (an exported ANDROID_AVD_HOME, else the tools' default), and a home a unit names wins over an
# export. If a unit file is gone and none that is left names the home, it is not known: nothing is deleted on a guess (not the
# tools' default, not what the shell exports), and the block says so and goes on to remove the units and the state.

SHELL_HOME = "/shell/guess"  # a home the operator's shell exports, which no unit names


def test_the_runbooks_rollback_deletes_from_the_home_the_shell_exports_when_no_unit_names_one(tmp_path):
    saw = run_rollback(tmp_path, None, exported=SHELL_HOME)  # units from before they named one, and an export
    assert saw["returncode"] == 0, saw["stderr"]
    assert [line.split(" units=")[0] for line in saw["avdmanager"]] == [f"home={SHELL_HOME}"] * 2, saw["avdmanager"]
    assert saw["units_left"] == [] and saw["state_left"] is False


def test_the_runbooks_rollback_prefers_the_home_a_unit_names_to_the_one_the_shell_exports(tmp_path):
    saw = run_rollback(tmp_path, Path(HAIRY_AVD_HOME), exported=SHELL_HOME)
    assert [line.split(" units=")[0] for line in saw["avdmanager"]] == [f"home={HAIRY_AVD_HOME}"] * 2, saw["avdmanager"]


def test_the_runbook_does_not_say_the_tools_default_is_right_for_units_that_name_no_home():
    # It is not what the block does: with an export in the shell, avdmanager looks there (see the tests above).
    section = RUNBOOK.read_text().split("\n## Rollback\n", 1)[1]
    assert "the tools' default is right" not in section
    assert "your own environment" in section  # what the units that name none are left to, said once, in the prose and the block



A_AND_B = ("orion-phone-a.service", "orion-phone-b.service")
HOME_FROM = {
    "phone a's unit": ((), ()),
    "phone b's unit, since phone a's is gone": (("orion-phone-a.service",), ()),
    "phone b's unit, since phone a's names none": ((), ("a",)),
    "phone a's unit, even though phone b's names none": ((), ("b",)),
}


@pytest.mark.parametrize("gone, unnamed_in", HOME_FROM.values(), ids=HOME_FROM)
def test_the_runbooks_rollback_reads_the_avd_home_from_the_first_phone_unit_that_names_one(tmp_path, gone, unnamed_in):
    saw = run_rollback(tmp_path, Path(HAIRY_AVD_HOME), missing=gone, unnamed_in=unnamed_in)
    assert saw["returncode"] == 0, saw["stderr"]
    assert [line.split(" units=")[0] for line in saw["avdmanager"]] == [f"home={HAIRY_AVD_HOME}"] * 2, saw  # the home used
    deleted = [line.split(" args=")[1] for line in saw["avdmanager"]]
    assert deleted == ["delete avd -n orion-phone-a", "delete avd -n orion-phone-b"]
    assert saw["units_left"] == [] and saw["state_left"] is False


HOME_NOT_KNOWN = {
    "phone a's unit is gone and phone b's names none": (("orion-phone-a.service",), None),
    "phone b's unit is gone and phone a's names none": (("orion-phone-b.service",), None),
    "both phones' units are gone": (A_AND_B, Path(HAIRY_AVD_HOME)),
}


@pytest.mark.parametrize("gone, avd", HOME_NOT_KNOWN.values(), ids=HOME_NOT_KNOWN)
def test_the_runbooks_rollback_deletes_no_phone_when_no_unit_that_is_left_names_the_avd_home(tmp_path, gone, avd):
    saw = run_rollback(tmp_path, avd, missing=gone)
    assert saw["avdmanager"] == [], saw  # never on a guess: neither the tools' default home nor one the shell exports
    assert "the AVD home is not known" in saw["stderr"] and "no phone was deleted" in saw["stderr"], saw["stderr"]
    assert saw["units_left"] == [] and saw["state_left"] is False  # and the rest of the block went on
    assert saw["stopped"] == ["orion-phone-a.service", "orion-phone-b.service", services.ADB_SERVER_UNIT]


# install-units never leaves a locked phone running without its session cap. A phone switched to locked keeps its old open
# unit, which has none, and the manager runs what it has loaded. So before it writes any unit, install-units stops each
# locked phone whose unit may be running and whose loaded definition does not carry exactly the configured cap (no cap,
# another cap, or one that cannot be read), and reports the phones it stopped. A pending reload alone does not stop a
# session there: what the process runs under is what is loaded. But the reload install-units then makes loads whatever is
# pending (a drop-in's RuntimeMaxSec or RuntimeRandomizedExtraSec included) and re-arms a running unit's runtime limit from
# it, so after the reload every locked phone still running is read again, and stopped the same way when its loaded
# definition is not exactly the configured cap with no extra run time and no reload pending. A capped session with
# nothing pending, an inactive unit, and an open phone are left alone, and a phone that cannot be stopped refuses the
# install.

UNIT_A, UNIT_B = "orion-phone-a.service", "orion-phone-b.service"
STATE_AND_CAP_B = [  # what install-units asks of a locked phone's unit: is it there, is it running, how long may a session
    # last, does a stop of the unit end it (in time), and does the manager start it again by itself
    "--user", "show", "-p", "ActiveState", "-p", "LoadState", "-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec",
    "-p", "KillMode", "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "Restart", "-p", "LimitCORE", "-p", "LimitCORESoft",
    UNIT_B,
]


DAEMON_RELOAD = ["--user", "daemon-reload"]
STATE_CAP_EXTRA_RELOAD_B = [  # the read after the reload: the reload state too, which must then say no
    "--user", "show", "-p", "ActiveState", "-p", "LoadState", "-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec",
    "-p", "KillMode", "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "Restart", "-p", "LimitCORE", "-p", "LimitCORESoft",
    "-p", "NeedDaemonReload", UNIT_B,
]


def stops(env) -> list[str]:
    """The units that were stopped through systemctl, in order."""
    return [call[-1] for call in env.calls("systemctl") if call[:2] == ["--user", "stop"]]


def active_units(env) -> set[str]:
    return set(json.loads(env.fake_state.read_text()).get("invocations", {}))


def set_tiers(env, **tiers: str) -> None:
    """Rewrite the tier of these phones in the test config."""
    path = Path(env.env["ORION_PHONE_CONFIG"])
    text = path.read_text()
    for name, tier in tiers.items():
        text = re.sub(rf'(\[phones\.{name}\]\ntier = )"[a-z]+"', rf'\1"{tier}"', text)
    path.write_text(text)


def switch_to_locked_while_running(env) -> None:
    """b was an open phone, installed as one and running; the config now says locked and nothing was installed again."""
    set_tiers(env, b="open")
    assert env.run("install-units")[0] == 0  # b's unit has no cap, and the manager loads it
    env.start_unit_by_hand(UNIT_B)  # it runs from that unit
    set_tiers(env, b="locked")


def test_install_units_stops_a_phone_switched_to_locked_that_still_runs_from_its_old_open_unit(phone_env):
    switch_to_locked_while_running(phone_env)
    code, out = phone_env.run("install-units")
    assert code == 0, out
    assert out["data"]["stopped"] == ["b"], out
    assert "not confirmed to enforce their session cap and core limits: b" in out["message"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)
    assert "RuntimeMaxSec=7200" in unit_text(phone_env, "b")  # and the install went on: b's unit now has its cap


def test_a_second_install_stops_nothing_more(phone_env):
    switch_to_locked_while_running(phone_env)
    assert phone_env.run("install-units")[0] == 0
    phone_env.start_unit_by_hand(UNIT_B)  # a session of the capped unit, as a lease would start it
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and "stopped" not in out["message"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B in active_units(phone_env)  # only the first install stopped anything


def test_a_capped_running_locked_session_is_left_alone(phone_env):
    phone_env.start_unit_by_hand(UNIT_B)  # the fake has b loaded with the 120 minute cap the config says
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [], out
    assert STATE_AND_CAP_B in phone_env.calls("systemctl")  # it was looked at
    assert stops(phone_env) == [] and UNIT_B in active_units(phone_env)


def test_an_inactive_locked_unit_is_untouched_even_without_its_cap(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)  # the old open unit, but nothing runs from it
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [], out
    assert STATE_AND_CAP_B in phone_env.calls("systemctl") and stops(phone_env) == []


LACKING_THE_CAP = {
    "no cap, as the old open unit has": {"cap": None},
    "a shorter cap than the config says": {"cap": 3600},
    "a longer cap than the config says": {"cap": 9000},
    "a cap that cannot be read": {"cap_text": "forever"},
    "no cap line at all": {"omit": ["RuntimeMaxUSec"]},
}


@pytest.mark.parametrize("loaded", LACKING_THE_CAP.values(), ids=LACKING_THE_CAP)
def test_a_running_locked_phone_whose_loaded_unit_lacks_the_configured_cap_is_stopped(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


# A process keeps the core limits it started with: a reload does not change them. So the look before the writes, which reads
# the unit the running phone was started from, stops one whose limits would let its emulator leave a dump of its memory, as
# a phone running from a unit written before LimitCORE does. The look after the reload stops one too when the reload loaded
# such limits (a drop-in): the next start of that unit would let the phone dump, and a lease refuses it until it is fixed.
LETTING_IT_DUMP = {
    "the manager's defaults, as a unit written before LimitCORE": {"core": "infinity", "core_soft": "0"},
    "no limit at all": {"core": "infinity", "core_soft": "infinity"},
    "limits that cannot be read": {"omit": ["LimitCORE", "LimitCORESoft"]},
}


@pytest.mark.parametrize("loaded", LETTING_IT_DUMP.values(), ids=LETTING_IT_DUMP)
def test_a_running_locked_phone_whose_unit_lets_it_dump_its_memory_is_stopped(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)
    assert "LimitCORE=1" in unit_text(phone_env, "b")  # and the install went on: b's unit now keeps the phone out of dumps


def test_a_drop_in_that_lifts_the_core_limit_stops_the_running_phone_once_the_reload_loads_it(phone_env):
    phone_env.start_unit_by_hand(UNIT_B)  # a session of the unit as written, with LimitCORE=1
    phone_env.set_loaded_unit(UNIT_B, pending={"core": "infinity", "core_soft": "infinity"})
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


# A session that carries exactly the right cap can still outlast it: RuntimeRandomizedExtraSec adds a random extra span
# to the cap, and a drop-in can set it without touching the unit file install-units writes. Only an extra time of 0 is
# the configured cap; anything else, or an answer that is not there or cannot be read, is stopped like a missing cap.

EXTRA_RUN_TIME = {
    "a day of extra time": {"extra": 86400},
    "a second of extra time": {"extra": 1},
    "unlimited extra time": {"extra_text": "infinity"},
    "extra time that cannot be read": {"extra_text": "soon"},
    "zero spelled with a unit": {"extra_text": "0s"},
    "no extra-time property at all, as before systemd 250": {"omit": ["RuntimeRandomizedExtraUSec"]},
}


@pytest.mark.parametrize("loaded", EXTRA_RUN_TIME.values(), ids=EXTRA_RUN_TIME)
def test_a_running_locked_phone_that_may_outlast_its_cap_is_stopped(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)  # the cap loaded is exactly the configured one
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


def test_a_running_locked_phone_with_the_right_cap_and_no_extra_time_is_left_alone(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=7200, extra=0)
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert UNIT_B in active_units(phone_env)


def test_a_unit_that_is_not_running_is_not_stopped_for_extra_time(phone_env):
    phone_env.set_loaded_unit(UNIT_B, extra=86400)  # nothing runs from it
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out


def test_an_open_phone_with_extra_time_is_left_alone(phone_env):
    phone_env.set_loaded_unit(UNIT_A, extra=86400)  # open phones have no cap to outlast
    phone_env.start_unit_by_hand(UNIT_A)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out


# The cap is only worth what a stop of the unit does at it. A running locked phone whose unit would not be ended by a stop
# (KillMode other than control-group) or would not be given the time the watch's own stop takes (a stop time under two
# graces, one that is never forced, or any of these that cannot be read) is stopped like one without a cap, before the
# writes; and a drop-in that sets one is caught by the read after the reload.

UNSAFE_STOPS = {
    "KillMode=process": {"kill_mode": "process"},
    "KillMode=none": {"kill_mode": "none"},
    "KillMode=mixed": {"kill_mode": "mixed"},
    "a kill mode that cannot be read": {"kill_mode_text": ""},
    "no KillMode property": {"omit": ["KillMode"]},
    "a stop time of 30 seconds": {"stop_timeout": 30},
    "a stop time of 59 seconds": {"stop_timeout": 59},
    "a stop time that is never forced": {"stop_timeout_text": "infinity"},
    "a stop time that cannot be read": {"stop_timeout_text": "soon"},
    "no TimeoutStopUSec property": {"omit": ["TimeoutStopUSec"]},
    "SendSIGKILL=no": {"send_sigkill": "no"},
    "a SendSIGKILL that cannot be read": {"send_sigkill_text": ""},
    "no SendSIGKILL property": {"omit": ["SendSIGKILL"]},
}


@pytest.mark.parametrize("loaded", UNSAFE_STOPS.values(), ids=UNSAFE_STOPS)
def test_a_running_locked_phone_whose_stop_would_not_end_it_in_time_is_stopped(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)  # the cap loaded is exactly the configured one
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


@pytest.mark.parametrize(
    "loaded",
    [{"stop_timeout": 60}, {"stop_timeout": 120}, {"kill_mode": "control-group"}, {"send_sigkill": "yes"}],
    ids=["two graces", "two minutes", "control-group", "SendSIGKILL=yes"],
)
def test_a_running_locked_phone_that_a_stop_would_end_in_time_is_left_alone(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert UNIT_B in active_units(phone_env)


def test_a_unit_that_is_not_running_is_not_stopped_for_its_kill_mode_or_its_stop_time(phone_env):
    phone_env.set_loaded_unit(UNIT_B, kill_mode="none", stop_timeout=5, send_sigkill="no")  # nothing runs from it
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out


def test_an_open_phone_is_left_alone_whatever_its_kill_mode_and_stop_time(phone_env):
    phone_env.set_loaded_unit(UNIT_A, kill_mode="none", stop_timeout=5, send_sigkill="no")  # no cap for a stop to enforce
    phone_env.start_unit_by_hand(UNIT_A)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out


@pytest.mark.parametrize("state", ["active", "activating", "reloading", "deactivating", ""], ids=lambda s: s or "unreadable")
def test_a_unit_that_may_be_running_is_treated_as_running(phone_env, state):
    phone_env.set_loaded_unit(UNIT_B, cap=None, state_text=state)  # not known to be off: it is not left to run uncapped
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_a_unit_with_no_process_behind_it_is_not_stopped(phone_env, state):
    phone_env.set_loaded_unit(UNIT_B, cap=None, state_text=state)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out


def test_a_pending_reload_that_changes_nothing_about_the_cap_does_not_stop_a_capped_session(phone_env):
    phone_env.set_loaded_unit(UNIT_B, reload_pending=True)  # the unit file changed since it was loaded, and nothing else
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert UNIT_B in active_units(phone_env)  # the reload loaded the unit just written, which carries the configured cap
    calls = phone_env.calls("systemctl")
    assert calls.index(DAEMON_RELOAD) < calls.index(STATE_CAP_EXTRA_RELOAD_B)  # and it was read again after the reload


def test_an_open_phone_is_never_stopped_for_lacking_a_cap(phone_env):
    phone_env.start_unit_by_hand(UNIT_A)  # open phones have no cap to lack
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert all(call[-1] != UNIT_A for call in phone_env.calls("systemctl") if call[:2] == ["--user", "show"])  # not even read


# A first install: the manager has no unit for either phone. systemd answers `show` for such a unit like any other (exit 0,
# LoadState=not-found, inactive, its defaults: probed on 255), so nothing fails for want of a unit. install-units reads each
# locked phone's unit, finds that nothing runs from it (it has no file, so no process can have been started from it), writes
# the units and reloads, and the manager has them from then on. A unit that systemd has not found and that still runs (a
# process that outlived its file) is not skipped for being not found: only a unit nothing runs from is.


def first_install(env) -> None:
    """Neither unit is known to the manager yet, and no unit file is on disk."""
    for unit in (UNIT_A, UNIT_B):
        env.set_loaded_unit(unit, not_found=True)
    assert not list(units_of(env).glob("*.service"))


def load_state(env, unit: str) -> str:
    import fake_systemctl

    return fake_systemctl.show(json.loads(env.fake_state.read_text()), ["-p", "LoadState", unit]).strip()


def test_a_first_install_writes_the_units_though_systemd_has_none_yet(phone_env):
    first_install(phone_env)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [], out
    written = sorted(path.name for path in units_of(phone_env).glob("*.service"))
    assert written == sorted([services.ADB_SERVER_UNIT, UNIT_A, UNIT_B])
    assert stops(phone_env) == []  # nothing was there to stop
    calls = phone_env.calls("systemctl")
    shows = [call for call in calls if call[:2] == ["--user", "show"]]
    assert len(shows) == 2 and calls.index(shows[0]) < calls.index(DAEMON_RELOAD) < calls.index(shows[1]), calls
    assert (load_state(phone_env, UNIT_A), load_state(phone_env, UNIT_B)) == ("LoadState=loaded", "LoadState=loaded")


def test_the_install_reads_whether_the_unit_is_there_before_it_decides_that_nothing_runs(phone_env):
    first_install(phone_env)
    assert phone_env.run("install-units")[0] == 0
    asked = [call for call in phone_env.calls("systemctl") if call[:2] == ["--user", "show"]]
    assert all("LoadState" in call for call in asked), asked  # before the writes and after the reload


def test_setup_on_a_first_install_writes_the_units_and_only_then_links_and_starts_the_open_phone(phone_env):
    first_install(phone_env)
    code, out = phone_env.run("setup")
    assert code == 0, out
    written = sorted(path.name for path in units_of(phone_env).glob("*.service"))
    assert written == sorted([services.ADB_SERVER_UNIT, UNIT_A, UNIT_B])
    calls = phone_env.calls("systemctl")
    link, start = ["--user", "enable", "--no-reload", UNIT_A], ["--user", "start", UNIT_A]
    reloads = [i for i, call in enumerate(calls) if call == DAEMON_RELOAD]
    assert len(reloads) == 2 and reloads[0] < calls.index(link) < reloads[1] < calls.index(start)  # the unit exists by then
    assert not any(call[:2] in (["--user", "enable"], ["--user", "start"]) and UNIT_B in call for call in calls)  # never b


def test_a_second_install_after_the_first_changes_nothing_about_what_it_finds(phone_env):
    first_install(phone_env)
    assert phone_env.run("install-units")[0] == 0
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out


def test_a_unit_systemd_has_not_found_that_still_runs_is_stopped_like_any_running_unit_without_a_cap(phone_env):
    phone_env.set_loaded_unit(UNIT_B, not_found=True)  # no file, so no cap
    phone_env.start_unit_by_hand(UNIT_B)  # and yet it runs: a process that outlived its unit file
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


def test_each_locked_phone_is_judged_on_its_own_unit(phone_env):
    set_tiers(phone_env, a="locked")  # a and b are both locked now; a's unit is the old open one, b's is capped
    phone_env.start_unit_by_hand(UNIT_A)
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["a"], out
    assert active_units(phone_env) == {UNIT_B}  # the capped session carries on


def test_a_locked_phone_that_cannot_be_stopped_refuses_the_install(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("systemctl_fail", "stop")
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert UNIT_B in out["message"] and "systemctl --user stop" in out["message"] and "install-units" in out["message"], out
    assert out["data"] == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "stopped": []}
    assert not list(units_of(phone_env).glob("*.service"))  # nothing was written
    assert ["--user", "daemon-reload"] not in phone_env.calls("systemctl")  # and nothing was reloaded
    assert UNIT_B in active_units(phone_env)  # the failure is not hidden: the phone really is still running


# The phones install-units stops are exactly those whose unit may leave the emulator alive: an old open unit with no cap, a kill
# mode that spares the emulator, a stop that is not forced. A zero exit from `systemctl stop` is not a phone that is gone. So
# the stop is the one a lease makes (phone.py does it, and hands services the function: services cannot import phone): under
# the phone's action lock, then the emulator told to exit, then adb and both ports looked at within the gone wait. A phone
# is reported stopped only once it is shown gone. If it is not, install-units fails, says which phone and what still
# answers, says not to trust the phone until it is gone, and carries the phones it did stop. The fake's `stop_ineffective`
# is a stop that exits 0 and leaves an emulator its unit did not end.


def devices_of(env) -> dict:
    return json.loads(env.fake_state.read_text())["devices"]


def a_stop_that_exits_zero_and_leaves_the_emulator_alive(env) -> None:
    env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    env.set_fake("stop_ineffective", True)  # `systemctl stop` exits 0, and the emulator keeps running
    env.set_fake("emu_kill_fail", True)  # and the kill it is sent, to a console that answers, fails
    for serial in devices_of(env):  # every running emulator has its console open: that is where the kill is sent
        env.phone_ports(int(serial.rsplit("-", 1)[1]))


UNSAFE_UNITS = {
    "an old unit with no cap": {"cap": None},
    "a kill mode that spares the emulator": {"kill_mode": "process"},
    "a kill mode that kills nothing": {"kill_mode": "none"},
    "a stop that is not forced": {"send_sigkill": "no"},
}


@pytest.mark.parametrize("loaded", UNSAFE_UNITS.values(), ids=UNSAFE_UNITS)
def test_a_stop_that_leaves_the_emulator_alive_fails_the_install_instead_of_reporting_the_phone_stopped(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)
    phone_env.start_unit_by_hand(UNIT_B)
    a_stop_that_exits_zero_and_leaves_the_emulator_alive(phone_env)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    assert "phone b" in message and "adb still lists emulator-5556" in message and "do not trust" in message, out
    assert "install-units" in message and "adb -s emulator-5556 emu kill" in message, out
    assert "or kill it), then run install-units again)" in message and "lease it again" not in message, out  # fits an install
    assert out["data"] == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "stopped": []}  # not reported stopped: it is not gone
    assert "emulator-5556" in devices_of(phone_env)  # and it is not: the failure hides nothing
    assert not list(units_of(phone_env).glob("*.service")) and DAEMON_RELOAD not in phone_env.calls("systemctl")  # before the writes


def test_a_phone_whose_port_still_answers_after_the_stop_fails_the_install(phone_env, listening):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    listening(5556)  # the unit stops and adb lists nothing, yet something still answers where the console was
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "port 5556 still answers" in out["message"] and "do not trust" in out["message"], out
    assert out["data"]["stopped"] == []


def test_a_phone_is_reported_stopped_once_it_is_shown_gone_and_the_looks_were_made(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.phone_ports()  # a running emulator has its console open: that is where the kill is sent
    phone_env.set_fake("stop_ineffective", True)  # an old unit's stop may leave the emulator: the kill is what ends it, and
    # a stop that did end it would take the console down too, and with it the kill (sent only to a console that answers)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and "emulator-5556" not in devices_of(phone_env)
    adb = phone_env.calls("adb")
    assert ["-s", "emulator-5556", "emu", "kill"] in adb and ["devices"] in adb, adb  # the kill, and the proof that it is gone


def test_a_phone_is_not_reported_before_it_is_shown_gone_when_another_was_stopped_first(phone_env):
    set_tiers(phone_env, a="locked")
    for unit in (UNIT_A, UNIT_B):
        phone_env.set_loaded_unit(unit, cap=None)
        phone_env.start_unit_by_hand(unit)
    phone_env.remove_device("emulator-5554")  # a is as gone as a phone gets; b's emulator is not
    a_stop_that_exits_zero_and_leaves_the_emulator_alive(phone_env)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert out["data"] == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "stopped": ["a"]}, out  # a was shown gone, and b was not


# What a look says about a phone it stops holds for every cause. A phone is stopped when its unit is not confirmed to enforce
# the session cap, which has more causes than a missing cap: the cap may be there, and right, with extra run time past it, a
# stop that would not end the phone, or a reload not made yet. So the words claim no more than is known.

GOOD_UNIT = {
    "ActiveState": "active", "LoadState": "loaded", "RuntimeMaxUSec": "2h", "RuntimeRandomizedExtraUSec": "0",
    "KillMode": "control-group", "TimeoutStopUSec": "1min 30s", "SendSIGKILL": "yes", "Restart": "no", "NeedDaemonReload": "no",
}
UNIT_CAUSES = {  # what is changed from a unit that is as it should be (None leaves the property out)
    "no cap": {"RuntimeMaxUSec": "infinity"},
    "another cap": {"RuntimeMaxUSec": "1h"},
    "extra run time past the cap": {"RuntimeRandomizedExtraUSec": "1d"},
    "a kill mode that spares the emulator": {"KillMode": "process"},
    "a stop time that is too short": {"TimeoutStopUSec": "30s"},
    "a stop that is not forced": {"SendSIGKILL": "no"},
    "a unit the manager would start again": {"Restart": "always"},
    "a reload pending": {"NeedDaemonReload": "yes"},
    "a property that is missing": {"KillMode": None},
}


def the_look_could_not_show_b_gone(overrides: dict, after_reload: bool) -> str:
    values = {name: value for name, value in {**GOOD_UNIT, **overrides}.items() if value is not None}
    problem = services._unit_problem(values, 120)
    assert problem, "the unit is as it should be, so this shows nothing"
    phone, still_there = PhoneConfig("b", "locked", 5556, 120), PhoneError(Code.USAGE, "adb still lists emulator-5556")
    return services._look_failed([services._Failure(phone, problem, still_there)], [], after_reload, False).message


@pytest.mark.parametrize("after_reload", [False, True], ids=["before the writes", "after the reload"])
@pytest.mark.parametrize("overrides", UNIT_CAUSES.values(), ids=UNIT_CAUSES)
def test_a_phone_stopped_for_any_cause_is_not_said_to_run_without_the_session_cap(overrides, after_reload):
    message = the_look_could_not_show_b_gone(overrides, after_reload)
    now = "now " if after_reload else ""
    said = f"phone b is {now}running from orion-phone-b.service, which is not confirmed to enforce the session cap"
    assert said in message, message
    assert "without the session cap" not in message, message


def test_a_unit_that_could_not_be_read_is_not_said_to_run_without_its_session_cap():
    gone = services._Failure(
        PhoneConfig("b", "locked", 5556, 120), services._unit_problem({**GOOD_UNIT, "KillMode": "none"}, 120),
        PhoneError(Code.USAGE, "adb still lists emulator-5556"),
    )
    unread = services._Failure(
        PhoneConfig("a", "locked", 5554, 120), None, PhoneError(Code.PHONE_DOWN, "systemctl --user show failed: no bus")
    )
    mixed = services._look_failed([gone, unread], [], True, False).message
    assert "could not be read (systemctl --user show failed: no bus), so it is not known whether it enforces its" in mixed
    only_reads = services._look_failed([unread], [], True, False).message
    assert "so it is not known whether a running locked phone's unit enforces its session cap" in only_reads, only_reads
    assert "runs without" not in mixed and "without its session cap" not in only_reads, (mixed, only_reads)


def test_the_runbook_does_not_say_every_phone_the_install_stops_ran_without_its_cap():
    text = RUNBOOK.read_text()
    assert "a unit without the session cap" not in text and "is without its session cap" not in text


def test_a_phone_the_reload_uncaps_that_is_still_alive_after_its_stop_fails_the_install_with_the_phones_stopped_so_far(phone_env):
    phone_env.set_loaded_unit(UNIT_B, pending={"cap": None})  # a drop-in on disk that the reload loads
    phone_env.start_unit_by_hand(UNIT_B)
    a_stop_that_exits_zero_and_leaves_the_emulator_alive(phone_env)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "written" in out["message"] and "loaded" in out["message"], out  # the units are already on disk and loaded
    assert "adb still lists emulator-5556" in out["message"] and "do not trust" in out["message"], out
    assert out["data"] == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "stopped": []}
    assert "emulator-5556" in devices_of(phone_env)


def test_a_reload_that_failed_and_a_phone_that_is_still_alive_after_its_stop_keep_the_reload_error_and_say_so(phone_env):
    phone_env.set_loaded_unit(UNIT_B, pending={"cap": None})
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("reload_applies_then_fails", True)  # the manager loaded it, and the answer was lost
    a_stop_that_exits_zero_and_leaves_the_emulator_alive(phone_env)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # still the reload's
    assert "daemon-reload" in out["message"] and "could not all be checked afterwards" in out["message"], out
    assert "adb still lists emulator-5556" in out["message"] and "do not trust" in out["message"], out
    assert "stopped" not in out["data"] or out["data"]["stopped"] == []


# Every locked phone is looked at in every pass, whatever happens to the others. A phone that cannot be shown gone does not keep
# a later one from being read and stopped: the failures are collected and the install fails once, after the last phone, naming
# each phone it could not show gone and what still answers for it, and carrying the phones it did stop. Before the writes it still
# writes nothing. A unit that cannot be read does not keep a later phone from being looked at either, and a phone not shown gone
# is never hidden by a read that fails after it.

TWO_RUNNING_PHONES = {  # how a and b come to need stopping: from the old unit before the writes, or by the reload itself
    "before the writes": {"cap": None},
    # the right cap is loaded (and, as a locked unit has them, no restart and core limits of 1), and a drop-in removes the cap
    # at the reload
    "after the reload": {"cap": 7200, "restart": "no", "core": "1", "core_soft": "1", "pending": {"cap": None}},
}


def two_locked_phones_running(env, **loaded) -> None:
    set_tiers(env, a="locked")
    for unit in (UNIT_A, UNIT_B):
        env.set_loaded_unit(unit, **loaded)
        env.start_unit_by_hand(unit)


@pytest.mark.parametrize("loaded", TWO_RUNNING_PHONES.values(), ids=TWO_RUNNING_PHONES)
def test_a_phone_not_shown_gone_does_not_keep_a_later_phone_from_being_stopped(phone_env, listening, loaded):
    two_locked_phones_running(phone_env, **loaded)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    listening(5554)  # a's console port still answers after its stop; b stops as it should
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "phone a" in out["message"] and "port 5554 still answers" in out["message"] and "do not trust" in out["message"], out
    assert "phone b" not in out["message"], out  # b did not fail
    assert out["data"] == {"phone": "a", "unit": UNIT_A, "failed": ["a"], "stopped": ["b"]}, out  # b is named, as stopped
    assert "stopped and shown gone first: b" in out["message"], out
    assert stops(phone_env) == [UNIT_A, UNIT_B], "the phone after the one not shown gone was never stopped"
    assert UNIT_B not in active_units(phone_env)


def test_before_the_writes_a_phone_not_shown_gone_still_writes_nothing_but_only_after_every_phone_was_tried(phone_env, listening):
    two_locked_phones_running(phone_env, cap=None)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    listening(5554)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert stops(phone_env) == [UNIT_A, UNIT_B]  # b was stopped before the install refused
    assert not list(units_of(phone_env).glob("*.service")) and DAEMON_RELOAD not in phone_env.calls("systemctl")


@pytest.mark.parametrize("loaded", TWO_RUNNING_PHONES.values(), ids=TWO_RUNNING_PHONES)
def test_every_phone_not_shown_gone_is_named_with_what_still_answers_for_it(phone_env, loaded):
    two_locked_phones_running(phone_env, **loaded)
    a_stop_that_exits_zero_and_leaves_the_emulator_alive(phone_env)  # neither emulator leaves
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    assert "phone a" in message and "adb still lists emulator-5554" in message, out
    assert "phone b" in message and "adb still lists emulator-5556" in message, out
    assert message.count("do not trust") == 2 and message.count("stop it by hand") == 2, out  # each is told what to do
    assert "not stopped" not in message and "not_stopped" not in out["data"], out  # the stop ran, and these were not gone
    assert out["data"] == {"phone": "a", "unit": UNIT_A, "failed": ["a", "b"], "stopped": []}, out
    assert {"emulator-5554", "emulator-5556"} <= set(devices_of(phone_env))  # and neither is hidden


def test_the_look_after_a_failed_reload_does_not_stop_at_the_first_phone_it_cannot_show_gone(phone_env, listening):
    two_locked_phones_running(phone_env, cap=7200, restart="no", core="1", core_soft="1", pending={"cap": None})
    phone_env.set_fake("reload_applies_then_fails", True)  # the manager loaded it, and the answer was lost
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    listening(5554)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # still the reload's
    assert "daemon-reload" in out["message"] and "could not all be checked afterwards" in out["message"], out
    assert "phone a" in out["message"] and "port 5554 still answers" in out["message"] and "do not trust" in out["message"], out
    assert out["data"]["stopped"] == ["b"], out  # the phone after the one not shown gone was still stopped
    assert out["data"]["failed"] == ["a"], out  # and the phone still answering is named in the data, as it is in the text
    assert stops(phone_env) == [UNIT_A, UNIT_B] and UNIT_B not in active_units(phone_env)


# The error of a reload that failed stays the reload's, code and message, and carries what the look after it found, in its data as in
# its text: the phones not shown gone (`failed`), those whose stop never began (`not_stopped`), the units that could not be read
# (`unread`), and the phones stopped, those before the reload and those the look stopped.


def test_after_a_failed_reload_the_error_carries_the_phones_stopped_before_it_and_the_one_the_look_could_not_show_gone(
    phone_env, listening
):
    set_tiers(phone_env, a="locked")
    phone_env.set_loaded_unit(UNIT_A, cap=None)  # a runs uncapped now: it is stopped before anything is written
    phone_env.set_loaded_unit(UNIT_B, cap=7200, pending={"cap": None})  # b is uncapped by the reload
    for unit in (UNIT_A, UNIT_B):
        phone_env.start_unit_by_hand(unit)
    phone_env.set_fake("reload_applies_then_fails", True)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    listening(5556)  # b's console port still answers after its stop
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "daemon-reload" in out["message"] and "phone b" in out["message"] and "port 5556 still answers" in out["message"], out
    assert out["data"]["stopped"] == ["a"] and out["data"]["failed"] == ["b"], out["data"]
    assert "unread" not in out["data"] and "not_stopped" not in out["data"], out["data"]


def test_after_a_failed_reload_the_stopped_list_holds_the_phones_stopped_before_it_and_by_the_look(phone_env):
    set_tiers(phone_env, a="locked")
    phone_env.set_loaded_unit(UNIT_A, cap=None)  # stopped before the writes
    phone_env.set_loaded_unit(UNIT_B, cap=7200, pending={"cap": None})  # stopped by the look after the reload
    for unit in (UNIT_A, UNIT_B):
        phone_env.start_unit_by_hand(unit)
    phone_env.set_fake("reload_applies_then_fails", True)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert out["data"]["stopped"] == ["a", "b"], out["data"]
    assert stops(phone_env) == [UNIT_A, UNIT_B] and "failed" not in out["data"]


UNIT_C = "orion-phone-c.service"


def three_locked_phones(env) -> None:
    """a, b and c are locked phones: c is added to the config (the fake systemctl knows its unit and its emulator)."""
    set_tiers(env, a="locked")
    path = Path(env.env["ORION_PHONE_CONFIG"])
    path.write_text(path.read_text() + '\n[phones.c]\ntier = "locked"\nconsole_port = 5558\nsession_cap_minutes = 120\n')


def test_after_a_failed_reload_the_stopped_list_names_the_stop_before_it_and_the_looks_when_the_look_then_fails_on_another(
    phone_env, listening
):
    three_locked_phones(phone_env)
    phone_env.set_loaded_unit(UNIT_A, cap=None)  # a runs uncapped now: stopped before anything is written
    phone_env.set_loaded_unit(UNIT_B, cap=7200, pending={"cap": None})  # b and c are uncapped by the reload
    phone_env.set_loaded_unit(UNIT_C, cap=7200, pending={"cap": None})
    for unit in (UNIT_A, UNIT_B, UNIT_C):
        phone_env.start_unit_by_hand(unit)
    phone_env.set_fake("reload_applies_then_fails", True)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    listening(5558)  # c's console port still answers after its stop; b stopped, and was shown gone, before it
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # still the reload's
    assert out["data"]["stopped"] == ["a", "b"], out["data"]  # the stop before the reload, then the one the look made
    assert out["data"]["failed"] == ["c"], out["data"]  # and the phone the look then failed on
    assert "phone c" in out["message"] and "port 5558 still answers" in out["message"], out
    assert stops(phone_env) == [UNIT_A, UNIT_B, UNIT_C]


# An error that already carries the phones stopped is not given another list instead: the install's own list (before the
# reload, then the look's) is the order, and a phone only the error names is added after them.

STOPPED_LISTS = {
    "an error that names one more": (["b"], ["b", "later"], ["b", "later"]),
    "an error that names fewer": (["a", "b"], ["b"], ["a", "b"]),
    "an error that names none": (["a", "b"], None, ["a", "b"]),
}


@pytest.mark.parametrize("own, carried, expected", STOPPED_LISTS.values(), ids=STOPPED_LISTS)
def test_an_error_that_carries_the_stopped_list_is_merged_with_the_installs_not_given_another_instead(
    phone_env, in_process, monkeypatch, own, carried, expected
):
    two_locked_phones_running(phone_env, cap=None)  # a and b are stopped before the writes, in that order
    if own == ["b"]:
        phone_env.remove_device("emulator-5554")  # a is not running: only b is stopped
        phone_env.stop_unit(UNIT_A)
    phone_env.set_fake("systemctl_fail", "daemon-reload")
    error = PhoneError(Code.PHONE_DOWN, "daemon-reload failed (stub)", {} if carried is None else {"stopped": carried})
    monkeypatch.setattr(services, "_reload_failed", lambda cfg, stopped, failed, stop, again: error)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.data["stopped"] == expected, caught.value.data


def test_after_a_failed_reload_a_unit_that_cannot_be_read_is_in_the_data_too(phone_env, in_process, monkeypatch):
    real = services._systemctl
    reloaded = []

    def reload_fails_and_b_cannot_be_read_after_it(*args):
        if args[0] == "daemon-reload":
            reloaded.append(True)
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user daemon-reload failed: timed out", {"args": list(args)})
        if args[0] == "show" and args[-1] == UNIT_B and reloaded:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user show failed: no bus", {"unit": UNIT_B})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", reload_fails_and_b_cannot_be_read_after_it)
    phone_env.set_loaded_unit(UNIT_B, cap=7200)
    phone_env.start_unit_by_hand(UNIT_B)  # capped when the look before the writes reads it
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert error.code is Code.PHONE_DOWN and "daemon-reload" in error.message, error.message  # the reload's code and message
    assert "could not all be checked afterwards" in error.message and "no bus" in error.message, error.message
    assert error.data["unread"] == ["b"] and "failed" not in error.data, error.data


def test_after_a_failed_reload_a_phone_never_stopped_is_in_the_data_too(phone_env, in_process, monkeypatch):
    monkeypatch.setattr(services, "ACTION_LOCK_WAIT", 0.5)
    phone_env.set_loaded_unit(UNIT_B, cap=7200, pending={"cap": None})
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("reload_applies_then_fails", True)
    install, ended, _ = install_while_busy(phone_env)  # the lock is busy when the look after the reload comes to b
    assert ended
    error = install.error
    assert isinstance(error, PhoneError) and error.code is Code.PHONE_DOWN and "daemon-reload" in error.message, error
    assert "not stopped: its action lock is busy" in error.message, error.message
    assert error.data["failed"] == ["b"] and error.data["not_stopped"] == ["b"] and "stopped" not in error.data, error.data


def test_a_unit_that_cannot_be_read_does_not_hide_a_phone_not_shown_gone_before_it(phone_env, in_process, monkeypatch, listening):
    two_locked_phones_running(phone_env, cap=None)
    monkeypatch.setenv("ORION_PHONE_GONE_WAIT", "0.3")  # this process reads it, not the copy the CLI runner is given
    listening(5554)
    real = services._systemctl

    def no_answer_for_b(*args):
        if args[0] == "show" and args[-1] == UNIT_B:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user show failed: no bus", {"unit": UNIT_B})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", no_answer_for_b)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert error.code is Code.USAGE and "phone a" in error.message and "port 5554 still answers" in error.message, error.message
    assert UNIT_B in error.message and "could not be read" in error.message and "no bus" in error.message, error.message
    assert error.data["failed"] == ["a"] and error.data["unread"] == ["b"], error.data
    assert not list(units_of(phone_env).glob("*.service"))


def test_a_unit_that_cannot_be_read_does_not_keep_a_later_phone_from_being_stopped(phone_env, in_process, monkeypatch):
    two_locked_phones_running(phone_env, cap=None)
    real = services._systemctl

    def no_answer_for_a(*args):
        if args[0] == "show" and args[-1] == UNIT_A:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user show failed: no bus", {"unit": UNIT_A})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", no_answer_for_a)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert error.code is Code.PHONE_DOWN and error.data == {"unit": UNIT_A, "unread": ["a"], "stopped": ["b"]}, error.data  # a's own
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)  # and b was looked at, and stopped
    assert not list(units_of(phone_env).glob("*.service"))


# The action lock of a phone is a file in the state directory. One that cannot be opened (a directory in its place, or no
# directory to hold it) is not a PhoneError by itself, so it used to leave the install as INTERNAL with the phones already
# stopped lost. And a stop that never began is not a stop that ran: the phone is said to be not stopped, in its own words, with the
# lock file named, and never as one the emulator of which was told to exit or still answers. The other phones are still stopped.

LOCK_TROUBLE = {
    "a directory in the lock file's place": lambda leases: (leases / "b.act").mkdir(parents=True),
    "a file where the lock directory goes": lambda leases: (leases.parent.mkdir(parents=True, exist_ok=True), leases.write_text("x")),
}
NEVER_RAN = ("shown gone", "still answers", "still lists", "told to exit", "still running outside")  # claims about a stop that ran


@pytest.mark.parametrize("spoil", LOCK_TROUBLE.values(), ids=LOCK_TROUBLE)
def test_an_action_lock_that_cannot_be_opened_is_not_stopped_naming_it_and_the_other_phones_are_still_stopped(phone_env, spoil):
    two_locked_phones_running(phone_env, cap=None)
    leases = Path(phone_env.env["ORION_PHONE_STATE"]) / "leases"
    spoil(leases)
    code, out = phone_env.run("install-units")
    message = out["message"]
    if (leases / "b.act").is_dir():  # the lock file of b is the trouble: a was stopped first, as any phone is
        assert out["data"] == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "not_stopped": ["b"], "stopped": ["a"]}, out
        assert str(leases / "b.act") in message, out
        message = message.split("(stopped and shown gone first")[0]  # what is said of b, before the note on a
    else:  # the directory that would hold every lock is the trouble: neither phone could be stopped
        assert out["data"] == {"phone": "a", "unit": UNIT_A, "failed": ["a", "b"], "not_stopped": ["a", "b"], "stopped": []}, out
        assert str(leases) in message, out
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "not stopped: its lock file cannot be opened" in message and "do not trust the phone until it is stopped" in message, out
    assert not any(claim in message for claim in NEVER_RAN), message
    assert "INTERNAL" not in json.dumps(out)
    assert not list(units_of(phone_env).glob("*.service"))  # before the writes: nothing written


def test_a_state_directory_that_cannot_be_used_is_a_stop_that_never_began_not_one_that_ran(phone_env, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, where the state directory's parent would go")
    phone_env.env["ORION_PHONE_STATE"] = str(blocker / "state")  # no lock can be taken in it
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    lock = blocker / "state" / "leases" / "b.act"  # where the action lock of b would be, were the state directory usable
    assert f"not stopped: its action lock {lock} cannot be taken: cannot use state directory {blocker / 'state'}" in out["message"], out
    assert "ORION_PHONE_STATE" in out["message"], out
    assert not any(claim in out["message"] for claim in NEVER_RAN), out["message"]
    assert out["data"] == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "not_stopped": ["b"], "stopped": []}, out
    assert stops(phone_env) == [] and UNIT_B in active_units(phone_env)


def test_the_phone_side_stop_of_a_lock_file_that_cannot_be_opened_is_not_attempted_naming_it(phone_env, in_process):
    phones = in_process()
    lock = Path(phone_env.env["ORION_PHONE_STATE"]) / "leases" / "b.act"
    lock.mkdir(parents=True)  # a directory where the lock file goes
    with pytest.raises(services.StopNotAttempted) as caught:
        phones.stop_for_install(phones.cfg.phones["b"])
    assert caught.value.code is Code.USAGE and str(lock) in caught.value.message, caught.value.message
    assert caught.value.message.startswith("not stopped: its lock file cannot be opened (") and "Is a directory" in caught.value.message
    assert caught.value.data == {"phone": "b", "path": str(lock)}
    assert "stop" not in [call[1] for call in phone_env.calls("systemctl")]  # the stop never began


class InThread:
    """A call made in a daemon thread, for a test that must fail rather than hang when the call never returns."""

    def __init__(self, call) -> None:
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                self.result = call()
            except BaseException as exc:  # kept for the test to look at
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


def install_while_busy(phone_env, held: str = "b") -> tuple[InThread, bool, float]:
    """install-units in this process while another holder has the action lock of phone `held`. Returns the call (ended, or
    let to end once the lock is let go), whether it ended while the lock was held, and how long that took."""
    with phone_env.holding_the_action_lock(held):
        began = time.monotonic()
        install = InThread(lambda: phone_module.install_units(config.load()))
        ended = install.done.wait(20)
        took = time.monotonic() - began
    install.thread.join(20)  # the lock is let go: a call that was still waiting ends now, and no thread outlives the test
    return install, ended, took


def test_a_phone_whose_action_lock_stays_busy_is_not_stopped_and_the_wait_has_a_bound(phone_env, in_process, monkeypatch):
    monkeypatch.setattr(services, "ACTION_LOCK_WAIT", 0.5)  # an action holds the phone for the whole wait
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    install, ended, took = install_while_busy(phone_env)
    assert ended, "install-units waits for the action lock of a phone with no bound"
    assert 0.4 <= took < 15, took  # it did wait for the lock, as long as it was told to, and no longer
    error = install.error
    assert isinstance(error, PhoneError) and error.code is Code.USAGE, (error, install.result)
    lock = Path(phone_env.env["ORION_PHONE_STATE"]) / "leases" / "b.act"
    assert "phone b" in error.message and "not stopped: its action lock is busy" in error.message, error.message
    assert str(lock) in error.message and "0.5 s" in error.message, error.message
    assert "do not trust the phone until it is stopped" in error.message and "stop it by hand" in error.message, error.message
    assert not any(claim in error.message for claim in NEVER_RAN), error.message  # it was never stopped: nothing is known of it
    assert error.data == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "not_stopped": ["b"], "stopped": []}, error.data
    assert stops(phone_env) == [] and UNIT_B in active_units(phone_env)  # not stopped, under an action in flight or without it
    assert not list(units_of(phone_env).glob("*.service"))  # and nothing written


def test_a_busy_action_lock_does_not_keep_the_other_phones_from_being_stopped(phone_env, in_process, monkeypatch):
    two_locked_phones_running(phone_env, cap=None)
    monkeypatch.setattr(services, "ACTION_LOCK_WAIT", 0.5)
    install, ended, _ = install_while_busy(phone_env)  # b is busy; a is not
    assert ended
    error = install.error
    assert isinstance(error, PhoneError), (error, install.result)
    assert error.data == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "not_stopped": ["b"], "stopped": ["a"]}, error.data
    assert "(stopped and shown gone first: a)" in error.message, error.message
    assert stops(phone_env) == [UNIT_A] and UNIT_B in active_units(phone_env)  # a was stopped, b was not, and is not in the list


def test_a_phone_never_stopped_and_a_phone_not_shown_gone_are_named_in_their_own_words(phone_env, in_process, monkeypatch):
    two_locked_phones_running(phone_env, cap=None)
    a_stop_that_exits_zero_and_leaves_the_emulator_alive(phone_env)  # b's stop runs, and its emulator stays
    monkeypatch.setenv("ORION_PHONE_GONE_WAIT", "0.3")
    monkeypatch.setattr(services, "ACTION_LOCK_WAIT", 0.5)
    install, ended, _ = install_while_busy(phone_env, held="a")  # a is busy and never stopped; b is stopped, and not gone
    assert ended
    error = install.error
    assert isinstance(error, PhoneError), (error, install.result)
    a_said, b_said = error.message.split("; and ")
    assert "not stopped: its action lock is busy" in a_said and not any(claim in a_said for claim in NEVER_RAN), a_said
    assert "could not be stopped and shown gone" in b_said and "adb still lists emulator-5556" in b_said, b_said
    assert "not stopped" not in b_said, b_said
    assert error.data == {"phone": "a", "unit": UNIT_A, "failed": ["a", "b"], "not_stopped": ["a"], "stopped": []}, error.data
    assert stops(phone_env) == [UNIT_B]  # a was never stopped, b was stopped and is still there


def test_only_the_taking_of_the_action_lock_is_mapped_an_error_inside_the_stop_is_not_called_a_lock_error(
    phone_env, in_process, monkeypatch
):
    phones = in_process()

    def broken(phone, retry="lease", installing=False):
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(phones, "_stop_and_verify", broken)
    with pytest.raises(OSError, match="disk on fire"):
        phones.stop_for_install(phones.cfg.phones["b"])


def test_install_units_waits_for_an_action_in_flight_on_the_phone_before_it_stops_it(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    with phone_env.holding_the_action_lock("b"):  # an action is running on the phone
        proc = phone_env.popen("install-units")
        time.sleep(1.0)
        assert proc.poll() is None, "install-units stopped the phone under an action in flight"
        assert UNIT_B in active_units(phone_env) and stops(phone_env) == []
    out, err = proc.communicate(timeout=60)  # the action ended: now it stops it
    assert proc.returncode == 0 and json.loads(out)["data"]["stopped"] == ["b"], (out, err)
    assert stops(phone_env) == [UNIT_B]


def test_install_units_that_stops_no_phone_does_not_touch_the_state_directory(phone_env):
    state = Path(phone_env.env["ORION_PHONE_STATE"])
    assert not state.exists()
    assert phone_env.run("install-units")[0] == 0
    assert not state.exists()  # the phone side is only built when a phone has to be stopped


def test_the_phone_side_stop_holds_the_action_lock_for_as_long_as_it_stops_and_looks(phone_env, in_process, monkeypatch):
    phones = in_process()
    seen = []

    def stop_and_look(phone, retry="lease", installing=False):
        with phones.leases.try_acting("b") as free:  # a second open of the lock file finds the first's hold
            seen.append(free)

    monkeypatch.setattr(phones, "_stop_and_verify", stop_and_look)
    phones.stop_for_install(phones.cfg.phones["b"])
    assert seen == [False], "the stop and the looks ran without the phone's action lock"
    with phones.leases.try_acting("b") as free:
        assert free  # and it is let go of afterwards


def test_the_phone_side_stop_treats_a_missing_unit_whose_phone_is_gone_as_stopped(phone_env, in_process):
    phone_env.set_loaded_unit(UNIT_B, not_found=True)  # the unit is gone; so is the phone
    phones = in_process()
    phones.stop_for_install(phones.cfg.phones["b"])  # shown gone: nothing is left to stop, and nothing is raised


def test_the_phone_side_stop_of_a_missing_unit_with_a_phone_still_there_is_not_gone(phone_env, in_process):
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.phone_ports()
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_fake("emu_kill_fail", True)
    phones = in_process()
    with pytest.raises(PhoneError) as caught:
        phones.stop_for_install(phones.cfg.phones["b"])
    assert caught.value.code is Code.PHONE_DOWN and "adb still lists emulator-5556" in caught.value.message
    assert "then run install-units again" in caught.value.message
    assert "lease it again" not in caught.value.message  # the advice is for an install, not for a lease
    assert caught.value.message.count("install-units") == 1 and "orion-phone setup" not in caught.value.message  # said once


def test_a_stop_that_fails_after_another_succeeded_reports_what_was_already_stopped(phone_env, in_process, monkeypatch):
    set_tiers(phone_env, a="locked")
    for unit in (UNIT_A, UNIT_B):
        phone_env.set_loaded_unit(unit, cap=None)
        phone_env.start_unit_by_hand(unit)
    real = services._systemctl

    def refusing_b(*args):
        if args[0] == "stop" and args[-1] == UNIT_B:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user stop failed: refused", {"unit": UNIT_B})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", refusing_b)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.USAGE and UNIT_B in caught.value.message
    assert caught.value.data == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "stopped": ["a"]}
    assert not list(units_of(phone_env).glob("*.service"))


def test_the_stop_comes_before_any_unit_is_written(phone_env, in_process, monkeypatch):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    events: list[tuple] = []
    sync_spy(monkeypatch, events)  # every rename is a unit written
    real = services._systemctl

    def spy(*args):
        events.append(("systemctl", *args))
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", spy)
    phone_module.install_units(config.load())
    steps = [e for e in events if e[0] == "replace" or (e[0] == "systemctl" and e[1] in ("stop", "daemon-reload"))]
    assert steps[0] == ("systemctl", "stop", UNIT_B)  # first of all
    # the stop, three writes (the adb server's unit, then a's and b's), the reload
    assert [step[0] for step in steps] == ["systemctl", "replace", "replace", "replace", "systemctl"]


def test_a_refused_install_stops_no_one(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.env["ANDROID_AVD_HOME"] = "relative/avd"  # refused before anything is written or stopped
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert stops(phone_env) == [] and UNIT_B in active_units(phone_env)


def test_setup_reports_the_locked_phones_it_stopped(phone_env):
    switch_to_locked_while_running(phone_env)
    code, out = phone_env.run("setup")
    assert code == 0, out
    assert out["data"]["stopped"] == ["b"] and "not confirmed to enforce their session cap and core limits: b" in out["message"], out
    assert UNIT_B not in active_units(phone_env)


# The reload install-units makes is itself a change to a running session. systemd re-arms a running service's runtime limit
# from the reloaded RuntimeMaxSec and RuntimeRandomizedExtraSec, drop-ins included, so a pending drop-in that removes a
# capped session's cap passes the read made before the writes (the old values are still loaded) and takes effect at the
# reload. So every locked phone still running is read again after the reload, with the reload state too, and stopped when
# it is not exactly the configured cap with no extra run time and no reload pending, and reported like the others. (The fake
# models this as systemd's source reads, as a reviewer traced it; it is not a probe of a real user manager, which a test
# must not reload.)

DROP_INS = {  # what a drop-in on disk sets, which the reload loads: the cap the config calls for is gone
    "no cap at all (RuntimeMaxSec=infinity)": {"cap": None},
    "a shorter cap": {"cap": 3600},
    "a longer cap": {"cap": 9000},
    "extra run time past the cap": {"extra": 86400},
    "no cap and extra run time": {"cap": None, "extra": 3600},
}


@pytest.mark.parametrize("pending", DROP_INS.values(), ids=DROP_INS)
def test_a_session_whose_cap_a_pending_drop_in_removes_is_stopped_after_the_reload_and_reported(phone_env, pending):
    phone_env.set_loaded_unit(UNIT_B, pending=pending)  # loaded: the right cap; on disk, not yet loaded: a drop-in
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0, out
    assert out["data"]["stopped"] == ["b"] and "not confirmed to enforce their session cap and core limits: b" in out["message"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)
    calls = phone_env.calls("systemctl")
    assert calls.index(["--user", "stop", UNIT_B]) > calls.index(DAEMON_RELOAD)  # the reload is what uncapped it
    assert STATE_AND_CAP_B in calls and STATE_CAP_EXTRA_RELOAD_B in calls  # read before the writes, and again after


DROP_INS_STOP = {  # what a drop-in on disk sets that the reload loads: the cap stays, and a stop no longer ends the unit in time
    "KillMode=none": {"kill_mode": "none"},
    "KillMode=process": {"kill_mode": "process"},
    "a stop time of ten seconds": {"stop_timeout": 10},
    "SendSIGKILL=no": {"send_sigkill": "no"},
    "all three": {"kill_mode": "process", "stop_timeout": 10, "send_sigkill": "no"},
}


@pytest.mark.parametrize("pending", DROP_INS_STOP.values(), ids=DROP_INS_STOP)
def test_a_session_whose_stop_a_pending_drop_in_breaks_is_stopped_after_the_reload_and_reported(phone_env, pending):
    phone_env.set_loaded_unit(UNIT_B, pending=pending)  # loaded: a stop that ends it; on disk, not yet loaded: a drop-in
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0, out
    assert out["data"]["stopped"] == ["b"] and "not confirmed to enforce their session cap and core limits: b" in out["message"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)
    calls = phone_env.calls("systemctl")
    assert calls.index(["--user", "stop", UNIT_B]) > calls.index(DAEMON_RELOAD)  # the reload is what broke it


DROP_INS_RESTART = {  # a drop-in that has the manager start the phone again when its unit ends: a boot comes back
    "Restart=on-failure": {"restart": "on-failure"},
    "Restart=always": {"restart": "always"},
}


@pytest.mark.parametrize("pending", DROP_INS_RESTART.values(), ids=DROP_INS_RESTART)
def test_a_session_whose_unit_a_pending_drop_in_would_restart_is_stopped_after_the_reload_and_reported(phone_env, pending):
    phone_env.set_loaded_unit(UNIT_B, pending=pending)  # loaded: Restart=no; on disk, not yet loaded: a drop-in
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0, out
    assert out["data"]["stopped"] == ["b"] and "not confirmed to enforce their session cap and core limits: b" in out["message"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)
    calls = phone_env.calls("systemctl")
    assert calls.index(["--user", "stop", UNIT_B]) > calls.index(DAEMON_RELOAD)  # the reload is what loaded the drop-in


@pytest.mark.parametrize("loaded", DROP_INS_RESTART.values(), ids=DROP_INS_RESTART)
def test_a_session_whose_loaded_unit_would_restart_is_stopped_before_the_writes(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)  # the manager has loaded such a drop-in already
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    calls = phone_env.calls("systemctl")
    assert calls.index(["--user", "stop", UNIT_B]) < calls.index(DAEMON_RELOAD)  # first of all, as for any other cause
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


def test_the_fake_systemctl_prints_restart_as_real_systemd_255_does():
    import fake_systemctl

    units = ["orion-phone-b.service", "orion-phone-a.service", services.ADB_SERVER_UNIT]
    # read on a host: a line for each unit asked, a blank line between them, and the bare value for --value
    assert fake_systemctl.show({}, ["-p", "Restart", *units]) == "Restart=no\n\nRestart=on-failure\n\nRestart=always\n"
    assert fake_systemctl.show({}, ["-p", "Restart", "orion-phone-b.service"]) == "Restart=no\n"
    assert fake_systemctl.show({}, ["-p", "Restart", "--value", "orion-phone-b.service"]) == "no\n"


def test_the_fake_reload_takes_restart_from_the_unit_file_and_defaults_to_no_when_it_names_none(phone_env):
    import fake_systemctl

    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    units.mkdir()
    (units / UNIT_B).write_text("[Service]\nRestart=on-failure\n")
    (units / UNIT_A).write_text("[Service]\n")  # names none: the manager's default
    phone_env.set_loaded_unit(UNIT_A, restart="always")
    subprocess.run(["systemctl", "--user", "daemon-reload"], env=phone_env.env, check=True)
    state = json.loads(phone_env.fake_state.read_text())
    assert fake_systemctl.show(state, ["-p", "Restart", UNIT_B]) == "Restart=on-failure\n"
    assert fake_systemctl.show(state, ["-p", "Restart", UNIT_A]) == "Restart=no\n"


def test_the_fake_reload_takes_the_kill_mode_and_the_stop_time_from_the_unit_file_and_defaults_when_it_names_none(phone_env):
    import fake_systemctl

    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    units.mkdir()
    (units / UNIT_B).write_text("[Service]\nRuntimeMaxSec=7200\nKillMode=process\nTimeoutStopSec=45\nSendSIGKILL=no\n")
    (units / UNIT_A).write_text("[Service]\n")  # names none of them: the manager's defaults
    phone_env.set_loaded_unit(UNIT_A, kill_mode="none", stop_timeout=5, send_sigkill="no")
    subprocess.run(["systemctl", "--user", "daemon-reload"], env=phone_env.env, check=True)
    state = json.loads(phone_env.fake_state.read_text())
    ask = lambda unit: fake_systemctl.show(  # noqa: E731
        state, ["-p", "KillMode", "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", unit]
    )
    assert ask(UNIT_B) == "TimeoutStopUSec=45s\nKillMode=process\nSendSIGKILL=no\n"
    assert ask(UNIT_A) == "TimeoutStopUSec=1min 30s\nKillMode=control-group\nSendSIGKILL=yes\n"


def test_a_capped_session_with_nothing_pending_is_read_again_after_the_reload_and_left_alone(phone_env):
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    calls = phone_env.calls("systemctl")
    assert [c for c in calls if c[:2] == ["--user", "show"]] == [STATE_AND_CAP_B, STATE_CAP_EXTRA_RELOAD_B]
    assert calls.index(STATE_AND_CAP_B) < calls.index(DAEMON_RELOAD) < calls.index(STATE_CAP_EXTRA_RELOAD_B)
    assert UNIT_B in active_units(phone_env)


def test_a_pending_drop_in_that_keeps_the_configured_cap_leaves_the_session_alone(phone_env):
    phone_env.set_loaded_unit(UNIT_B, pending={"cap": 7200, "extra": 0})
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert UNIT_B in active_units(phone_env)


def test_a_unit_that_is_not_running_is_read_after_the_reload_and_not_stopped(phone_env):
    phone_env.set_loaded_unit(UNIT_B, pending={"cap": None})  # a drop-in would uncap it, but nothing runs from it
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == [] and stops(phone_env) == [], out
    assert STATE_CAP_EXTRA_RELOAD_B in phone_env.calls("systemctl")  # it was read after the reload, and found off


def test_a_session_whose_unit_still_has_a_reload_pending_after_the_reload_is_stopped(phone_env):
    phone_env.set_loaded_unit(UNIT_B, reload_pending=True)  # the right cap is loaded, and a change on disk is not
    phone_env.set_fake("reload_ineffective", True)  # the reload exits 0 and loads nothing: what runs is not known
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


def test_a_phone_stopped_before_the_writes_is_not_stopped_again_after_the_reload(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B]  # once


@pytest.mark.parametrize("a_is_stopped", ["before the writes", "after the reload"])
def test_a_session_that_cannot_be_stopped_after_the_reload_refuses_the_install_and_lists_who_was_stopped(
    phone_env, in_process, monkeypatch, a_is_stopped
):
    set_tiers(phone_env, a="locked")  # a and b are locked and running
    if a_is_stopped == "before the writes":
        phone_env.set_loaded_unit(UNIT_A, cap=None)  # a runs from the old open unit: stopped before anything is written
        phone_env.set_loaded_unit(UNIT_B, pending={"cap": None})
    else:
        # both are uncapped by the reload itself
        phone_env.set_loaded_unit(UNIT_A, cap=7200, restart="no", pending={"cap": None})
        phone_env.set_loaded_unit(UNIT_B, pending={"cap": None})
    phone_env.start_unit_by_hand(UNIT_A)
    phone_env.start_unit_by_hand(UNIT_B)
    real = services._systemctl

    def refusing_b(*args):
        if args[0] == "stop" and args[-1] == UNIT_B:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user stop failed: refused", {"unit": UNIT_B})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", refusing_b)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert error.code is Code.USAGE and UNIT_B in error.message and "systemctl --user stop" in error.message, error.message
    assert "reloaded" in error.message and "drop-in" in error.message, error.message  # the install is otherwise done
    assert error.data == {"phone": "b", "unit": UNIT_B, "failed": ["b"], "stopped": ["a"]}
    assert len(list(units_of(phone_env).glob("*.service"))) == 3  # a's, b's and the adb server's, all written
    assert DAEMON_RELOAD in phone_env.calls("systemctl")
    assert UNIT_A not in active_units(phone_env) and UNIT_B in active_units(phone_env)  # and the failure is not hidden


def test_a_read_after_the_reload_that_cannot_be_made_fails_the_install_and_keeps_the_stopped_list(
    phone_env, in_process, monkeypatch
):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)  # stopped before the writes
    real = services._systemctl
    reloaded: list[bool] = []

    def no_answer_after_the_reload(*args):
        if args[0] == "daemon-reload":
            reloaded.append(True)
        if args[0] == "show" and reloaded:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user show failed: no bus", {"unit": args[-1]})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", no_answer_after_the_reload)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.PHONE_DOWN and caught.value.data["stopped"] == ["b"], caught.value.data  # not a success


def test_a_read_before_the_writes_that_cannot_be_made_after_an_earlier_phone_was_stopped_keeps_the_list(
    phone_env, in_process, monkeypatch
):
    set_tiers(phone_env, a="locked")
    phone_env.set_loaded_unit(UNIT_A, cap=None)
    phone_env.start_unit_by_hand(UNIT_A)  # a is stopped; then the look at b cannot be made
    real = services._systemctl

    def no_answer_for_b(*args):
        if args[0] == "show" and args[-1] == UNIT_B:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user show failed: no bus", {"unit": UNIT_B})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", no_answer_for_b)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is Code.PHONE_DOWN, caught.value.data
    assert caught.value.data == {"unit": UNIT_B, "unread": ["b"], "stopped": ["a"]}, caught.value.data  # the unit not read, named
    assert not list(units_of(phone_env).glob("*.service"))  # and nothing was written


# Every failure after a stop says which phones were stopped, whatever failed: the operator must learn that those sessions
# ended even though the install did not finish. A failure when nothing was stopped adds no list.


def fail_writing_a_unit(env, monkeypatch) -> None:
    def refuse(path, data):
        raise PhoneError(Code.USAGE, f"cannot write {path} (test)", {"unit": path.name})

    monkeypatch.setattr(services, "_write_unit", refuse)


def fail_syncing_the_unit_directory(env, monkeypatch) -> None:
    real = services._sync_directory

    def refuse(path):
        if path == units_of(env):
            raise OSError(errno.EIO, "Input/output error")
        real(path)

    monkeypatch.setattr(services, "_sync_directory", refuse)


def fail_the_reload(env, monkeypatch) -> None:
    env.set_fake("systemctl_fail", "daemon-reload")


LATER_FAILURES = {
    "writing a unit": (fail_writing_a_unit, Code.USAGE),
    "syncing the unit directory": (fail_syncing_the_unit_directory, Code.USAGE),
    "the reload": (fail_the_reload, Code.PHONE_DOWN),
}


@pytest.mark.parametrize("fail, code", LATER_FAILURES.values(), ids=LATER_FAILURES)
def test_a_failure_after_a_stop_says_which_phones_were_stopped(phone_env, in_process, monkeypatch, fail, code):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)  # stopped before anything is written
    fail(phone_env, monkeypatch)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is code, caught.value
    assert caught.value.data["stopped"] == ["b"], caught.value.data
    assert UNIT_B not in active_units(phone_env)  # the stop did happen


@pytest.mark.parametrize("fail, code", LATER_FAILURES.values(), ids=LATER_FAILURES)
def test_a_failure_when_nothing_was_stopped_adds_no_list(phone_env, in_process, monkeypatch, fail, code):
    fail(phone_env, monkeypatch)  # nothing runs: nothing is stopped
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.code is code and "stopped" not in caught.value.data, caught.value.data


def test_a_sync_of_the_unit_directory_that_fails_says_the_units_are_written_what_to_run_and_who_was_stopped(
    phone_env, in_process, monkeypatch
):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)  # stopped before anything is written
    fail_syncing_the_unit_directory(phone_env, monkeypatch)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert error.code is Code.USAGE, error
    assert error.message == (
        f"cannot sync {units_of(phone_env)} (Input/output error); the units are written, but they may not survive a power loss "
        "until the sync succeeds: run install-units again; stopped the locked phones that were not confirmed to enforce their "
        "session cap and core limits: b"
    )
    assert error.data == {"path": str(units_of(phone_env)), "stopped": ["b"]}, error.data


def test_a_sync_of_the_unit_directory_that_fails_when_nothing_was_stopped_names_no_phone(phone_env, in_process, monkeypatch):
    fail_syncing_the_unit_directory(phone_env, monkeypatch)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    assert caught.value.message == (
        f"cannot sync {units_of(phone_env)} (Input/output error); the units are written, but they may not survive a power loss "
        "until the sync succeeds: run install-units again"
    )
    assert caught.value.data == {"path": str(units_of(phone_env))}


# Every failure of install-units after the units are written says so, whichever step it is: that the units are written (or
# were, and systemd reloaded them), what to run again, and which phones were stopped. The steps after the writes are the sync of
# the unit directory, the adb server's login link, the reload, and the look at the running locked phones after the reload (a
# phone it could not show gone, a unit it could not read). setup's own steps come after these (see test_setup).

STOPPED_NOTE = "stopped the locked phones that were not confirmed to enforce their session cap and core limits: "


def b_is_stopped_first(env) -> list[str]:
    """b runs from a unit without its cap, so the install stops it before it writes anything: the phone a failure must name."""
    env.set_loaded_unit(UNIT_B, cap=None)
    env.start_unit_by_hand(UNIT_B)
    return ["b"]


def the_sync_fails(env, monkeypatch) -> list[str]:
    stopped = b_is_stopped_first(env)
    fail_syncing_the_unit_directory(env, monkeypatch)
    return stopped


def the_adb_servers_login_link_fails(env, monkeypatch) -> list[str]:
    stopped = b_is_stopped_first(env)
    env.set_fake("systemctl_fail_on", ["enable", services.ADB_SERVER_UNIT])
    return stopped


def the_reload_fails(env, monkeypatch) -> list[str]:
    stopped = b_is_stopped_first(env)
    env.set_fake("systemctl_fail", "daemon-reload")
    return stopped


def the_look_after_the_reload_cannot_read_a_unit(env, monkeypatch) -> list[str]:
    stopped = b_is_stopped_first(env)
    env.set_fake("show_fails_from_reload", 1)  # the look before the writes was made; after the reload no unit can be read
    return stopped


def the_look_after_the_reload_cannot_show_a_phone_gone(env, monkeypatch) -> list[str]:
    set_tiers(env, a="locked")
    env.set_loaded_unit(UNIT_A, cap=None)  # a is stopped before anything is written
    env.set_loaded_unit(UNIT_B, pending={"cap": None})  # b is uncapped by the reload, and then it cannot be stopped
    for unit in (UNIT_A, UNIT_B):
        env.start_unit_by_hand(unit)
    real = services._systemctl

    def refusing_b(*args):
        if args[0] == "stop" and args[-1] == UNIT_B:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user stop failed: refused", {"unit": UNIT_B})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", refusing_b)
    return ["a"]


AFTER_THE_WRITES = {
    "the sync of the unit directory": the_sync_fails,
    "the adb server's login link": the_adb_servers_login_link_fails,
    "the reload": the_reload_fails,
    "the look after the reload, which cannot read a unit": the_look_after_the_reload_cannot_read_a_unit,
    "the look after the reload, which cannot show a phone gone": the_look_after_the_reload_cannot_show_a_phone_gone,
}


@pytest.mark.parametrize("arrange", AFTER_THE_WRITES.values(), ids=AFTER_THE_WRITES)
def test_every_failure_after_the_writes_says_the_units_are_written_what_to_run_again_and_who_was_stopped(
    phone_env, in_process, monkeypatch, arrange
):
    stopped = arrange(phone_env, monkeypatch)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert len(list(units_of(phone_env).glob("*.service"))) == 3, "not all the units were written: this is not after the writes"
    message = error.message
    assert re.search(r"units (are|were) written", message), message  # that they are on disk
    assert "run install-units again" in message, message  # and what to run
    names = ", ".join(stopped)
    first = f"stopped and shown gone first: {names}"
    assert f"{STOPPED_NOTE}{names}" in message or first in message, message  # and who was stopped
    assert error.data["stopped"] == stopped, error.data  # in the data too


def test_the_failure_after_a_stop_reaches_the_operator_through_the_command(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("systemctl_fail", "daemon-reload")
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert out["data"]["stopped"] == ["b"], out


# A reload that fails is not a reload that did not happen: our wait can run out while systemd goes on (it then loads what
# was pending, a drop-in included, into a running unit), or the answer can be lost after it loaded. So the running locked
# phones are looked at all the same, and stopped when they are not confirmed to be exactly the configured cap with no
# extra run time and no reload pending. The error stays the reload's, with the phones stopped and the advice to run
# install-units again once systemd is responsive.

ADVICE = "run install-units again once systemd is responsive"


@pytest.mark.parametrize("pending", DROP_INS.values(), ids=DROP_INS)
def test_a_reload_that_fails_after_systemd_applied_a_drop_in_still_stops_the_session_and_reports_it(phone_env, pending):
    phone_env.set_loaded_unit(UNIT_B, pending=pending)  # the right cap is loaded, and a drop-in on disk would remove it
    phone_env.set_fake("reload_applies_then_fails", True)  # the manager loads it, and the call reports failure
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # the error is the reload's
    assert "daemon-reload" in out["message"] and ADVICE in out["message"], out
    assert out["data"]["stopped"] == ["b"] and "stopped the locked phones" in out["message"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)
    calls = phone_env.calls("systemctl")
    assert calls.index(["--user", "stop", UNIT_B]) > calls.index(DAEMON_RELOAD)  # the look came after the reload failed


def test_a_reload_that_fails_before_loading_anything_stops_a_session_a_pending_drop_in_would_reach(phone_env):
    phone_env.set_loaded_unit(UNIT_B, pending={"cap": None})  # not loaded yet, and systemd may load it when it gets to it
    phone_env.set_fake("systemctl_fail", "daemon-reload")  # as when our wait for it runs out
    phone_env.start_unit_by_hand(UNIT_B)
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert ADVICE in out["message"] and out["data"]["stopped"] == ["b"], out  # a reload still pending: not confirmed
    assert UNIT_B not in active_units(phone_env)


def test_a_reload_failure_that_leaves_nothing_pending_stops_nothing(phone_env):
    phone_env.set_fake("systemctl_fail", "daemon-reload")
    phone_env.set_loaded_unit(UNIT_B, reload_text="no")  # the manager says that nothing is pending (the reload had happened)
    phone_env.start_unit_by_hand(UNIT_B)  # capped, no extra time, nothing pending: confirmed
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert ADVICE in out["message"] and "stopped" not in out["data"], out  # still told to run it again
    assert stops(phone_env) == [] and UNIT_B in active_units(phone_env)
    calls = phone_env.calls("systemctl")
    assert calls.index(DAEMON_RELOAD) < calls.index(STATE_CAP_EXTRA_RELOAD_B)  # and it was looked at, after the failure


def test_a_reload_failure_after_the_servers_link_leaves_a_reload_owed_so_a_running_locked_phone_is_stopped(phone_env):
    # The link was made without a reload, so every unit says NeedDaemonReload=yes until one works: a phone that runs is not
    # confirmed to be what is on disk, and the look after the failed reload stops it, as it stops any that is not
    phone_env.set_fake("systemctl_fail", "daemon-reload")
    phone_env.start_unit_by_hand(UNIT_B)  # capped, no extra time, and nothing pending but the link
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert ADVICE in out["message"] and out["data"]["stopped"] == ["b"], out
    assert stops(phone_env) == [UNIT_B] and UNIT_B not in active_units(phone_env)


def test_a_failed_reload_does_not_stop_an_open_phone_or_a_unit_that_is_off(phone_env):
    phone_env.set_loaded_unit(UNIT_B, pending={"cap": None})  # b is off: nothing runs from it
    phone_env.set_fake("systemctl_fail", "daemon-reload")
    phone_env.start_unit_by_hand(UNIT_A)  # and a is open: it has no cap to lack
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert stops(phone_env) == [] and "stopped" not in out["data"], out
    assert all(call[-1] != UNIT_A for call in phone_env.calls("systemctl") if call[:2] == ["--user", "show"])


def test_when_the_look_after_a_failed_reload_fails_too_the_reload_error_stays_with_what_the_look_managed(
    phone_env, in_process, monkeypatch
):
    set_tiers(phone_env, a="locked")
    for unit in (UNIT_A, UNIT_B):  # both are running with the right cap loaded and a drop-in pending that would remove it
        phone_env.set_loaded_unit(unit, cap=7200, restart="no", pending={"cap": None})
        phone_env.start_unit_by_hand(unit)
    phone_env.set_fake("systemctl_fail", "daemon-reload")
    real = services._systemctl

    def refusing_b(*args):
        if args[0] == "stop" and args[-1] == UNIT_B:
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user stop failed: refused", {"unit": UNIT_B})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", refusing_b)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert error.code is Code.PHONE_DOWN and "daemon-reload" in error.message, error  # the reload's error, not USAGE
    assert ADVICE in error.message and "could not all be checked afterwards" in error.message, error.message
    assert UNIT_B in error.message and "could not be stopped" in error.message, error.message  # and what the look found
    assert "reloaded them" not in error.message  # the reload is not known to have happened
    assert error.data["stopped"] == ["a"], error.data  # what the look did manage
    assert UNIT_A not in active_units(phone_env) and UNIT_B in active_units(phone_env)


def test_when_systemctl_cannot_be_read_after_a_failed_reload_the_reload_error_stays_with_the_earlier_stops(
    phone_env, in_process, monkeypatch
):
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    phone_env.start_unit_by_hand(UNIT_B)  # stopped before the writes
    real = services._systemctl
    reloaded: list[bool] = []

    def systemd_is_gone_after_the_reload(*args):
        if args[0] == "daemon-reload":
            reloaded.append(True)
            raise PhoneError(Code.PHONE_DOWN, "systemctl --user daemon-reload timed out after 120s", {})
        if reloaded:
            raise PhoneError(Code.PHONE_DOWN, f"systemctl --user {args[0]} timed out after 120s", {"unit": args[-1]})
        return real(*args)

    monkeypatch.setattr(services, "_systemctl", systemd_is_gone_after_the_reload)
    with pytest.raises(PhoneError) as caught:
        phone_module.install_units(config.load())
    error = caught.value
    assert error.code is Code.PHONE_DOWN and "daemon-reload timed out after 120s" in error.message, error.message
    assert ADVICE in error.message and "could not all be checked afterwards" in error.message, error.message
    assert error.data == {"unread": ["b"], "stopped": ["b"]}, error.data  # b was stopped before the reload, and not read after it


def test_a_look_after_the_reload_that_can_only_fail_to_read_a_unit_says_the_units_are_written_and_how_to_go_on(phone_env):
    phone_env.set_loaded_unit(UNIT_B, cap=None)  # the look before the writes stops b
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("show_fails_from_reload", 1)  # and after the reload no unit can be read
    code, out = phone_env.run("install-units")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    message = out["message"]
    assert "show" in message and UNIT_B in message, out  # systemctl's own words are kept
    assert "the units are written and systemd reloaded them" in message and "could not be read" in message, out
    assert "run install-units again" in message, out
    assert "not confirmed to enforce their session cap and core limits: b" in message, out
    assert out["data"] == {"unit": UNIT_B, "unread": ["b"], "stopped": ["b"]}, out


# The fake's reload, as the tests above rely on it.


def test_the_fake_reload_loads_a_pending_drop_in_into_a_running_unit_and_not_before():
    import fake_systemctl

    unit = "orion-phone-b.service"
    state = {"invocations": {unit: "run-1"},
             "loaded": {unit: {"cap": 7200, "reload_pending": False, "pending": {"cap": None, "extra": 3600}}}}
    asked = ["-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec", "-p", "ActiveState", "-p", "NeedDaemonReload", unit]
    assert fake_systemctl.show(state, asked).splitlines() == [
        "RuntimeMaxUSec=2h", "RuntimeRandomizedExtraUSec=0", "ActiveState=active", "NeedDaemonReload=yes",
    ]  # until the reload the running unit has its old cap, and says a reload is needed
    state["reload_ineffective"] = True
    fake_systemctl.reload_units(state)
    assert fake_systemctl.show(state, asked).splitlines()[-1] == "NeedDaemonReload=yes"  # a reload that loaded nothing
    del state["reload_ineffective"]
    fake_systemctl.reload_units(state)
    assert fake_systemctl.show(state, asked).splitlines() == [
        "RuntimeMaxUSec=infinity", "RuntimeRandomizedExtraUSec=1h", "ActiveState=active", "NeedDaemonReload=no",
    ]  # now it carries what the drop-in said
