"""Setup installs SDK packages, creates each phone once, fixes AVD settings, and starts only open phones."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orion_phone import config, services, setup
from orion_phone.codes import Code, PhoneError

# A Google Play image, because the config has a locked phone; not the default one, so a hard-coded one cannot pass.
IMAGE = "system-images;android-35;google_apis_playstore;x86_64"

UNIT_A, UNIT_B = "orion-phone-a.service", "orion-phone-b.service"
PROPERTIES = [  # what the look at a running locked phone asks of its unit (then whether it would start again, and its core limits)
    "-p", "ActiveState", "-p", "LoadState", "-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec", "-p", "KillMode",
    "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "Restart", "-p", "LimitCORE", "-p", "LimitCORESoft",
]
LOOK = ["--user", "show", *PROPERTIES, UNIT_B]  # before the writes: a pending reload does not count yet
LOOK_AFTER_A_RELOAD = ["--user", "show", *PROPERTIES, "-p", "NeedDaemonReload", UNIT_B]  # and after one, which must say no
RELOAD = ["--user", "daemon-reload"]
LINK_SERVER = ["--user", "enable", "--no-reload", services.ADB_SERVER_UNIT]
LINK_A = ["--user", "enable", "--no-reload", UNIT_A]
START_SERVER = ["--user", "start", services.ADB_SERVER_UNIT]
START_A = ["--user", "start", UNIT_A]
NOTE_FOR_B = "stopped the locked phones that were not confirmed to enforce their session cap and core limits: b"
SHOW_SERVER = ["--user", "show", "-p", "ActiveState", services.ADB_SERVER_UNIT]  # a look of the check that the server is up
SERVER_PORT = 5037  # the adb server's port: on the probe address in these tests, never the real 127.0.0.1
BOUND, SETTLE = 10.0, 2.0  # seconds setup waits for the server, and for which it must have stayed up (see the docs)
SETTLE_VARIABLE, WAIT_VARIABLE = "ORION_PHONE_SERVER_SETTLE", "ORION_PHONE_SERVER_WAIT"


def with_the_real_waits(env) -> None:
    """The waits the code has by default (and the docs state), not the short ones the tests run with: for a test that needs the
    settle to be longer than the abort of an adb that finds its port taken (about 0.7 s), and the wait to be long."""
    env.env[SETTLE_VARIABLE] = str(services.SERVER_SETTLE)
    env.env[WAIT_VARIABLE] = str(services.SERVER_READY_WAIT)


def without_the_looks_at_the_server(calls: list[list[str]]) -> list[list[str]]:
    """The systemctl calls without the looks of the check for the adb server: how many there are depends on the clock."""
    return [call for call in calls if call != SHOW_SERVER]


def test_setup_creates_phones_and_starts_only_open_ones(phone_env):
    code, out = phone_env.run("setup")
    assert code == 0, out
    assert any("system-images/android-36/google_apis_playstore/x86_64" in c for c in phone_env.calls("sdkmanager"))
    created = [entry["argv"] for entry in phone_env.calls("avdmanager")]
    assert sorted(argv[argv.index("-n") + 1] for argv in created) == ["orion-phone-a", "orion-phone-b"]
    for name in ("orion-phone-a", "orion-phone-b"):
        ini = (Path(phone_env.env["ANDROID_AVD_HOME"]) / f"{name}.avd" / "config.ini").read_text()
        assert "hw.keyboard = yes" in ini and "hw.keyboard = no" not in ini
        assert "disk.dataPartition.size = 16G" in ini and "hw.ramSize = 4096" in ini
    systemctl = phone_env.calls("systemctl")
    assert LINK_A in systemctl and START_A in systemctl
    assert not any(c[1] in ("enable", "start") and "orion-phone-b.service" in c for c in systemctl)  # a locked phone: never


def test_setup_creates_each_phone_only_once(phone_env):
    phone_env.run("setup")
    phone_env.run("setup")
    assert len(phone_env.calls("avdmanager")) == 2


def test_unaccepted_licenses_stop_setup_before_any_phone_exists(phone_env):
    state = json.loads(phone_env.fake_state.read_text())
    state["licenses_accepted"] = False
    phone_env.fake_state.write_text(json.dumps(state))
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE")
    assert "not accepted" in out["message"]
    assert phone_env.calls("avdmanager") == []


def test_missing_command_line_tools_is_usage(phone_env):
    (phone_env.root / "sdk" / "cmdline-tools" / "latest" / "bin" / "sdkmanager").unlink()
    code, out = phone_env.run("setup")
    assert code == 2 and "command-line tools" in out["message"]


def configure(env, **android) -> None:
    """Add keys to the test config's [android] table, which ends right before [lease]."""
    path = Path(env.env["ORION_PHONE_CONFIG"])
    extra = "".join(f"{key} = {json.dumps(value)}\n" for key, value in android.items())
    path.write_text(path.read_text().replace("\n[lease]", "\n" + extra + "\n[lease]", 1))


def avd_ini(env, name: str) -> str:
    return (Path(env.env["ANDROID_AVD_HOME"]) / f"orion-phone-{name}.avd" / "config.ini").read_text()


def test_setup_asks_each_tool_for_what_the_config_names(phone_env):
    configure(phone_env, system_image=IMAGE, device="pixel_6", ram_mb=2048, data_partition="8G")
    code, out = phone_env.run("setup")
    assert code == 0, out
    (sdkmanager,) = phone_env.calls("sdkmanager")
    # sdkmanager takes the slash form; the only option is the SDK root, and the packages are the three we need
    assert [a for a in sdkmanager if a.startswith("-")] == [f"--sdk_root={phone_env.root / 'sdk'}"]
    assert sorted(a for a in sdkmanager if not a.startswith("-")) == [
        "emulator", "platform-tools", "system-images/android-35/google_apis_playstore/x86_64",
    ]
    # avdmanager takes the semicolon form and the configured device, and is told "no" to its hardware-profile question
    entries = phone_env.calls("avdmanager")
    options = [dict(zip(e["argv"][2::2], e["argv"][3::2])) for e in entries]
    assert all(e["argv"][:2] == ["create", "avd"] and e["stdin"] == "no\n" for e in entries)
    assert sorted(o["-n"] for o in options) == ["orion-phone-a", "orion-phone-b"]
    assert all({k: v for k, v in o.items() if k != "-n"} == {"-k": IMAGE, "-d": "pixel_6"} for o in options)
    for name in ("a", "b"):
        assert "hw.ramSize = 2048" in avd_ini(phone_env, name) and "disk.dataPartition.size = 8G" in avd_ini(phone_env, name)


def test_setup_never_answers_the_license_prompt(phone_env, in_process, monkeypatch):
    """The operator accepts Google's license. Setup gives sdkmanager an empty stdin: end of input at its prompt."""
    given = []
    real = subprocess.run

    def spy(argv, **kwargs):
        given.append((Path(argv[0]).name, kwargs.get("input")))
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert setup.run(config.load()).code is Code.OK
    assert [stdin for tool, stdin in given if tool == "sdkmanager"] == [""]


def test_setup_installs_the_units_then_starts_only_the_open_phone(phone_env):
    code, out = phone_env.run("setup")
    assert code == 0, out
    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    # the debug bridge's server unit is written first, since the phones' units name it; the phones' follow in config order
    written = [units / services.ADB_SERVER_UNIT, units / "orion-phone-a.service", units / "orion-phone-b.service"]
    assert all(unit.is_file() for unit in written)
    assert out["data"]["units"] == [str(unit) for unit in written]
    # Every login link is made before a reload, and a reload is always followed by the look at the running locked phones; the
    # starts come after the last of them. The install looks at the locked phone's unit before it writes anything, makes the
    # server's link, reloads and looks; setup then links the open phone, reloads once more and looks again, and only then
    # starts the server (a lease talks to adb before it starts a phone) and the open phone. The locked phone is never
    # enabled or started.
    calls = phone_env.calls("systemctl")
    assert without_the_looks_at_the_server(calls) == [
        LOOK, LINK_SERVER, RELOAD, LOOK_AFTER_A_RELOAD,  # the install's
        LINK_A, RELOAD, LOOK_AFTER_A_RELOAD,  # setup's links, and its own reload and look
        START_SERVER, START_A,
    ]
    looks = [i for i, call in enumerate(calls) if call == SHOW_SERVER]  # between the start of the server and any phone's
    assert looks and calls.index(START_SERVER) < looks[0] and looks[-1] < calls.index(START_A), calls


def with_tiers(env, **tiers: str) -> None:
    """Rewrite the tier of these phones in the test config."""
    path = Path(env.env["ORION_PHONE_CONFIG"])
    text = path.read_text()
    for name, tier in tiers.items():
        text = re.sub(rf'(\[phones\.{name}\]\ntier = )"[a-z]+"', rf'\1"{tier}"', text)
    path.write_text(text)


TIERS = {"an open phone and a locked one": {"a": "open", "b": "locked"}, "locked phones only": {"a": "locked", "b": "locked"}}


@pytest.mark.parametrize("tiers", TIERS.values(), ids=TIERS)
def test_setup_links_and_starts_the_adb_server_before_the_phones_whatever_their_tiers(phone_env, tiers):
    # A lease on a locked phone talks to adb before it starts the phone, so a phone's own start cannot be what starts the
    # server: with locked phones only, nothing else would. The install made its link before its reload; setup starts it.
    with_tiers(phone_env, **tiers)
    code, out = phone_env.run("setup")
    assert code == 0, out
    calls = phone_env.calls("systemctl")
    assert calls.count(LINK_SERVER) == 1 and calls.index(LINK_SERVER) < calls.index(RELOAD), calls  # no link after a reload
    assert calls.count(START_SERVER) == 1 and calls.index(START_SERVER) > max(i for i, c in enumerate(calls) if c == RELOAD)
    open_units = [f"orion-phone-{name}.service" for name, tier in tiers.items() if tier == "open"]
    linked = [call[-1] for call in calls if call[:3] == ["--user", "enable", "--no-reload"] and call != LINK_SERVER]
    started = [call[-1] for call in calls if call[:2] == ["--user", "start"] and call != START_SERVER]
    assert (linked, started) == (open_units, open_units)
    assert all(calls.index(START_SERVER) < calls.index(["--user", "start", unit]) for unit in open_units)  # before every phone
    assert max(i for i, c in enumerate(calls) if c[:2] == ["--user", "enable"]) < max(  # and no link after the last reload
        i for i, c in enumerate(calls) if c == RELOAD
    )


# setup installs the units too, so it stops a running locked phone the way install-units does: the phone is reported stopped
# only once it is shown gone, and one that is not fails the setup before an open phone is started.


def test_setup_stops_a_running_locked_phone_without_its_cap_and_reports_it_once_it_is_shown_gone(phone_env):
    phone_env.set_loaded_unit("orion-phone-b.service", cap=None)  # an old unit with no session cap
    phone_env.start_unit_by_hand("orion-phone-b.service")
    phone_env.phone_ports()  # a running emulator has its console open: that is where the kill is sent
    phone_env.set_fake("stop_ineffective", True)  # an old unit's stop may leave the emulator: the kill is what ends it, and
    # a stop that did end it would take the console down too, and with it the kill (sent only to a console that answers)
    code, out = phone_env.run("setup")
    assert code == 0 and out["data"]["stopped"] == ["b"], out
    adb = phone_env.calls("adb")
    assert ["-s", "emulator-5556", "emu", "kill"] in adb and ["devices"] in adb, adb  # told to exit, then shown gone


def test_setup_does_not_report_a_locked_phone_stopped_that_still_runs_after_its_stop(phone_env):
    phone_env.set_loaded_unit("orion-phone-b.service", cap=None)
    phone_env.start_unit_by_hand("orion-phone-b.service")
    phone_env.phone_ports()
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_fake("stop_ineffective", True)  # `systemctl stop` exits 0 and the emulator keeps running
    phone_env.set_fake("emu_kill_fail", True)  # and the kill it is sent, to its open console, fails
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "adb still lists emulator-5556" in out["message"] and "do not trust" in out["message"], out
    assert out["data"] == {"phone": "b", "unit": "orion-phone-b.service", "failed": ["b"], "stopped": []}, out
    assert LINK_A not in phone_env.calls("systemctl") and START_A not in phone_env.calls("systemctl")  # nothing went on


# A link made with `enable --no-reload` leaves the unit-file state of the whole manager outdated until the next reload: on
# a real host (systemd 255) every unit said NeedDaemonReload=yes after one, and a locked lease is refused for that (USAGE)
# until something reloads. So no link may be made after the last reload. The install makes the server's before its reload,
# and setup, which links the open phones after the install's reload, reloads once more and looks again before it starts
# anything. The fake models the flag.


def test_a_locked_lease_right_after_setup_is_not_refused_for_a_reload_that_is_owed(phone_env):
    code, out = phone_env.run("setup")
    assert code == 0, out
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "starting"), out  # not USAGE


def test_setup_looks_again_after_its_own_reload_and_stops_a_session_that_reload_left_unloaded(phone_env):
    # b runs from a unit that carries its cap, so the install leaves it alone; setup's own reload then loads nothing (it exits
    # 0, and what was outdated still is), and the look that follows it is what finds b running from an unloaded unit
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("ineffective_reload_from", 2)
    code, out = phone_env.run("setup")
    assert code == 0, out
    assert out["data"]["stopped"] == ["b"], out
    assert "stopped the locked phones that were not confirmed to enforce their session cap and core limits: b" in out["message"], out
    calls = phone_env.calls("systemctl")
    last_reload = max(i for i, call in enumerate(calls) if call == RELOAD)
    assert LOOK_AFTER_A_RELOAD in calls[last_reload:] and ["--user", "stop", UNIT_B] in calls[last_reload:]
    assert calls.index(START_SERVER) > calls.index(["--user", "stop", UNIT_B])  # and nothing is started before the look


def a_phone_the_install_stops(env) -> None:
    """b runs from a unit without its cap, so the install stops it first: the phone a failure after that must still name."""
    env.set_loaded_unit(UNIT_B, cap=None)
    env.start_unit_by_hand(UNIT_B)


def assert_the_failure_keeps_what_the_install_did(out: dict, unit: str, what: str) -> None:
    """What every failure of setup after the install says: the units are written, how to go on, and the phones stopped."""
    message = out["message"]
    assert "the units are written" in message and what in message, out
    assert "run setup again" in message, out
    assert "stopped the locked phones that were not confirmed to enforce their session cap and core limits: b" in message, out
    assert out["data"]["stopped"] == ["b"] and out["data"]["unit"] == unit, out


def test_a_link_that_fails_says_the_units_are_written_how_to_go_on_and_keeps_the_stopped_list(phone_env):
    a_phone_the_install_stops(phone_env)
    phone_env.set_fake("systemctl_fail_on", ["enable", UNIT_A])
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "enable" in out["message"] and UNIT_A in out["message"], out  # systemctl's own words are kept
    assert_the_failure_keeps_what_the_install_did(out, UNIT_A, "could not be enabled at login")
    calls = phone_env.calls("systemctl")
    assert START_SERVER not in calls and START_A not in calls and calls.count(RELOAD) == 1  # nothing went on


def test_a_link_that_times_out_is_reported_the_same_way(phone_env):
    a_phone_the_install_stops(phone_env)
    phone_env.set_fake("systemctl_hang_on", ["enable", UNIT_A])
    phone_env.env["ORION_PHONE_SYSTEMCTL_TIMEOUT"] = "0.5"
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "timed out" in out["message"], out
    assert_the_failure_keeps_what_the_install_did(out, UNIT_A, "could not be enabled at login")


START_FAILURES = {"the server": services.ADB_SERVER_UNIT, "the open phone": UNIT_A}


@pytest.mark.parametrize("unit", START_FAILURES.values(), ids=START_FAILURES)
def test_a_start_that_fails_says_the_units_are_written_how_to_go_on_and_keeps_the_stopped_list(phone_env, unit):
    a_phone_the_install_stops(phone_env)
    phone_env.set_fake("systemctl_fail_on", ["start", unit])
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "start" in out["message"] and unit in out["message"], out  # systemctl's own words are kept
    assert_the_failure_keeps_what_the_install_did(out, unit, "could not be started")
    assert phone_env.calls("systemctl").count(RELOAD) == 2  # the links, the reload and the look were all done


def test_a_reload_of_setups_own_that_fails_says_how_to_go_on_and_keeps_the_stopped_list(phone_env):
    a_phone_the_install_stops(phone_env)
    phone_env.set_fake("fail_reload_from", 2)  # the install's reload works; setup's does not
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    message = out["message"]
    assert "daemon-reload" in message, out
    assert "the units are written, but systemd has not confirmed that it loaded them" in message, out
    assert "run setup again once systemd is responsive" in message and "install-units" not in message, out
    assert "stopped the locked phones that were not confirmed to enforce their session cap and core limits: b" in message, out
    assert out["data"]["stopped"] == ["b"], out
    calls = phone_env.calls("systemctl")
    assert START_SERVER not in calls and START_A not in calls  # nothing is started on a reload that did not answer


def test_a_look_after_setups_reload_that_can_only_fail_to_read_a_unit_keeps_what_the_install_did(phone_env):
    # b runs without its cap, so the install stops it; after setup's own reload no unit can be read, so the look has nothing
    # but a read that failed. That error stands, and it still says that the units are written, what to run, and who was stopped
    a_phone_the_install_stops(phone_env)
    phone_env.set_fake("show_fails_from_reload", 2)
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    message = out["message"]
    assert "show" in message and UNIT_B in message, out  # systemctl's own words are kept
    assert "the units are written and systemd reloaded them" in message and "could not be read" in message, out
    assert "run setup again" in message and "install-units" not in message, out
    assert NOTE_FOR_B in message, out
    assert out["data"]["stopped"] == ["b"] and out["data"]["unread"] == ["b"] and out["data"]["unit"] == UNIT_B, out
    calls = phone_env.calls("systemctl")
    assert START_SERVER not in calls and START_A not in calls  # nothing is started on a look that could not be made


def test_a_phone_stopped_because_its_unit_was_not_confirmed_is_not_said_to_have_run_without_its_cap(phone_env):
    # b runs from a unit with its cap, and setup's own reload fails: the link left a reload owed, so b is not confirmed, and the
    # look after the failed reload stops it. It did not run without its cap, and the message does not say that it did.
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.set_fake("fail_reload_from", 2)
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert out["data"]["stopped"] == ["b"], out
    assert NOTE_FOR_B in out["message"] and "without" not in out["message"], out


def test_the_look_after_setups_reload_that_cannot_stop_a_phone_says_to_run_setup_again(phone_env, in_process, monkeypatch):
    from orion_phone import phone as phone_module

    phone_env.set_loaded_unit(UNIT_B, cap=None)  # b runs without its cap, and cannot be stopped
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.phone_ports()
    monkeypatch.setenv("ORION_PHONE_GONE_WAIT", "0.3")  # this process, not the CLI runner's environment
    phone_env.set_fake("stop_ineffective", True)
    phone_env.set_fake("emu_kill_fail", True)
    in_process()
    with pytest.raises(PhoneError) as caught:
        phone_module.reload_and_look(config.load(), [], "setup")
    assert caught.value.code is Code.USAGE, caught.value.message
    message = caught.value.message
    assert "do not trust" in message and message.endswith("then run setup again"), message
    assert "install-units" not in message, message  # the stop's own advice names the command too: setup


def test_the_look_after_setups_reload_that_cannot_stop_a_phone_says_the_units_are_written_and_who_was_stopped(
    phone_env, in_process, monkeypatch
):
    from orion_phone import phone as phone_module

    phone_env.set_loaded_unit(UNIT_B, cap=None)  # b runs without its cap, and cannot be stopped
    phone_env.start_unit_by_hand(UNIT_B)
    phone_env.phone_ports()
    monkeypatch.setenv("ORION_PHONE_GONE_WAIT", "0.3")
    phone_env.set_fake("stop_ineffective", True)
    phone_env.set_fake("emu_kill_fail", True)
    in_process()
    stopped = ["a"]  # a phone an earlier step of this setup stopped
    with pytest.raises(PhoneError) as caught:
        phone_module.reload_and_look(config.load(), stopped, "setup")
    message = caught.value.message
    assert "the units were written and systemd reloaded them" in message and "run setup again" in message, message
    assert "(stopped and shown gone first: a)" in message, message
    assert caught.value.data["stopped"] == ["a"], caught.value.data


# `systemctl start` is no proof that the adb server is up. Its unit is Type=simple, which systemd counts as started the moment
# adb is spawned, not once adb has bound its port. With another adb server holding port 5037, start exits 0 at once and the
# unit's adb aborts a moment later (a real host: about 0.7 s, `Address already in use`, SIGABRT), which leaves the unit failed
# while the other server goes on answering. So setup, after it starts the server and before it starts an open phone, waits
# until the unit is active with its port answering and has stayed so for a few seconds. A port that answers is not enough, and
# neither is a unit that is active, for as long as an abort may still come. The fake models the unit going active and then
# failed (`server_aborts_after`), and the other server is a listener on the probe address.


def test_setup_starts_no_open_phone_until_the_adb_server_has_stayed_up(phone_env):
    settle = 1.0  # long enough to look again and again; the environment's, which the tests shorten for every other run
    phone_env.env[SETTLE_VARIABLE] = str(settle)
    began = time.monotonic()
    code, out = phone_env.run("setup")
    elapsed = time.monotonic() - began
    assert code == 0, out
    calls = phone_env.calls("systemctl")
    looks = [i for i, call in enumerate(calls) if call == SHOW_SERVER]
    assert len(looks) >= 3, calls  # looked at again and again, not once
    assert calls.index(START_SERVER) < looks[0] and looks[-1] < calls.index(START_A), calls  # after its start, before a phone's
    assert elapsed >= settle, elapsed  # and not before it had stayed up that long


def test_a_short_settle_from_the_environment_is_all_setup_waits(phone_env):
    phone_env.env[SETTLE_VARIABLE] = "0.05"  # what the tests run with
    code, out = phone_env.run("setup")
    assert code == 0, out
    looks = [call for call in phone_env.calls("systemctl") if call == SHOW_SERVER]
    assert 1 <= len(looks) <= 5, len(looks)  # not the eight or so that the 2 seconds of the default settle take


@pytest.mark.parametrize("tiers", TIERS.values(), ids=TIERS)
def test_a_server_that_aborts_because_another_server_holds_the_port_fails_setup_before_any_open_phone_starts(
    phone_env, listening, tiers
):
    with_tiers(phone_env, **tiers)
    with_the_real_waits(phone_env)  # a settle that outlasts the abort below, and a wait that a unit waited out would not end in
    phone_env.set_loaded_unit(UNIT_B, cap=None)  # b runs from an old unit: the install stops it first, and the failure names it
    phone_env.start_unit_by_hand(UNIT_B)
    listening(SERVER_PORT)  # another adb server holds the port, and goes on answering
    phone_env.set_fake("server_aborts_after", 0.7)  # and the unit's adb aborts, as it did on a real host
    began = time.monotonic()
    code, out = phone_env.run("setup")
    elapsed = time.monotonic() - began
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    message = out["message"]
    assert services.ADB_SERVER_UNIT in message and "failed" in message, out  # which unit, and what became of it
    assert "the units are written" in message and "no open phone was started" in message, out
    assert "Address already in use" in message and f"another adb server holds port {SERVER_PORT}" in message, out
    start = f"systemctl --user start {services.ADB_SERVER_UNIT}"
    assert message.index("adb kill-server") < message.index(start) < message.rindex("run setup again"), out  # in that order
    assert NOTE_FOR_B in message, out  # the phones stopped are named
    assert out["data"]["stopped"] == ["b"] and out["data"]["unit"] == services.ADB_SERVER_UNIT, out
    assert out["data"]["state"] == "failed", out
    calls = phone_env.calls("systemctl")
    assert START_SERVER in calls and START_A not in calls, calls  # the server was started, and no open phone after it
    assert elapsed < BOUND, elapsed  # a failed unit ends the wait at once: it is not waited out


def test_a_port_that_answers_while_the_servers_unit_is_failed_is_not_ready(phone_env, listening):
    with_the_real_waits(phone_env)  # so that a failed unit waited out would take as long as the real wait, and show
    listening(SERVER_PORT)  # the port answers (another server's) ...
    phone_env.set_fake("server_aborts_after", 0)  # ... and the unit is failed from the first look
    began = time.monotonic()
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert out["data"]["state"] == "failed" and "no open phone was started" in out["message"], out
    assert START_A not in phone_env.calls("systemctl")
    assert time.monotonic() - began < BOUND


def test_a_server_that_never_answers_fails_setup_when_the_wait_is_up(phone_env):
    wait = 1.5  # the environment's, which the tests keep short: the default is 10 seconds
    phone_env.env[WAIT_VARIABLE] = str(wait)
    phone_env.set_fake("server_never_binds", True)  # its adb is alive, and never listens: the unit stays active
    began = time.monotonic()
    code, out = phone_env.run("setup")
    elapsed = time.monotonic() - began
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    message = out["message"]
    assert services.ADB_SERVER_UNIT in message and f"port {SERVER_PORT} does not answer" in message, out
    assert f"within {wait:g} s of its start" in message, out  # the wait it had, not the default
    assert "the units are written" in message and "no open phone was started" in message, out
    assert "Address already in use" in message and "adb kill-server" in message, out
    assert out["data"]["state"] == "active" and out["data"]["port_answers"] is False, out
    assert START_A not in phone_env.calls("systemctl")
    assert wait <= elapsed < BOUND, elapsed  # it waited the whole time it had before it gave up, and not the default time


def test_a_look_at_the_servers_unit_that_cannot_be_made_fails_setup_before_any_open_phone_starts(phone_env):
    phone_env.set_fake("systemctl_fail_on", ["show", services.ADB_SERVER_UNIT])  # the check's own look: no answer is no pass
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "show failed" in out["message"] and "the units are written" in out["message"], out  # systemctl's own words are kept
    assert "no open phone was started" in out["message"] and "run setup again" in out["message"], out
    assert START_A not in phone_env.calls("systemctl")


def test_install_units_starts_nothing_and_does_not_check_the_adb_server(phone_env):
    code, out = phone_env.run("install-units")
    assert code == 0, out
    calls = phone_env.calls("systemctl")
    assert START_SERVER not in calls and SHOW_SERVER not in calls, calls  # it never starts the server: nothing to wait for
    assert not any(call[:2] == ["--user", "start"] for call in calls), calls


def test_the_wait_for_the_adb_server_is_what_the_docs_say():
    assert (services.SERVER_READY_WAIT, services.SERVER_SETTLE) == (BOUND, SETTLE)  # "10 seconds" and "2 seconds" in the docs
    assert (services.MAX_SERVER_WAIT, services.MAX_SERVER_SETTLE) == (120.0, 30.0)  # and the most either can be made
    assert services.ADB_SERVER_PORT == SERVER_PORT
    assert services.ADB_SERVER_ADDRESS == f"tcp:localhost:{services.ADB_SERVER_PORT}"  # one port, written once


# The wait and the settle are tuning variables, as the other time limits are: a slow machine may need longer ones, and the tests
# run with short ones. A value that is not a finite number above zero is ignored, and one above its maximum is held to it.


def test_the_waits_follow_the_environment_and_are_held_to_a_maximum(monkeypatch):
    for name in (SETTLE_VARIABLE, WAIT_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    assert services.server_waits() == (SETTLE, BOUND)  # none set: the defaults
    monkeypatch.setenv(SETTLE_VARIABLE, "0.3")
    monkeypatch.setenv(WAIT_VARIABLE, "4")
    assert services.server_waits() == (0.3, 4.0)  # each its own
    monkeypatch.setenv(SETTLE_VARIABLE, "1e9")
    monkeypatch.setenv(WAIT_VARIABLE, "1e9")
    assert services.server_waits() == (services.MAX_SERVER_SETTLE, services.MAX_SERVER_WAIT)  # held to the most
    monkeypatch.setenv(SETTLE_VARIABLE, "31")
    monkeypatch.setenv(WAIT_VARIABLE, "121")
    assert services.server_waits() == (30.0, 120.0)  # just over it
    for bad in ("0", "-1", "nan", "inf", "-inf", "soon", ""):
        monkeypatch.setenv(SETTLE_VARIABLE, bad)
        monkeypatch.setenv(WAIT_VARIABLE, bad)
        assert services.server_waits() == (SETTLE, BOUND), bad  # a bad value never turns the wait off, or makes it up


class FakeClock:
    """A clock that moves only when something sleeps, to see how a wait paces itself without waiting for it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("settle", ["0.05", "0.2", "1"])
def test_the_looks_at_the_server_are_never_further_apart_than_the_settle_and_it_ends_when_it_is_up(monkeypatch, settle):
    clock = FakeClock()
    monkeypatch.setattr(services, "time", clock)
    monkeypatch.setattr(services, "_show", lambda unit, *names: {"ActiveState": "active"})  # active, and its port answers
    monkeypatch.setattr(services, "_accepts", lambda host, port: True)
    monkeypatch.setenv(SETTLE_VARIABLE, settle)
    monkeypatch.setenv(WAIT_VARIABLE, "5")
    services.wait_for_adb_server([], "setup")
    assert clock.slept and max(clock.slept) <= min(float(settle), services.SERVER_POLL), clock.slept  # a settle is not overshot
    assert float(settle) <= clock.now < float(settle) + services.SERVER_POLL, clock.now  # and it ends once the settle is up


def test_setup_reports_the_phones_it_created(phone_env):
    first_code, first = phone_env.run("setup")
    second_code, second = phone_env.run("setup")
    assert (first_code, second_code) == (0, 0)
    assert first["data"]["created"] == ["a", "b"]
    assert second["data"]["created"] == []


def test_setup_keeps_every_other_setting_of_a_phone_and_repeats_cleanly(phone_env):
    phone_env.run("setup")
    first = avd_ini(phone_env, "a")
    assert first.endswith("\n")
    assert sorted(first.splitlines()) == sorted(
        ["hw.keyboard = yes", "hw.lcd.density = 420", "hw.ramSize = 4096", "disk.dataPartition.size = 16G"]
    )
    phone_env.run("setup")
    assert avd_ini(phone_env, "a") == first  # a second run changes and duplicates nothing


def test_set_ini_replaces_a_setting_however_it_is_spelled_and_keeps_the_rest(tmp_path):
    ini = tmp_path / "config.ini"
    ini.write_text("# a comment\nhw.keyboard=no\n\nhw.lcd.density = 420\n")
    setup.set_ini(ini, {"hw.keyboard": "yes", "hw.ramSize": "4096"})
    assert ini.read_text() == "# a comment\nhw.keyboard = yes\n\nhw.lcd.density = 420\nhw.ramSize = 4096\n"


def test_set_ini_keeps_a_value_that_holds_a_line_separator_on_its_one_line(tmp_path):
    # the file comes from the Android tools and the system image: a separator in a value is part of the value, and the
    # setting it spells (hw.ramSize here) is not a line of the file, so it is not ours to replace in place
    ini = tmp_path / "config.ini"
    odd = "tag.display = Google Play" + chr(0x2028) + "hw.ramSize = 99999"
    ini.write_text(odd + "\n" + "hw.keyboard = no\n", encoding="utf-8")
    setup.set_ini(ini, {"hw.keyboard": "yes", "hw.ramSize": "4096"})
    assert ini.read_text(encoding="utf-8") == odd + "\nhw.keyboard = yes\nhw.ramSize = 4096\n"


@pytest.mark.parametrize("separator", [chr(0x2029), chr(0x85), chr(13), chr(11), chr(12), chr(0x1C)],
                         ids=["paragraph separator", "next line", "carriage return", "vertical tab", "form feed", "file separator"])
def test_set_ini_breaks_lines_on_a_newline_and_nothing_else(tmp_path, separator):
    ini = tmp_path / "config.ini"
    odd = "x = a" + separator + "hw.ramSize = 99999"
    with open(ini, "w", encoding="utf-8", newline="") as fh:  # exactly these characters, none turned into newlines
        fh.write(odd + "\n")
    setup.set_ini(ini, {"hw.ramSize": "4096"})
    with open(ini, encoding="utf-8", newline="") as fh:
        assert fh.read() == odd + "\nhw.ramSize = 4096\n"


def test_set_ini_leaves_a_line_it_does_not_manage_exactly_as_it_was_including_a_carriage_return(tmp_path):
    ini = tmp_path / "config.ini"
    with open(ini, "w", encoding="utf-8", newline="") as fh:
        fh.write("a = 1\r\nhw.keyboard = no\r\n")
    setup.set_ini(ini, {"hw.keyboard": "yes"})
    with open(ini, encoding="utf-8", newline="") as fh:
        assert fh.read() == "a = 1\r\nhw.keyboard = yes\n"


def test_set_ini_makes_the_file_when_it_is_missing(tmp_path):
    ini = tmp_path / "config.ini"
    setup.set_ini(ini, {"hw.keyboard": "yes"})
    assert ini.read_text() == "hw.keyboard = yes\n"


def test_setup_deletes_every_phones_snapshots(phone_env):
    avd = Path(phone_env.env["ANDROID_AVD_HOME"])
    assert phone_env.run("setup")[0] == 0  # makes the phones
    for name in ("a", "b"):
        (avd / f"orion-phone-{name}.avd" / "snapshots" / "default_boot").mkdir(parents=True)
    assert phone_env.run("setup")[0] == 0
    # Every phone cold-boots. A locked phone's snapshot, taken while it was unlocked, would restore an unlocked phone,
    # and loading any phone's by hand would roll its disks back to that save.
    for name in ("a", "b"):
        assert not (avd / f"orion-phone-{name}.avd" / "snapshots").exists(), name


def test_setup_stops_when_a_locked_phones_snapshot_directory_cannot_be_deleted(phone_env, tmp_path):
    assert phone_env.run("setup")[0] == 0  # makes the phones
    snapshots = Path(phone_env.env["ANDROID_AVD_HOME"]) / "orion-phone-b.avd" / "snapshots"
    (tmp_path / "elsewhere").mkdir()
    snapshots.symlink_to(tmp_path / "elsewhere")  # rmtree refuses a link: a stand-in for any directory it cannot delete
    enabled = [c for c in phone_env.calls("systemctl") if "enable" in c]
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(snapshots) in out["message"] and "no snapshots" in out["message"]
    assert [c for c in phone_env.calls("systemctl") if "enable" in c] == enabled  # setup went no further


def test_missing_avdmanager_stops_setup_before_anything_is_downloaded(phone_env):
    (phone_env.root / "sdk" / "cmdline-tools" / "latest" / "bin" / "avdmanager").unlink()
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE") and "avdmanager" in out["message"]
    assert phone_env.calls("sdkmanager") == []


def write_tool(env, name: str, text: str, mode: int = 0o755) -> None:
    path = env.root / "sdk" / "cmdline-tools" / "latest" / "bin" / name
    path.write_text(text)
    path.chmod(mode)


BROKEN_TOOLS = {
    "not executable": ("#!/bin/sh\nexit 0\n", 0o644),
    "interpreter missing": ("#!/nonexistent/interpreter\n", 0o755),
}


@pytest.mark.parametrize("tool", ["sdkmanager", "avdmanager"])
@pytest.mark.parametrize("script, mode", BROKEN_TOOLS.values(), ids=BROKEN_TOOLS)
def test_a_tool_that_cannot_run_is_usage_naming_it_not_an_internal_error(phone_env, tool, script, mode):
    write_tool(phone_env, tool, script, mode)
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert f"cannot run {tool}" in out["message"]


def test_a_tool_that_outlives_its_time_limit_is_usage(phone_env, in_process, monkeypatch):
    seen = {}

    def hang(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(argv, seen["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(PhoneError) as caught:
        setup.run(config.load())
    assert caught.value.code is Code.USAGE and "sdkmanager" in caught.value.message
    assert seen["timeout"] and seen["timeout"] > 0  # a call always has a limit


def test_a_failing_tool_that_prints_bytes_that_are_not_text_is_still_usage(phone_env):
    write_tool(phone_env, "sdkmanager", "#!/bin/sh\nprintf '\\377 not text' >&2\nexit 1\n")
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "sdkmanager failed" in out["message"]


def test_a_tool_that_fails_silently_still_reports_its_exit_status(phone_env):
    write_tool(phone_env, "sdkmanager", "#!/bin/sh\nexit 3\n")
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE") and "exit 3" in out["message"]


def test_setup_leaves_a_phone_that_became_locked_without_a_login_link(phone_env):
    units = Path(phone_env.env["ORION_PHONE_UNIT_DIR"])
    wants = units / "default.target.wants"
    wants.mkdir(parents=True)
    for name in ("a", "b"):  # both were enabled while open; the config now says b is locked
        (wants / f"orion-phone-{name}.service").symlink_to(units / f"orion-phone-{name}.service")
    assert phone_env.run("setup")[0] == 0
    assert not (wants / "orion-phone-b.service").is_symlink()  # or the locked phone would boot at the next login
    assert (wants / "orion-phone-a.service").is_symlink()


def test_a_failure_reported_on_stdout_is_not_hidden_by_a_banner_on_stderr(phone_env):
    banner = "echo 'WARNING: sdkmanager is deprecated' >&2"  # the 23.0 sdkmanager prints one on every call
    write_tool(phone_env, "sdkmanager", f"#!/bin/sh\n{banner}\necho 'package not found'\nexit 1\n")
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE") and "package not found" in out["message"]


def test_a_long_failure_output_is_cut_to_its_tail(phone_env):
    write_tool(phone_env, "sdkmanager", "#!/bin/sh\nyes 'download progress' | head -n 200 >&2\necho 'the real reason' >&2\nexit 1\n")
    code, out = phone_env.run("setup")
    assert code == 2 and out["message"].endswith("the real reason") and len(out["message"]) < 1000


# Where setup makes the phones. The real avdmanager ignores an ANDROID_AVD_HOME that is not an existing directory and puts
# the phone in its default home instead, so setup works out the effective home (see test_services), makes the directory,
# and runs avdmanager with ANDROID_AVD_HOME set to that very path.

PHONES = ["orion-phone-a.avd", "orion-phone-b.avd"]


def made_in(home: Path) -> list[str]:
    return sorted(path.name for path in home.glob("*.avd"))


def given_to_avdmanager(env) -> list[str | None]:
    return [entry["avd_home"] for entry in env.calls("avdmanager")]


def unit_names_home(env, home: Path) -> bool:
    units = Path(env.env["ORION_PHONE_UNIT_DIR"])
    return all(f'Environment="ANDROID_AVD_HOME={home}"' in (units / f"orion-phone-{n}.service").read_text() for n in "ab")


def test_setup_makes_a_fresh_avd_home_and_the_phones_in_it(phone_env, tmp_path):
    home = tmp_path / "new-home" / "avd"  # neither it nor its parent exists yet
    phone_env.env["ANDROID_AVD_HOME"] = str(home)
    previous = os.umask(0o022)
    try:
        code, out = phone_env.run("setup")
    finally:
        os.umask(previous)
    assert code == 0, out
    assert stat.S_IMODE(home.stat().st_mode) == 0o700  # new, so owner-only
    assert made_in(home) == PHONES  # where the phones were asked for, not in a fallback
    assert given_to_avdmanager(phone_env) == [str(home)] * 2
    assert unit_names_home(phone_env, home)
    assert not (Path(phone_env.env["HOME"]) / ".android").exists()  # and nothing went to the default home


def test_setup_resolves_the_avd_home_before_it_makes_it_or_hands_it_on(phone_env, tmp_path):
    fresh = tmp_path / "fresh"
    phone_env.env["ANDROID_AVD_HOME"] = str(tmp_path / "nowhere" / ".." / "fresh")  # Python can follow this; the OS cannot
    code, out = phone_env.run("setup")
    assert code == 0, out
    assert made_in(fresh) == PHONES and not (tmp_path / "nowhere").exists()
    assert given_to_avdmanager(phone_env) == [str(fresh)] * 2  # the resolved path, never the one with `..` in it
    assert unit_names_home(phone_env, fresh)
    assert not (Path(phone_env.env["HOME"]) / ".android").exists()


def test_setup_leaves_an_existing_avd_home_as_it_is(phone_env, tmp_path):
    home = tmp_path / "mine"
    home.mkdir()
    home.chmod(0o755)
    (home / "other.ini").write_text("keep\n")
    phone_env.env["ANDROID_AVD_HOME"] = str(home)
    assert phone_env.run("setup")[0] == 0
    assert stat.S_IMODE(home.stat().st_mode) == 0o755  # not its to change
    assert (home / "other.ini").read_text() == "keep\n" and made_in(home) == PHONES


def test_setup_with_no_variable_set_makes_the_default_home_and_tells_avdmanager(phone_env):
    phone_env.env.pop("ANDROID_AVD_HOME")
    home = Path(phone_env.env["HOME"]) / ".android" / "avd"
    code, out = phone_env.run("setup")
    assert code == 0, out
    assert made_in(home) == PHONES
    assert given_to_avdmanager(phone_env) == [str(home)] * 2  # told, not left to find its own default
    assert unit_names_home(phone_env, home)


def test_setup_makes_the_phones_in_android_user_homes_avd_directory_when_no_avd_home_is_set(phone_env, tmp_path):
    phone_env.env.pop("ANDROID_AVD_HOME")
    phone_env.env["ANDROID_USER_HOME"] = str(tmp_path / "user")
    home = tmp_path / "user" / "avd"
    code, out = phone_env.run("setup")
    assert code == 0, out
    assert made_in(home) == PHONES
    assert given_to_avdmanager(phone_env) == [str(home)] * 2
    assert unit_names_home(phone_env, home)
    assert not (Path(phone_env.env["HOME"]) / ".android").exists()


@pytest.mark.parametrize("shape", ["a file at the path", "a file above the path"])
def test_an_avd_home_setup_cannot_make_is_usage_naming_it_before_any_tool_runs(phone_env, tmp_path, shape):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    home = blocker if shape == "a file at the path" else blocker / "avd"
    phone_env.env["ANDROID_AVD_HOME"] = str(home)
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(home) in out["message"] and "AVD home" in out["message"], out
    assert phone_env.calls("sdkmanager") == [] and phone_env.calls("avdmanager") == []  # nothing was downloaded or made


def test_a_phone_avdmanager_did_not_make_where_expected_is_usage_not_a_crash(phone_env):
    write_tool(phone_env, "avdmanager", "#!/bin/sh\nexit 0\n")  # succeeds, and makes nothing
    code, out = phone_env.run("setup")
    assert (code, out["code"]) == (2, "USAGE"), out
    home = phone_env.env["ANDROID_AVD_HOME"]
    assert f"{home}/orion-phone-a.avd" in out["message"] and "did not make" in out["message"], out


# The fake avdmanager is only worth having while it does what the real tool does with the home (observed with the
# command-line tools 23.0): the tests above are meaningful because it does.

FAKE_AVDMANAGER = Path(__file__).resolve().parent / "fake_avdmanager.py"
FAKE_CASES = {
    "an existing directory is used": ({"ANDROID_AVD_HOME": "{root}/there"}, "{root}/there"),
    "a missing one is ignored, not made": ({"ANDROID_AVD_HOME": "{root}/missing"}, "{root}/home/.android/avd"),
    "an empty value means the working directory": ({"ANDROID_AVD_HOME": ""}, "{root}"),
    "the user home's avd directory is used and made": ({"ANDROID_USER_HOME": "{root}/user"}, "{root}/user/avd"),
    "an avd home wins over the user home": (
        {"ANDROID_AVD_HOME": "{root}/there", "ANDROID_USER_HOME": "{root}/user"}, "{root}/there"),
}


@pytest.mark.parametrize("variables, lands", FAKE_CASES.values(), ids=FAKE_CASES)
def test_the_fake_avdmanager_puts_a_phone_where_the_real_one_would(phone_env, variables, lands):
    root = phone_env.root
    (root / "there").mkdir()
    environment = {k: v for k, v in phone_env.env.items() if k not in ("ANDROID_AVD_HOME", "ANDROID_USER_HOME")}
    environment.update({name: value.format(root=root) for name, value in variables.items()})
    subprocess.run(
        [sys.executable, str(FAKE_AVDMANAGER), "create", "avd", "-n", "x"],
        env=environment, input="no\n", text=True, check=True, cwd=root, capture_output=True,
    )
    assert (Path(lands.format(root=root)) / "x.avd" / "config.ini").is_file()
    assert not (root / "missing").exists()  # an ANDROID_AVD_HOME that is not there is never made
