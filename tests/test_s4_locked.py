"""S4: a locked phone is usable only after its owner unlocks it by hand, once per session."""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

import fake_adb
import pytest
from conftest import PROBE_HOST, pick_probe_host

from orion_phone.codes import Code, PhoneError
from orion_phone.lease import Lease, LeaseStore

START = ["--user", "start", "orion-phone-b.service"]
STOP = ["--user", "stop", "orion-phone-b.service"]
SHOW = ["--user", "show", "-p", "ActiveState", "-p", "InvocationID", "orion-phone-b.service"]  # which boot is it on
CAP_SHOW = [  # the loaded unit: that it is there, its cap, any extra run time past it, how a stop of it ends it, whether
    # the manager starts it again by itself, whether its file changed since, and its environment
    "--user", "show", "-p", "LoadState", "-p", "RuntimeMaxUSec", "-p", "RuntimeRandomizedExtraUSec", "-p", "KillMode",
    "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "Restart", "-p", "NeedDaemonReload", "-p", "Environment",
    "-p", "LimitCORE", "-p", "LimitCORESoft", "orion-phone-b.service",
]
COLD_BOOT = [STOP, CAP_SHOW, START, SHOW]  # stop, read the loaded unit, start, then read the boot the session is bound to
LOAD_SHOW = [  # what a stop that failed is followed by: is the unit there at all, and does anything run under it
    "--user", "show", "-p", "LoadState", "-p", "ActiveState", "orion-phone-b.service",
]


def start_session(env, holder: str = "agent-1"):
    """Request phone b while it is off: the session and its lease begin at the cold boot."""
    return env.run("lease", "b", "--holder", holder)


def unlocked_session(env, holder: str = "agent-1"):
    """A session as a phone that has its PIN is used: the cold boot (`starting`), a lease that finds the lock screen showing
    over the PIN (`locked`, the look that every boot needs before an unlock can count), the owner typing the PIN, and the
    lease that then holds."""
    start_session(env, holder)
    code, out = env.run("lease", "b", "--holder", holder)
    assert (code, out["data"]["state"]) == (5, "locked"), out
    env.set_device("emulator-5556", keyguard="false")  # the owner typed the PIN
    code, _ = env.run("lease", "b", "--holder", holder)
    assert code == 0


def test_leasing_an_off_locked_phone_starts_it_holds_it_and_needs_the_operator(phone_env):
    code, out = start_session(phone_env)
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting")
    assert out["data"]["holder"] == "agent-1" and out["data"]["expires_at"]
    assert START in phone_env.calls("systemctl")
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"]) == (3, "BUSY")  # the session stays reserved while the owner unlocks


def test_lock_screen_keeps_the_session_waiting(phone_env):
    start_session(phone_env)  # the fake boots it to its lock screen
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "locked")


def test_still_booting_keeps_the_session_waiting(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", booted=False)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "booting")


def test_unknown_lock_state_counts_as_locked(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "unknown")


def test_operator_unlock_completes_the_lease_and_allows_actions(phone_env):
    unlocked_session(phone_env)
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0


def test_relock_mid_session_stops_actions_and_is_audited(phone_env):
    unlocked_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="true")
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("type", "b", "--holder", "agent-1", "--", "1234")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR")
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])
    assert phone_env.audit("b")[-1]["code"] == "NEEDS_OPERATOR"


def test_release_shuts_the_locked_phone_down(phone_env):
    unlocked_session(phone_env)
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["data"]["stopped"]) == (0, True)
    assert phone_env.calls("systemctl")[-1] == STOP
    assert lease_file(phone_env) is None  # freed once the phone is shown gone


def test_a_failed_shutdown_keeps_the_phone_reserved(phone_env):
    unlocked_session(phone_env)
    phone_env.set_fake("systemctl_fail", "stop")
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN")
    assert phone_env.run("lease", "b", "--holder", "agent-2")[1]["code"] == "BUSY"  # never a free, unlocked phone


# Release promises a phone that is off. Stopping the unit does not reach an emulator the unit did not start, so the
# release looks for the phone as a new session does, and while it still shows the holder keeps the lease.


def test_a_release_that_cannot_see_the_phone_gone_keeps_the_lease(phone_env):
    unlocked_session(phone_env)
    phone_env.phone_ports()  # a running emulator has its console open: that is where the kill is sent
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_fake("stop_ineffective", True)  # the unit stops, but an emulator it did not start keeps running
    phone_env.set_fake("emu_kill_fail", True)
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert ADB_KILL in phone_env.calls("adb")  # the kill was sent, and did not work
    assert "still running outside its unit" in out["message"] and out["message"].endswith("then release it again")
    assert (out["data"]["phone"], out["data"]["unit"]) == ("b", "orion-phone-b.service")
    assert lease_file(phone_env)["holder"] == "agent-1"  # the holder keeps the lease
    assert phone_env.run("lease", "b", "--holder", "agent-2")[1]["code"] == "BUSY"  # never a free, unlocked phone
    phone_env.set_fake("stop_ineffective", False)
    phone_env.set_fake("emu_kill_fail", False)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "10"  # the phone's ports close a moment after it goes: the looks wait for that
    code, out = phone_env.run("release", "b", "--holder", "agent-1")  # the advice works
    assert (code, out["code"], out["data"]["stopped"]) == (0, "OK", True), out
    assert lease_file(phone_env) is None


def test_a_release_whose_phone_still_answers_on_a_port_keeps_the_lease(phone_env, listening):
    unlocked_session(phone_env)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    listening(5556)  # the unit stops and adb lists nothing, yet something still answers where the console was
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "port 5556 still answers" in out["message"] and out["message"].endswith("then release it again")
    assert out["data"]["port"] == 5556
    assert lease_file(phone_env)["holder"] == "agent-1"


def test_busy_is_answered_before_anything_boots(phone_env):
    unlocked_session(phone_env)
    phone_env.remove_device("emulator-5556")  # the session cap shut it down; agent-1's lease is still live
    starts = phone_env.calls("systemctl").count(START)
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"]) == (3, "BUSY")
    assert phone_env.calls("systemctl").count(START) == starts


def test_a_new_session_never_inherits_an_unlocked_phone(three_second_lease_env):
    env = three_second_lease_env
    unlocked_session(env)
    time.sleep(3.5)  # agent-1's lease lapses while the phone is still running and unlocked
    code, out = env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting")
    assert env.calls("systemctl")[-4:] == COLD_BOOT  # cold boot: the PIN is needed again


def test_a_running_phone_without_a_session_is_cold_booted_first(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")  # started and unlocked outside orion-phone
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "starting")
    assert phone_env.calls("systemctl")[-4:] == COLD_BOOT


def test_an_expired_holder_can_still_release_and_shut_down(three_second_lease_env):
    env = three_second_lease_env
    unlocked_session(env)
    time.sleep(3.5)
    code, out = env.run("release", "b", "--holder", "agent-1")
    assert (code, out["data"]["stopped"]) == (0, True)
    assert env.calls("systemctl")[-1] == STOP


def test_a_refused_type_never_puts_the_text_in_the_audit(phone_env):
    unlocked_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="true")  # the phone relocked mid-session
    code, out = phone_env.run("type", "b", "--holder", "agent-1", "--", "1234")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR")
    folder = Path(phone_env.env["ORION_PHONE_STATE"]) / "audit" / "b"
    assert "1234" not in "".join(f.read_text() for f in folder.glob("*.jsonl"))  # not a trace of the refused text
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["args"]) == ("type", "NEEDS_OPERATOR", {"text_length": 4})


# The guarantee behind "once per session": a phone is never handed over on the strength of a lease alone. The lease is
# written before the cold boot, so a crash, a kill, or a timeout leaves it behind; only a started phone makes it ready.

ADB_KILL = ["-s", "emulator-5556", "emu", "kill"]


def lease_file(env) -> dict | None:
    path = Path(env.env["ORION_PHONE_STATE"]) / "leases" / "b.json"
    return json.loads(path.read_text()) if path.exists() else None


def leave_a_lease_behind(env, holder: str) -> None:
    """All an interrupted lease call leaves: the lease, written before the cold boot, and nothing else."""
    LeaseStore(Path(env.env["ORION_PHONE_STATE"])).acquire("b", holder, 1800)


def kill_a_lease_call_at(env, verb: str, holder: str) -> None:
    """Run `lease b`; the fake systemctl SIGKILLs the call as it reaches this verb, before the verb does anything."""
    env.set_fake("systemctl_kill_parent", verb)
    proc = env.popen("lease", "b", "--holder", holder)
    proc.communicate(timeout=60)
    assert proc.returncode == -signal.SIGKILL
    env.set_fake("systemctl_kill_parent", None)


def test_an_interrupted_session_start_is_never_resumed(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")  # the last session's phone: up, and unlocked
    leave_a_lease_behind(phone_env, "agent-1")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out  # never OK on a phone that was not cold booted
    assert out["data"]["state"] == "starting"
    assert phone_env.calls("systemctl") == COLD_BOOT


def test_a_lease_call_killed_at_the_stop_leaves_nothing_to_resume(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    kill_a_lease_call_at(phone_env, "stop", "agent-2")
    assert phone_env.calls("systemctl") == [STOP]  # the stop was reached and never took effect: the phone is still up
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-2")  # straight to an action
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls())
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")  # or the retry
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out
    assert phone_env.calls("systemctl")[-4:] == COLD_BOOT
    assert lease_file(phone_env)["ready"] is True  # only the retry's start made the session ready


def test_a_lease_call_killed_at_the_start_boots_again_from_the_stop(phone_env):
    kill_a_lease_call_at(phone_env, "start", "agent-2")  # the phone was stopped; the call died as it was started
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting")
    assert phone_env.calls("systemctl")[-4:] == COLD_BOOT  # the whole cold boot again, never just a start


@pytest.mark.parametrize("blips", [1, 2])
def test_an_adb_blip_never_decides_whether_the_phone_is_stopped(phone_env, blips):
    phone_env.set_device("emulator-5556", keyguard="false")  # up and unlocked, but adb lists nothing for a moment
    phone_env.set_fake("adb_blip", blips)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out
    assert phone_env.calls("systemctl") == COLD_BOOT


def test_a_phone_started_outside_its_unit_is_killed_and_cold_booted(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.phone_ports()  # a running emulator has its console open: that is where the kill is sent
    phone_env.set_fake("stop_ineffective", True)  # systemctl stop cannot reach an emulator its unit did not start
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out
    assert ADB_KILL in phone_env.calls("adb")
    assert phone_env.calls("systemctl") == COLD_BOOT
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")  # the phone that is up now is a fresh, locked one
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "locked")


# `adb emu kill` talks to the phone's console port itself, not through the adb server. Under WSL2's mirrored networking a
# connect to a closed loopback port hangs instead of being refused (measured on a real host: `adb -s emulator-5556 emu kill`
# with the phone down ran until a 40 second timeout killed it), and a locked phone is down between every two sessions, so a
# kill sent every time would have every cold boot wait out orion-phone's 30 second adb limit. The stop sends it only when
# the console port accepts a plain connection: with nothing listening it could reach nothing anyway. What follows it is
# unchanged and decides: `adb devices`, then the ports. A foreign phone so new that it has opened no port yet, and that adb
# has not seen either, is the residual it always was.


def test_a_phone_that_is_down_is_not_told_to_exit_but_is_still_looked_for(phone_env):
    code, out = start_session(phone_env)  # no device: its console port is closed, as a phone that is down has it
    assert (code, out["data"]["state"]) == (5, "starting"), out
    adb = phone_env.calls("adb")
    assert ADB_KILL not in adb  # nothing listens for it
    assert ["devices"] in adb  # and the look at adb still decides
    assert phone_env.calls("systemctl") == COLD_BOOT


def test_a_phone_that_adb_still_lists_and_whose_console_is_closed_is_not_told_to_exit_and_fails_closed(phone_env):
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_device("emulator-5556", keyguard="false")  # listed, with no port of it answering
    phone_env.set_fake("stop_ineffective", True)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # the looks decide: adb still lists it
    assert "adb still lists emulator-5556" in out["message"], out
    assert ADB_KILL not in phone_env.calls("adb") and phone_env.calls().count(["devices"]) >= 2
    assert phone_env.calls("systemctl") == [STOP]  # never started on top of a phone that is still there


def test_a_phone_whose_adb_port_alone_answers_is_not_told_to_exit(phone_env, listening):
    # The kill goes to the console, which is the other port: sent when only the adb port is open, it would hang the same way.
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.set_fake("stop_ineffective", True)
    listening(5557)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # adb still lists it, and nothing told it to go
    assert ADB_KILL not in phone_env.calls("adb")


@pytest.mark.parametrize("state", ["device", "offline", "unauthorized"])
def test_a_phone_that_survives_the_stop_and_the_kill_fails_closed(phone_env, state):
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_device("emulator-5556", keyguard="false", state=state)  # listed in any state counts as still there
    phone_env.phone_ports()  # whatever state adb shows, its console is open: the kill is sent
    phone_env.set_fake("stop_ineffective", True)
    phone_env.set_fake("emu_kill_fail", True)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "outside" in out["message"] and out["data"]["unit"] == "orion-phone-b.service"
    assert ADB_KILL in phone_env.calls("adb")  # the kill was sent, and did not work
    assert phone_env.calls("systemctl") == [STOP]  # never started on top of a phone that is still up
    assert lease_file(phone_env) is None  # the session could not start: the lease is freed
    phone_env.set_fake("stop_ineffective", False)
    phone_env.set_fake("emu_kill_fail", False)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "10"  # the phone's ports close a moment after it goes: the looks wait for that
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out  # not BUSY


def test_a_phone_that_adb_still_lists_for_a_moment_is_waited_for(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.set_fake("adb_ghost", 2)  # after the stop, adb lists the dying phone as offline for two more looks
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out
    assert phone_env.calls("systemctl") == COLD_BOOT
    assert phone_env.calls().count(["devices"]) >= 3  # it looked until the phone was gone


def test_an_adb_that_cannot_answer_at_the_check_fails_closed(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.set_fake("adb_devices_fail", True)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # no answer is not "gone"
    assert START not in phone_env.calls("systemctl")
    assert lease_file(phone_env) is None


def test_a_stop_that_outlasts_the_timeout_is_phone_down_and_frees_the_lease(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.set_fake("systemctl_hang", "stop")
    phone_env.set_fake("hang_seconds", 4)
    phone_env.env["ORION_PHONE_SYSTEMCTL_TIMEOUT"] = "1"
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "timed out" in out["message"] and out["data"]["unit"] == "orion-phone-b.service"
    assert lease_file(phone_env) is None
    phone_env.set_fake("systemctl_hang", None)
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out  # the lease was freed: not BUSY


def test_a_stop_that_did_not_answer_in_time_is_not_followed_by_a_second_wait_for_the_manager(phone_env):
    # a unit that is not installed makes `systemctl stop` exit at once, so a stop that never answered is not that case, and
    # asking a manager that does not answer would double the wait before the refusal
    phone_env.set_fake("systemctl_hang", "stop")
    phone_env.set_fake("hang_seconds", 4)
    phone_env.env["ORION_PHONE_SYSTEMCTL_TIMEOUT"] = "1"
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN") and "timed out" in out["message"], out
    assert phone_env.calls("systemctl") == [STOP]


def test_a_failed_stop_frees_the_lease_and_names_the_phone_and_its_unit(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.set_fake("systemctl_fail", "stop")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert (out["data"]["phone"], out["data"]["unit"]) == ("b", "orion-phone-b.service")
    assert lease_file(phone_env) is None


def test_a_failed_start_frees_the_session(phone_env):
    phone_env.set_fake("systemctl_fail", "start")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert lease_file(phone_env) is None
    phone_env.set_fake("systemctl_fail", None)
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out  # not BUSY: nobody holds a phone that never started


def test_a_busy_loser_touches_nothing(phone_env):
    unlocked_session(phone_env)
    before = (len(phone_env.calls("systemctl")), len(phone_env.calls()))
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"]) == (3, "BUSY")
    assert (len(phone_env.calls("systemctl")), len(phone_env.calls())) == before  # no systemctl call, no adb call


def test_a_busy_loser_whose_early_look_was_stale_still_touches_nothing(phone_env, in_process, monkeypatch):
    unlocked_session(phone_env)
    phones = in_process()
    monkeypatch.setattr(phones.leases, "peek", lambda name: None)  # the loser looked before agent-1 took the lease
    before = (len(phone_env.calls("systemctl")), len(phone_env.calls()))
    with pytest.raises(PhoneError) as caught:
        phones.lease("b", "agent-2")  # only the lease itself can turn it away now
    assert caught.value.code is Code.BUSY
    assert (len(phone_env.calls("systemctl")), len(phone_env.calls())) == before


def test_an_action_on_a_locked_phone_that_went_down_never_boots_it(phone_env):
    unlocked_session(phone_env)
    phone_env.remove_device("emulator-5556")  # the session cap shut it down; agent-1's lease is still live
    starts = phone_env.calls("systemctl").count(START)
    sent = len(phone_env.shell_calls())  # the session's lease woke the screen: count from the tap
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert phone_env.calls("systemctl").count(START) == starts
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[sent:])


def test_an_action_needs_a_session_whose_phone_was_started(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    leave_a_lease_behind(phone_env, "agent-1")  # a lease, but its cold boot never happened
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out
    assert "lease" in out["message"]
    assert not any(c.startswith("input") for c in phone_env.shell_calls())
    assert phone_env.audit("b")[-1]["code"] == "NOT_HOLDER"


def test_a_session_that_was_never_started_is_not_holder_whatever_the_arguments(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    leave_a_lease_behind(phone_env, "agent-1")  # a lease, but its cold boot never happened
    code, out = phone_env.run("tap", "b", "5", "-5", "--holder", "agent-1")  # malformed as well: the lease comes first
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls())
    assert phone_env.audit("b")[-1]["code"] == "NOT_HOLDER"


# A foreign phone (one its unit did not start) that adb cannot see is still there for anything that looks at its ports.
# The probe looks at an address nothing listens on in tests (conftest), and these tests open the ports themselves.

FOREIGN_PORTS = {"console": (5556,), "adb": (5557,), "both": (5556, 5557)}


@pytest.mark.parametrize("ports", FOREIGN_PORTS.values(), ids=FOREIGN_PORTS)
def test_a_foreign_phone_that_adb_cannot_see_is_found_by_its_ports(phone_env, listening, ports):
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_device("emulator-5556", keyguard="false")  # started outside its unit, and unlocked
    phone_env.set_fake("stop_ineffective", True)
    phone_env.set_fake("emu_kill_fail", True)
    phone_env.set_fake("adb_blip", 1)  # and adb has just restarted: its one look at the check lists nothing
    listening(*ports)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "still running outside its unit" in out["message"] and out["data"]["port"] in ports
    assert phone_env.calls("systemctl") == [STOP]  # never started on top of a phone that still answers
    assert lease_file(phone_env) is None  # the session could not start: the lease is freed


def test_ports_that_close_within_the_wait_are_waited_for(phone_env, listening):
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "10"
    phone_env.set_device("emulator-5556", keyguard="false")  # adb sees the phone go; its ports linger a moment
    listener = listening(5556, 5557)

    def close_after_the_first_look():
        listener.first.wait(20)
        time.sleep(0.5)
        listener.close()

    closer = threading.Thread(target=close_after_the_first_look, daemon=True)
    closer.start()
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    closer.join(30)
    assert listener.first.is_set()  # the probe did see them answer
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out  # and it looked again until they were gone


def assert_a_safe_probe_address(text: str) -> None:
    address = ipaddress.ip_address(text)
    assert address.is_loopback, text
    assert address != ipaddress.ip_address("127.0.0.1"), text  # where real emulators listen
    assert address not in ipaddress.ip_network("127.0.0.0/16"), text  # where the system keeps its own: 127.0.0.53, 127.0.1.1


def test_no_test_can_reach_a_real_emulator_port(phone_env):
    assert_a_safe_probe_address(PROBE_HOST)
    assert phone_env.env["ORION_PHONE_PROBE_HOST"] == PROBE_HOST  # the CLI runner probes an address nothing uses
    assert os.environ["ORION_PHONE_PROBE_HOST"] == PROBE_HOST  # and so does any Phones built in this process


def test_the_probe_address_is_picked_at_random_and_always_from_the_safe_range():
    picks = {pick_probe_host() for _ in range(5000)}  # the whole range, not just this session's one pick
    assert len(picks) > 1  # a fixed address would let two suites running at once collide on the listeners' ports again
    for pick in picks:
        assert_a_safe_probe_address(pick)


# A session is bound to one boot of the unit: systemd gives every run its own InvocationID, which the lease records.


def test_the_lease_records_the_boot_it_started(phone_env):
    start_session(phone_env)
    unit_run = json.loads(phone_env.fake_state.read_text())["invocations"]["orion-phone-b.service"]
    assert lease_file(phone_env)["invocation"] == unit_run and unit_run


@pytest.mark.parametrize("retained", [False, True], ids=["id cleared", "last id kept by a failed unit"])
def test_a_unit_that_stopped_never_serves_its_old_holder(phone_env, retained):
    phone_env.set_fake("retain_id_after_stop", retained)
    unlocked_session(phone_env)
    phone_env.stop_unit()  # the session cap ends the run, and the phone goes with it
    sent = len(phone_env.shell_calls())  # the session's lease woke the screen: count from the tap
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out  # an id that is no longer a running boot is not ready
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[sent:])
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")  # leasing again boots it afresh
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting")
    assert phone_env.calls("systemctl")[-4:] == COLD_BOOT


def test_a_phone_the_operator_restarted_never_serves_the_old_holder(phone_env):
    unlocked_session(phone_env)
    phone_env.stop_unit()  # the session cap
    phone_env.start_unit_by_hand()  # the operator starts the phone themselves ...
    phone_env.set_device("emulator-5556", keyguard="false")  # ... and unlocks it
    sent = len(phone_env.shell_calls())  # the session's lease woke the screen: count from the tap
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out  # an unlock of another boot is not this session's
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[sent:])
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out  # never OK: that phone is stopped, and booted for this session
    assert phone_env.calls("systemctl")[-4:] == COLD_BOOT


def write_a_ready_lease_with_no_boot(env, holder: str) -> None:
    """An unexpired lease that is ready and names no boot, as leases written before the InvocationID binding were. The
    store refuses to make one (mark_ready needs an id), so the file is written directly, in the store's own format."""
    now = time.time()
    path = Path(env.env["ORION_PHONE_STATE"]) / "leases" / "b.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(asdict(Lease("b", holder, now, now + 1800, ready=True, invocation=""))))


def test_a_ready_lease_with_no_boot_on_record_never_serves_its_holder(phone_env):
    write_a_ready_lease_with_no_boot(phone_env, "agent-1")
    phone_env.stop_unit()  # the unit is not running, so it reports no InvocationID: as empty as the lease's
    phone_env.set_device("emulator-5556", keyguard="false")  # and the operator started the emulator by hand, unlocked
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out  # no id from the unit is not a match for no boot on record
    assert not any(c.startswith("input") for c in phone_env.shell_calls())
    last = phone_env.audit("b")[-1]  # it holds the lease, so its refusal is recorded like any other
    assert (last["action"], last["code"], last["holder"]) == ("tap", "NOT_HOLDER", "agent-1")


def test_a_start_that_leaves_the_unit_inactive_is_never_ready(phone_env):
    phone_env.set_fake("start_inactive", True)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # no boot to bind the session to
    assert "not active" in out["message"] and out["data"]["unit"] == "orion-phone-b.service"
    assert lease_file(phone_env) is None


# Refusals are answered at once: they never wait behind the action in flight on the phone.


def test_busy_is_answered_at_once_while_the_action_lock_is_held(phone_env):
    unlocked_session(phone_env)
    with phone_env.holding_the_action_lock("b"):  # an action is in flight on b
        code, out = phone_env.run("lease", "b", "--holder", "agent-2", timeout=10)
    assert (code, out["code"]) == (3, "BUSY")


def test_an_action_by_a_non_holder_is_refused_while_the_action_lock_is_held(phone_env):
    unlocked_session(phone_env)
    with phone_env.holding_the_action_lock("b"):
        code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-2", timeout=10)
    assert (code, out["code"]) == (4, "NOT_HOLDER")


def test_an_action_by_a_holder_whose_lease_lapsed_is_refused_while_the_action_lock_is_held(phone_env):
    LeaseStore(Path(phone_env.env["ORION_PHONE_STATE"])).acquire("a", "agent-1", -1)  # lapsed, and nobody took it over
    with phone_env.holding_the_action_lock("a"):
        code, out = phone_env.run("tap", "a", "5", "5", "--holder", "agent-1", timeout=10)
    assert (code, out["code"]) == (4, "NOT_HOLDER")
    assert "expired" in out["message"]
    assert not any(c.startswith("input") for c in phone_env.shell_calls())


def test_a_release_by_a_non_holder_is_refused_while_the_action_lock_is_held(phone_env):
    unlocked_session(phone_env)
    with phone_env.holding_the_action_lock("b"):
        code, out = phone_env.run("release", "b", "--holder", "agent-2", timeout=10)
    assert (code, out["code"]) == (4, "NOT_HOLDER")


def test_a_session_that_never_started_is_refused_at_once_and_recorded(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    leave_a_lease_behind(phone_env, "agent-1")  # a lease, but its cold boot never happened
    with phone_env.holding_the_action_lock("b"):
        code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1", timeout=10)
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out
    last = phone_env.audit("b")[-1]  # it holds the lease, so its refusal is recorded like any other
    assert (last["action"], last["code"], last["holder"]) == ("tap", "NOT_HOLDER", "agent-1")


# What went wrong is on record, and names the phone.


def test_a_failed_cold_boot_is_audited(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.set_fake("systemctl_fail", "stop")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["phone"], last["holder"]) == ("lease", "PHONE_DOWN", "b", "agent-1")


def test_a_failed_release_names_the_phone_and_its_unit(phone_env):
    unlocked_session(phone_env)
    phone_env.set_fake("systemctl_fail", "stop")
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert (out["data"]["phone"], out["data"]["unit"]) == ("b", "orion-phone-b.service")


def test_an_extreme_systemctl_timeout_is_a_limit_not_a_crash(phone_env):
    phone_env.env["ORION_PHONE_SYSTEMCTL_TIMEOUT"] = "1e30"  # no timeout can be this long: it is held to a day
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (5, "NEEDS_OPERATOR"), out


# The locked tier relies on file-based encryption and on a debug shell that cannot become root. A phone that has booted
# is asked what its image reports, before its lock state is read and before any action reaches it: ro.crypto.type must
# be `file` and ro.debuggable must be `0`. Anything else is refused: the phone is shut down, its lease freed, and the
# refusal audited. The check runs on every lease retry and every action after the boot, not once per boot: the
# properties cannot change during a boot, so a re-read only repeats the answer, but nothing has to be remembered.

BOOT_DONE = "getprop sys.boot_completed"
CRYPTO_TYPE = "getprop ro.crypto.type"
DEBUGGABLE = "getprop ro.debuggable"
LOCK_STATE = "dumpsys activity activities"

UNSAFE_IMAGES = {  # (ro.crypto.type, ro.debuggable) as the booted phone reports them
    "no encryption": ("none", "0"),
    "block-based encryption": ("block", "0"),
    "no encryption type reported": ("", "0"),
    "an encryption type that only resembles file": ("File", "0"),
    "a debuggable image": ("file", "1"),
    "no debuggable flag reported": ("file", ""),
    "no encryption and a debuggable image": ("none", "1"),
}


def devices(env) -> dict:
    return json.loads(env.fake_state.read_text())["devices"]


def boot_from_image(env, crypto: str, debuggable: str, **more) -> None:
    """Request phone b so that it cold boots, then have the booted phone report this image (and anything in `more`)."""
    start_session(env)
    env.set_device("emulator-5556", crypto_type=crypto, debuggable=debuggable, **more)


# What a phone reports is not made to match by what is trimmed from it: a value is the text up to its line end, and
# nothing else comes off, so a separator, a space, or a second newline after `file` is not `file`.

PADDED_VALUES = {
    "a line separator": chr(0x2028), "a paragraph separator": chr(0x2029), "a next line character": chr(0x85),
    "a no-break space": chr(0xA0), "a trailing space": " ", "a tab": chr(9), "a second newline": chr(10),
}


@pytest.mark.parametrize("padding", PADDED_VALUES.values(), ids=PADDED_VALUES)
def test_an_encryption_type_with_something_after_it_is_not_file(phone_env, padding):
    boot_from_image(phone_env, "file" + padding, "0")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "ro.crypto.type reports" in out["message"] and "\n" not in out["message"], out
    assert phone_env.calls("systemctl")[-1] == STOP and "emulator-5556" not in devices(phone_env)  # shut down


@pytest.mark.parametrize("padding", PADDED_VALUES.values(), ids=PADDED_VALUES)
def test_a_debuggable_flag_with_something_after_it_is_not_zero(phone_env, padding):
    boot_from_image(phone_env, "file", "0" + padding)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "ro.debuggable reports" in out["message"], out


def test_an_encryption_type_that_ends_in_a_carriage_return_is_still_file(phone_env):
    boot_from_image(phone_env, "file" + chr(13), "0" + chr(13))  # a pty gives \r\n for \n
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "locked"), out


# What ends the value of a one-line command is one terminal sequence: a newline, or a carriage return and a newline (a pty makes
# one of the other). Exactly one, and never both of two: a lone carriage return, a carriage return after the newline, and a
# doubled one stay in the value, which is then not `file` (or `0`, or `1`), so the image is refused and the boot is waited for.

@pytest.mark.parametrize(
    "text, expected",
    [
        ("file", "file"), ("file\n", "file"), ("file\r\n", "file"), ("", ""), ("\n", ""), ("\r\n", ""),
        ("file\r", "file\r"), ("file\n\r", "file\n\r"), ("file\r\r\n", "file\r"), ("file\r\r", "file\r\r"),
        ("file\n\n", "file\n"), ("file\r\n\r\n", "file\r\n"), ("file\r\n\n", "file\r\n"), ("\r", "\r"), ("file \r\n", "file "),
    ],
    ids=repr,
)
def test_a_value_loses_exactly_one_terminal_line_end_and_nothing_else(text, expected):
    from orion_phone.phone import _line_of

    assert _line_of(text) == expected


ODD_VALUE_ENDS = {
    "a lone carriage return": "\r",
    "a carriage return after the newline": "\n\r",
    "two carriage returns and a newline": "\r\r\n",
    "two newlines": "\n\n",
    "a CR LF and a CR LF": "\r\n\r\n",
}


@pytest.mark.parametrize("ending", ODD_VALUE_ENDS.values(), ids=ODD_VALUE_ENDS)
def test_an_image_whose_properties_end_oddly_is_refused_not_read_as_if_they_ended_cleanly(phone_env, ending):
    boot_from_image(phone_env, "file", "0", image_end=ending)  # the right values, with an end that is not a line end
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "ro.crypto.type reports" in out["message"] and "ro.debuggable reports" in out["message"], out
    assert phone_env.calls("systemctl")[-1] == STOP and "emulator-5556" not in devices(phone_env)  # shut down


def test_an_image_whose_properties_end_in_crlf_is_still_the_right_image(phone_env):
    boot_from_image(phone_env, "file", "0", image_end="\r\n", boot_end="\r\n")  # what a pty prints
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "locked"), out


@pytest.mark.parametrize("ending", ODD_VALUE_ENDS.values(), ids=ODD_VALUE_ENDS)
def test_a_boot_flag_that_ends_oddly_keeps_the_session_waiting(phone_env, ending):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", boot_end=ending)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "booting"), out


@pytest.mark.parametrize("text", ["1" + chr(0x2028), "1 ", " 1", "1" + chr(0xA0)], ids=repr)
def test_a_boot_flag_that_is_not_exactly_one_keeps_the_session_waiting(phone_env, text):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", boot_text=text)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "booting"), out


@pytest.mark.parametrize("crypto, debuggable", UNSAFE_IMAGES.values(), ids=UNSAFE_IMAGES)
def test_a_phone_whose_image_cannot_protect_it_is_refused_shut_down_and_freed(phone_env, crypto, debuggable):
    boot_from_image(phone_env, crypto, debuggable)  # the lock screen is showing, as after any cold boot
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out  # not NEEDS_OPERATOR: the owner is not asked to unlock it
    assert f"ro.crypto.type reports {crypto!r}" in out["message"], out
    assert f"ro.debuggable reports {debuggable!r}" in out["message"], out
    assert phone_env.calls("systemctl")[-1] == STOP and "emulator-5556" not in devices(phone_env)  # shut down
    assert lease_file(phone_env) is None  # freed: no one waits on a phone that cannot be used
    assert LOCK_STATE not in phone_env.shell_calls()  # refused before the lock state was read
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["holder"]) == ("lease", "USAGE", "agent-1")
    assert last["reported"] == {"ro.crypto.type": repr(crypto), "ro.debuggable": repr(debuggable)}
    assert out["data"]["reported"] == last["reported"] and out["data"]["phone"] == "b"
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting")  # not BUSY


def test_an_unlocked_phone_on_a_bad_image_is_refused_too(phone_env):
    boot_from_image(phone_env, "file", "1", keyguard="false")  # its owner has already typed the PIN
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert phone_env.calls("systemctl")[-1] == STOP and lease_file(phone_env) is None


def test_an_action_never_reaches_a_phone_whose_image_cannot_protect_it(phone_env):
    boot_from_image(phone_env, "none", "0", keyguard="false")  # an agent that acts without leasing again after the boot
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls())
    assert phone_env.calls("systemctl")[-1] == STOP and "emulator-5556" not in devices(phone_env)
    assert lease_file(phone_env) is None  # its session is over: the lease goes with the phone
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["args"]) == ("tap", "USAGE", {"x": 5, "y": 5})
    assert last["reported"]["ro.crypto.type"] == "'none'"
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER"), out  # nothing is left to act with


def test_the_image_is_read_after_the_boot_finishes_and_before_the_lock_state(phone_env):
    start_session(phone_env)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "locked")  # a safe image goes on to the lock screen, as before
    shell = phone_env.shell_calls()
    assert shell.index(BOOT_DONE) < min(shell.index(CRYPTO_TYPE), shell.index(DEBUGGABLE))
    assert max(shell.index(CRYPTO_TYPE), shell.index(DEBUGGABLE)) < shell.index(LOCK_STATE)


def test_a_phone_that_is_still_booting_is_not_asked_about_its_image_yet(phone_env):
    boot_from_image(phone_env, "none", "1", booted=False)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "booting")
    assert CRYPTO_TYPE not in phone_env.shell_calls() and DEBUGGABLE not in phone_env.shell_calls()
    assert phone_env.calls("systemctl").count(STOP) == 1  # only the cold boot's own stop: this phone is left alone


def test_an_open_phone_is_never_asked_about_its_image(phone_env):
    phone_env.set_device("emulator-5554", crypto_type="none", debuggable="1")  # the open tier claims no protection
    assert phone_env.run("lease", "a", "--holder", "agent-1")[0] == 0
    assert phone_env.run("tap", "a", "5", "5", "--holder", "agent-1")[0] == 0
    assert CRYPTO_TYPE not in phone_env.shell_calls() and DEBUGGABLE not in phone_env.shell_calls()


def test_a_refusal_that_cannot_stop_the_phone_tells_both_and_still_frees_the_lease(phone_env):
    boot_from_image(phone_env, "file", "1")
    phone_env.set_fake("systemctl_fail", "stop")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # still running: the stop's own code
    assert "ro.debuggable reports '1'" in out["message"] and "could not be shut down" in out["message"], out
    assert "stop" in out["message"] and out["data"]["unit"] == "orion-phone-b.service"
    assert lease_file(phone_env) is None  # the next session stops and boots the phone afresh anyway
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"]) == ("lease", "PHONE_DOWN") and last["reported"]["ro.debuggable"] == "'1'"


def test_what_a_phone_reports_about_its_image_is_quoted_and_cut_short(phone_env):
    hostile = chr(27) + "[31m" + "x" * 500 + chr(10) + "forged: line"  # untrusted: it lands in a message and a record
    boot_from_image(phone_env, "file", hostile)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert out["message"].isprintable() and len(out["message"]) < 700, out  # no escape character or line break survives
    assert all(value.isprintable() and len(value) < 100 for value in out["data"]["reported"].values())
    assert len(phone_env.audit("b")[-1]["reported"]["ro.debuggable"]) < 100


# A new phone boots with no screen lock at all: no lock screen, and nothing to swipe. Such a phone has no keyguard, so its
# lock state reads as unlocked and the locked tier would protect nothing, with nothing to say so. So a locked phone must have
# a secure screen lock: once its image has been checked, and before its lock state is read, `dumpsys lock_settings` must say
# that user 0's CredentialType is PIN, PASSWORD or PATTERN. NONE is no screen lock (a swipe-only lock included): NEEDS_OPERATOR
# with state `no-screen-lock`, which asks the owner to set a PIN on the phone's own screen. Any other name, or a report that
# cannot be read with certainty, is NEEDS_OPERATOR with state `unknown`, and the phone is treated as having none. The lease
# is kept, as for `locked` and `unknown`, and every action is refused. An open phone claims no protection and is never
# asked. The fake prints the report as the pinned image does: the device field `credential` is user 0's, "PIN" unless a test
# says otherwise.

LOCK_SETTINGS = "dumpsys lock_settings"
ACTIONS = {  # what follows the phone name, for every action there is
    "tap": ("tap", "5", "5"), "type": ("type", "--", "1234"), "swipe": ("swipe", "1", "2", "3", "4"), "key": ("key", "BACK"),
    "open": ("open", "https://example.com"), "shot": ("shot",), "ui": ("ui",),
}


def with_no_screen_lock(env, **more) -> None:
    """Request phone b so that it cold boots, then have the booted phone report no screen lock, as a new phone does (and,
    unless `more` says otherwise, nothing showing: with no lock there is no lock screen)."""
    start_session(env)
    env.set_device("emulator-5556", **{"credential": "NONE", "keyguard": "false", **more})


def test_a_locked_phone_with_no_screen_lock_is_refused_and_its_owner_is_asked_to_set_a_pin(phone_env):
    with_no_screen_lock(phone_env)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "no-screen-lock"), out
    message = out["message"]
    assert "no screen lock" in message and "set a PIN" in message, out
    assert "Settings, then Security, then Screen lock" in message and "before anything else" in message, out
    assert "unlock" not in message.lower(), out  # it asks for a PIN to be set, never for an unlock
    assert out["data"]["holder"] == "agent-1" and out["data"]["expires_at"]  # reserved, as for `locked` and `unknown`
    assert phone_env.run("lease", "b", "--holder", "agent-2")[1]["code"] == "BUSY"
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"]) == ("lease", "NEEDS_OPERATOR", "no-screen-lock")


@pytest.mark.parametrize("action", ACTIONS.values(), ids=ACTIONS)
def test_every_action_on_a_phone_with_no_screen_lock_is_refused_before_anything_reaches_it(phone_env, action):
    with_no_screen_lock(phone_env)
    before = len(phone_env.calls())
    code, out = phone_env.run(action[0], "b", "--holder", "agent-1", *action[1:])
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "no-screen-lock"), out
    sent = [call[3] for call in phone_env.calls()[before:] if len(call) > 3]  # shell and exec-out commands of that call
    assert [command for command in sent if not command.startswith(("getprop ", "dumpsys "))] == []  # only reads reached it
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"]) == (action[0], "NEEDS_OPERATOR")


@pytest.mark.parametrize("credential", ["PIN", "PASSWORD", "PATTERN"])
def test_a_secure_screen_lock_goes_on_to_the_lock_state_as_before(phone_env, credential):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", credential=credential)  # the lock screen showing, as after any cold boot
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "locked"), out
    phone_env.set_device("emulator-5556", keyguard="false")  # its owner typed it
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0


# A boot that was seen with no secure screen lock never becomes usable. Setting a PIN during that boot leaves the phone
# unlocked (nothing showed, so the owner had nothing to unlock, and was signed in when the PIN was set), and whoever holds the
# lease would be handed it: an unlocked phone must not reach a session whose owner did not unlock it. So the boot is marked
# when it is seen with none, and a later look in that boot that finds a secure screen lock refuses with `restart-needed`: an
# action only refuses, and a lease stops the phone for certain and cold boots it, as a new session does, so that the owner
# unlocks it afresh with the new PIN. The mark belongs to the boot: the next boot starts without it. (A boot that was never
# seen locked is refused the same way, whatever was seen of it: see the tests after these.)

PIN_SET_IN_THE_WINDOW = {"credential": "PIN", "keyguard": "false"}  # the owner set one, and is signed in: nothing showing


def first_setup_up_to_the_pin(env):
    """The first setup of a locked phone: the boot, a lease that says there is no screen lock, and then the owner sets a PIN
    in the phone window. Returns how many systemctl calls had been made by then."""
    with_no_screen_lock(env)
    code, out = env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "no-screen-lock"), out
    env.set_device("emulator-5556", **PIN_SET_IN_THE_WINDOW)
    return len(env.calls("systemctl"))


def test_an_action_after_a_pin_was_set_in_a_boot_with_none_is_refused_and_nothing_reaches_the_phone(phone_env):
    systemctl_before = first_setup_up_to_the_pin(phone_env)
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "restart-needed"), out
    assert "a screen lock was set after this boot was seen with none" in out["message"], out
    assert "lease it again" in out["message"] and "once a lease says it is locked" in out["message"], out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])  # nothing reached the phone
    started_or_stopped = [c for c in phone_env.calls("systemctl")[systemctl_before:] if c[1] in ("start", "stop")]
    assert started_or_stopped == []  # an action never starts or stops a unit: it looks at which boot it is on, and refuses
    assert phone_env.run("lease", "b", "--holder", "agent-2")[1]["code"] == "BUSY"  # and the session is still reserved


def test_a_lease_after_a_pin_was_set_in_a_boot_with_none_cold_boots_the_phone_and_the_owner_unlocks_it_afresh(phone_env):
    systemctl_before = first_setup_up_to_the_pin(phone_env)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "starting"), out  # not usable, and not OK
    # which boot the session is on, and then what any new session does: stopped for certain, the unit read, started again
    assert phone_env.calls("systemctl")[systemctl_before:] == [SHOW, *COLD_BOOT]
    assert "Do not unlock it yet" in out["message"], out
    assert "was seen with no screen lock, and one was set after that" in out["message"], out
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"]) == ("lease", "NEEDS_OPERATOR", "starting")
    assert last["restart"] == "restart-needed"  # the record says why this lease restarted the phone
    phone_env.set_device("emulator-5556", booted=False)  # the new boot is under way
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"].get("state") == "booting"
    phone_env.set_device("emulator-5556", booted=True)  # up, with the PIN that was set, and its lock screen showing
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "locked"), out
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 5  # still refused: nobody has unlocked it
    phone_env.set_device("emulator-5556", keyguard="false")  # the owner unlocks it with the new PIN
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0
    assert phone_env.calls("systemctl").count(START) == 2  # the session's own boot, and the one restart: no more


def test_the_refusal_and_the_restart_are_audited(phone_env):
    first_setup_up_to_the_pin(phone_env)
    phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    refused = phone_env.audit("b")[-1]
    assert (refused["action"], refused["code"], refused["state"]) == ("tap", "NEEDS_OPERATOR", "restart-needed")
    phone_env.run("lease", "b", "--holder", "agent-1")
    restarted = phone_env.audit("b")[-1]
    assert (restarted["action"], restarted["code"], restarted["state"]) == ("lease", "NEEDS_OPERATOR", "starting")
    assert restarted["restart"] == "restart-needed"


# A boot that cannot be used is not used for one of two causes, which the audit says as well as the reply does, with a stable
# short value for each: `seen-with-none` (it was seen with no screen lock, and has one now) and `never-seen-locked` (it was
# found unlocked without ever having been seen at its lock screen). The record of the action that was refused for it, the
# record of the lease that restarted the phone, and the record of a restart that failed all carry it, and the last has its state
# too, the one that sent the phone to be restarted.

CAUSES = {
    "seen-with-none": first_setup_up_to_the_pin,
    "never-seen-locked": lambda env: a_pin_set_in_a_boot_never_seen_locked(env, []),
}


@pytest.mark.parametrize("cause", CAUSES)
def test_an_action_refused_for_a_boot_that_cannot_be_used_is_audited_with_its_cause(phone_env, cause):
    CAUSES[cause](phone_env)
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["data"].get("state"), out["data"].get("cause")) == (5, "restart-needed", cause), out  # the reply says it
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"], last["cause"]) == ("tap", "NEEDS_OPERATOR", "restart-needed", cause)


@pytest.mark.parametrize("cause", CAUSES)
def test_a_lease_that_restarts_a_phone_is_audited_with_its_cause(phone_env, cause):
    CAUSES[cause](phone_env)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state"), out["data"].get("cause")) == (5, "starting", cause), out
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"]) == ("lease", "NEEDS_OPERATOR", "starting")
    assert (last["restart"], last["cause"]) == ("restart-needed", cause)


@pytest.mark.parametrize("cause", CAUSES)
def test_a_restart_that_fails_is_audited_with_its_state_and_its_cause(phone_env, cause):
    CAUSES[cause](phone_env)
    phone_env.set_fake("systemctl_fail", "start")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert out["code"] == "PHONE_DOWN", out
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"]) == ("lease", "PHONE_DOWN", "restart-needed")
    assert (last["restart"], last["cause"]) == ("restart-needed", cause)


# A refused action is audited with the state that refused it, as a refused lease is: whoever reads the record sees which
# wait the operator was asked into (no screen lock, the lock screen, a restart) without having to guess it from the code.

REFUSED_STATES = {
    "no screen lock": ({"credential": "NONE", "keyguard": "false"}, "no-screen-lock"),
    "the lock screen": ({}, "locked"),  # as after any cold boot of a phone that has a PIN
    "a screen lock that cannot be read": ({"lock_settings": ""}, "unknown"),
    "a lock state that cannot be read": ({"keyguard": "unknown"}, "unknown"),
    "a boot still under way": ({"booted": False}, "booting"),
    "a boot never seen at its lock screen": ({"keyguard": "false"}, "restart-needed"),
}


@pytest.mark.parametrize("fields, state", REFUSED_STATES.values(), ids=REFUSED_STATES)
def test_a_refused_action_audits_the_state_that_refused_it(phone_env, fields, state):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", **fields)
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", state), out
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"]) == ("tap", "NEEDS_OPERATOR", state)


def test_an_action_that_works_and_one_refused_for_its_arguments_audit_no_state(phone_env):
    unlocked_session(phone_env)
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0
    assert "state" not in phone_env.audit("b")[-1]
    code, out = phone_env.run("tap", "b", "99999", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out  # a refusal that is not a wait for the operator has no state to give
    assert phone_env.audit("b")[-1]["code"] == "USAGE" and "state" not in phone_env.audit("b")[-1]


def test_the_starting_reply_says_that_a_later_lease_asks_for_a_screen_lock_or_an_unlock(phone_env):
    # On a new phone the later lease asks for a screen lock first, and on a phone that has one it asks for the unlock: the
    # reply is the same for both, so it says both.
    code, out = start_session(phone_env)
    assert (code, out["data"]["state"]) == (5, "starting"), out
    message = out["message"]
    assert "set a screen lock" in message and "or unlock it" in message, out
    assert "Do not unlock it yet" in message and "lease it again" in message, out


def test_a_boot_that_still_has_no_screen_lock_is_not_restarted_by_the_next_lease(phone_env):
    with_no_screen_lock(phone_env)
    for _ in range(3):  # the owner has not set one yet: nothing to restart for, and no loop of restarts either
        code, out = phone_env.run("lease", "b", "--holder", "agent-1")
        assert (code, out["data"].get("state")) == (5, "no-screen-lock"), out
    assert phone_env.calls("systemctl").count(STOP) == 1 and phone_env.calls("systemctl").count(START) == 1


def test_a_boot_that_had_a_screen_lock_from_the_start_is_never_restarted(phone_env):
    unlocked_session(phone_env)  # a PIN, as after any boot of a phone that has one
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0
    assert phone_env.calls("systemctl").count(STOP) == 1 and phone_env.calls("systemctl").count(START) == 1


def test_a_report_that_could_not_be_read_does_not_cost_the_boot_a_restart(phone_env):
    # Only a report that says there is none marks a boot as unprotected. An unreadable one is refused (unknown) but is no
    # evidence of an unprotected boot, and a mark made on it would restart every boot whose first look happens to fail. It
    # is no sighting of the lock screen either, so nothing is recorded: a later look that does see the lock screen, and the
    # unlock after it, use the boot as any other.
    start_session(phone_env)
    phone_env.set_device("emulator-5556", lock_settings="")
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"].get("state") == "unknown"
    record = lease_file(phone_env)
    assert (record["unprotected"], record["seen_locked"]) == ("", "")
    phone_env.set_device("emulator-5556", lock_settings=real_report("PIN"))  # readable now: the lock screen shows
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"].get("state") == "locked"
    phone_env.set_device("emulator-5556", keyguard="false")  # its owner unlocks it
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0
    assert phone_env.calls("systemctl").count(START) == 1  # no restart on the way


def test_a_mark_on_the_boot_never_carries_over_to_the_next_session(phone_env):
    first_setup_up_to_the_pin(phone_env)
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"].get("state") == "starting"  # the restart
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"].get("state") == "locked"
    phone_env.set_device("emulator-5556", keyguard="false")
    assert phone_env.run("release", "b", "--holder", "agent-1")[0] == 0
    code, out = start_session(phone_env, "agent-2")  # a new session, on a new boot of a phone that has its PIN
    assert (code, out["data"].get("state")) == (5, "starting"), out
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["data"].get("state")) == (5, "locked"), out  # locked, not restart-needed
    phone_env.set_device("emulator-5556", keyguard="false")
    assert phone_env.run("lease", "b", "--holder", "agent-2")[0] == 0


# A boot is used only on positive evidence that its owner unlocked it by hand: a look in that same boot found the lock screen
# showing over a secure screen lock, and a later look finds the phone unlocked, still with one. A phone found unlocked with a
# screen lock in a boot that was never seen locked proves nothing: a PIN set during a boot leaves the phone unlocked (its
# owner was signed in when they set it, so there was no lock screen for anyone to unlock), and whoever holds the lease would
# be handed it. What the earlier looks of the boot saw does not matter: no look at all, a boot still under way, and a screen
# lock or a lock state that could not be read all end in the same refusal. An action refuses with `restart-needed` and starts
# nothing; a lease stops the phone for certain and cold boots it, as a new session does, and its owner unlocks it from its
# lock screen once a lease says `locked`. The only cost is one more restart when the owner unlocks before that: the replies
# already say not to.

BEFORE_THE_PIN = {  # what the looks of a boot saw before its owner set a PIN: none of them found the phone locked
    "no look at all": [],
    "a boot still under way": [{"booted": False}],
    "a screen lock that could not be read": [{"lock_settings": ""}],
    "a lock state that could not be read": [{"keyguard": "unknown"}],
}


def a_pin_set_in_a_boot_never_seen_locked(env, looks):
    """The boot (`starting`), then the looks, each a wait and none of them `locked`, and then the owner, signed in, sets a PIN:
    it reads, and nothing shows. Returns how many systemctl calls had been made by then."""
    start_session(env)
    for fields in looks:
        env.set_device("emulator-5556", **fields)
        assert env.run("lease", "b", "--holder", "agent-1")[0] == 5
    env.set_device("emulator-5556", booted=True, keyguard="false", lock_settings=real_report("PIN"))
    return len(env.calls("systemctl"))


@pytest.mark.parametrize("looks", BEFORE_THE_PIN.values(), ids=BEFORE_THE_PIN)
def test_a_lease_that_finds_the_phone_unlocked_in_a_boot_never_seen_locked_restarts_it(phone_env, looks):
    systemctl_before = a_pin_set_in_a_boot_never_seen_locked(phone_env, looks)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "starting"), out  # not OK
    assert phone_env.calls("systemctl")[systemctl_before:] == [SHOW, *COLD_BOOT]  # as for any new session
    assert "never seen at its lock screen" in out["message"] and "Do not unlock it yet" in out["message"], out
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"]) == ("lease", "NEEDS_OPERATOR", "starting")
    assert last["restart"] == "restart-needed"
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")  # the new boot is at its lock screen, which is now seen
    assert (code, out["data"].get("state")) == (5, "locked"), out
    phone_env.set_device("emulator-5556", keyguard="false")  # its owner unlocks it, as asked
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0
    assert phone_env.calls("systemctl").count(START) == 2  # the session's own boot and the one restart: no more


@pytest.mark.parametrize("looks", BEFORE_THE_PIN.values(), ids=BEFORE_THE_PIN)
def test_an_action_in_a_boot_never_seen_locked_is_refused_and_starts_nothing(phone_env, looks):
    systemctl_before = a_pin_set_in_a_boot_never_seen_locked(phone_env, looks)
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "restart-needed"), out
    assert "never seen at its lock screen in this boot" in out["message"] and "lease it again" in out["message"], out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])  # nothing reached the phone
    started_or_stopped = [c for c in phone_env.calls("systemctl")[systemctl_before:] if c[1] in ("start", "stop")]
    assert started_or_stopped == []  # an action never starts or stops a unit
    assert phone_env.run("lease", "b", "--holder", "agent-2")[1]["code"] == "BUSY"  # and the session is still reserved
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["state"]) == ("tap", "NEEDS_OPERATOR", "restart-needed")


@pytest.mark.parametrize("action", ACTIONS.values(), ids=ACTIONS)
def test_every_action_in_a_boot_never_seen_locked_is_refused_before_anything_reaches_the_phone(phone_env, action):
    a_pin_set_in_a_boot_never_seen_locked(phone_env, [])
    before = len(phone_env.calls())
    code, out = phone_env.run(action[0], "b", "--holder", "agent-1", *action[1:])
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "restart-needed"), out
    sent = [call[3] for call in phone_env.calls()[before:] if len(call) > 3]  # shell and exec-out commands of that call
    assert [command for command in sent if not command.startswith(("getprop ", "dumpsys "))] == []  # only reads reached it


def test_the_lock_screen_seen_over_a_pin_is_recorded_for_that_boot_and_the_unlock_after_it_is_used(phone_env):
    start_session(phone_env)
    assert lease_file(phone_env)["seen_locked"] == ""  # a boot that has only been started has not been seen locked
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "locked"), out
    record = lease_file(phone_env)
    assert record["invocation"] and record["seen_locked"] == record["invocation"]  # this boot, seen at its lock screen
    phone_env.set_device("emulator-5556", keyguard="false")  # its owner unlocks it
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0
    assert phone_env.calls("systemctl").count(START) == 1  # one boot, and no restart


def test_an_action_refused_at_the_lock_screen_is_a_look_that_saw_it(phone_env):
    start_session(phone_env)
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "locked"), out  # refused at the lock screen, as ever
    record = lease_file(phone_env)
    assert record["invocation"] and record["seen_locked"] == record["invocation"]
    phone_env.set_device("emulator-5556", keyguard="false")  # its owner unlocks it
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0  # and the boot is used, with no restart
    assert phone_env.calls("systemctl").count(START) == 1


def test_a_swipe_only_lock_screen_is_not_a_lock_screen_seen_over_a_screen_lock(phone_env):
    with_no_screen_lock(phone_env, keyguard="true")  # showing, over no screen lock: it needs only a swipe
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "no-screen-lock"), out
    assert lease_file(phone_env)["seen_locked"] == ""  # nothing protects this phone, so nothing was locked


WAITS_THAT_ARE_NOT_A_SIGHTING = {  # what every one of these says is that the phone was not seen at its lock screen
    "a boot still under way": {"booted": False},
    "a screen lock that cannot be read": {"lock_settings": ""},
    "a lock state that cannot be read": {"keyguard": "unknown"},
    "no screen lock": {"credential": "NONE", "keyguard": "false"},
}


@pytest.mark.parametrize("fields", WAITS_THAT_ARE_NOT_A_SIGHTING.values(), ids=WAITS_THAT_ARE_NOT_A_SIGHTING)
def test_a_look_that_did_not_find_the_phone_locked_over_a_screen_lock_records_no_sighting(phone_env, fields):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", **fields)
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 5
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 5
    assert lease_file(phone_env)["seen_locked"] == ""


def test_a_boot_seen_locked_does_not_make_the_next_sessions_boot_usable(phone_env):
    unlocked_session(phone_env)  # a boot seen locked, then unlocked, and used
    assert phone_env.run("release", "b", "--holder", "agent-1")[0] == 0
    code, out = start_session(phone_env, "agent-2")  # a new session, on a new boot
    assert (code, out["data"].get("state")) == (5, "starting"), out
    assert lease_file(phone_env)["seen_locked"] == ""
    phone_env.set_device("emulator-5556", keyguard="false")  # unlocked before any lease has seen its lock screen
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")
    assert (code, out["data"].get("state"), out["data"].get("restart")) == (5, "starting", "restart-needed"), out


def test_a_sighting_on_file_for_another_boot_counts_for_nothing(phone_env):
    start_session(phone_env)
    path = Path(phone_env.env["ORION_PHONE_STATE"]) / "leases" / "b.json"
    record = json.loads(path.read_text())
    record["seen_locked"] = "f" * 32  # the id of a boot that is not the one this session is bound to
    path.write_text(json.dumps(record))
    phone_env.set_device("emulator-5556", keyguard="false")
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "restart-needed"), out


def test_a_boot_seen_locked_and_then_with_none_is_not_used_and_nothing_of_it_counts_for_the_restart(phone_env):
    unlocked_session(phone_env)  # a boot with a PIN: seen locked, then unlocked
    phone_env.set_device("emulator-5556", credential="NONE")  # its owner took the PIN away
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "no-screen-lock"), out
    phone_env.set_device("emulator-5556", credential="PIN")  # and set one again, signed in: nothing shows
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "starting"), out  # seen with none: restarted, though seen locked before
    phone_env.set_device("emulator-5556", keyguard="false")  # the new boot, unlocked before any lease has seen its lock screen
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "starting"), out  # what the old boot was seen to be counts for nothing here
    assert phone_env.calls("systemctl").count(START) == 3


# A restart is a start like any other. It reads the loaded unit before it starts the phone, and refuses a unit that lacks the
# session cap, and whatever stops it is audited with the reason the phone was being restarted.


def test_a_restart_is_refused_like_any_start_when_the_unit_has_lost_its_cap(phone_env):
    systemctl_before = a_pin_set_in_a_boot_never_seen_locked(phone_env, [])
    phone_env.set_loaded_unit(UNIT_B, cap=None)  # the unit changed under the session: it no longer carries its cap
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "no session cap" in out["message"] and UNIT_B in out["message"] and "install-units" in out["message"], out
    assert phone_env.calls("systemctl")[systemctl_before:] == [SHOW, STOP, CAP_SHOW]  # looked, stopped, read: never started
    assert lease_file(phone_env) is None  # the session could not start again: freed
    assert "emulator-5556" not in devices(phone_env)  # and the phone is off
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["restart"]) == ("lease", "USAGE", "restart-needed")


RESTARTS_THAT_FAIL = {
    "a start that fails": ("systemctl_fail", "start", "PHONE_DOWN"),
    "a stop that fails": ("systemctl_fail", "stop", "PHONE_DOWN"),
    "a start that leaves the unit inactive": ("start_inactive", True, "PHONE_DOWN"),
}


@pytest.mark.parametrize("fault, value, code_name", RESTARTS_THAT_FAIL.values(), ids=RESTARTS_THAT_FAIL)
def test_a_restart_that_fails_is_audited_with_the_reason_it_was_made(phone_env, fault, value, code_name):
    a_pin_set_in_a_boot_never_seen_locked(phone_env, [])
    phone_env.set_fake(fault, value)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert out["code"] == code_name, out
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["restart"]) == ("lease", code_name, "restart-needed")
    assert lease_file(phone_env) is None  # the session could not start again: freed


def test_a_cold_boot_that_fails_for_a_new_session_has_no_restart_reason(phone_env):
    phone_env.set_fake("systemctl_fail", "start")
    code, out = start_session(phone_env)
    assert out["code"] == "PHONE_DOWN", out
    last = phone_env.audit("b")[-1]
    assert "restart" not in last and "cause" not in last and "state" not in last  # a new session, not a restart


def test_a_lease_record_from_before_the_marks_existed_still_works(phone_env):
    start_session(phone_env)
    path = Path(phone_env.env["ORION_PHONE_STATE"]) / "leases" / "b.json"

    def as_an_older_version_wrote_it() -> None:
        record = json.loads(path.read_text())
        record.pop("unprotected", None)
        record.pop("seen_locked", None)
        path.write_text(json.dumps(record))

    as_an_older_version_wrote_it()
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")  # it loads, and the lock screen showing is recorded
    assert (code, out["data"].get("state")) == (5, "locked"), out
    record = json.loads(path.read_text())
    assert record["invocation"] and record["seen_locked"] == record["invocation"]
    as_an_older_version_wrote_it()
    phone_env.set_device("emulator-5556", keyguard="false")
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")  # no sighting on file: read as never seen
    assert (code, out["data"].get("state")) == (5, "restart-needed"), out
    as_an_older_version_wrote_it()
    phone_env.set_device("emulator-5556", credential="NONE")  # and it can gain the other mark: no screen lock is found
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"].get("state") == "no-screen-lock"
    assert json.loads(path.read_text())["unprotected"] == record["invocation"]


@pytest.mark.parametrize("keyguard", ["true", "false", "unknown"])
def test_no_screen_lock_is_said_whatever_the_lock_state_is(phone_env, keyguard):
    # "true" is a swipe-only lock screen, showing: the lock state alone would have asked for an unlock. "unknown" is a lock
    # state that cannot be read: the screen lock is judged first, and is refused first.
    with_no_screen_lock(phone_env, keyguard=keyguard)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "no-screen-lock"), out
    assert LOCK_STATE not in phone_env.shell_calls()  # refused before the lock state was read


def real_report(credential: str = "PIN") -> str:
    return fake_adb.lock_settings_dump({"credential": credential})


PIN_LINE = "    CredentialType: PIN\n"
UNREADABLE_SCREEN_LOCKS = {
    "no User State block": real_report().replace("User State:", "Users:"),
    "two CredentialType lines": real_report().replace(PIN_LINE, PIN_LINE + PIN_LINE),
    "the timeout marker": real_report() + "*** SERVICE 'lock_settings' DUMP TIMEOUT (10000ms) EXPIRED ***\n",
    "a name the tool does not know": real_report("UNKNOWN_7"),
    "a name of capitals that is none of the four": real_report("UNKNOWN"),
    "nothing printed": "",
}


@pytest.mark.parametrize("report", UNREADABLE_SCREEN_LOCKS.values(), ids=UNREADABLE_SCREEN_LOCKS)
def test_a_screen_lock_that_cannot_be_read_with_certainty_counts_as_none(phone_env, report):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="false", lock_settings=report)  # nothing showing: the worst case
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"].get("state")) == (5, "NEEDS_OPERATOR", "unknown"), out
    assert "screen lock" in out["message"] and "treating it as having none" in out["message"], out
    assert LOCK_STATE not in phone_env.shell_calls()  # refused before the lock state was read
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "unknown"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])  # nothing reached the phone


def test_the_screen_lock_is_read_after_the_image_and_before_the_lock_state(phone_env):
    start_session(phone_env)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "locked")  # a PIN goes on to the lock screen, as before
    shell = phone_env.shell_calls()
    assert shell.index(BOOT_DONE) < min(shell.index(CRYPTO_TYPE), shell.index(DEBUGGABLE))
    assert max(shell.index(CRYPTO_TYPE), shell.index(DEBUGGABLE)) < shell.index(LOCK_SETTINGS) < shell.index(LOCK_STATE)


def test_an_unsafe_image_is_refused_as_one_even_when_the_phone_has_no_screen_lock(phone_env):
    boot_from_image(phone_env, "none", "0", credential="NONE")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out  # the image is judged first: the owner is not asked to set a PIN on it
    assert "ro.crypto.type reports" in out["message"], out
    assert LOCK_SETTINGS not in phone_env.shell_calls()
    assert phone_env.calls("systemctl")[-1] == STOP and lease_file(phone_env) is None


def test_a_phone_that_is_still_booting_is_not_asked_about_its_screen_lock_yet(phone_env):
    with_no_screen_lock(phone_env, booted=False)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "booting"), out
    assert LOCK_SETTINGS not in phone_env.shell_calls()


def test_an_open_phone_is_never_asked_about_its_screen_lock(phone_env):
    phone_env.set_device("emulator-5554", credential="NONE")  # the open tier claims no protection
    assert phone_env.run("lease", "a", "--holder", "agent-1")[0] == 0
    assert phone_env.run("tap", "a", "5", "5", "--holder", "agent-1")[0] == 0
    assert LOCK_SETTINGS not in phone_env.shell_calls()


# A locked phone's session cap lives in its unit, and only a unit that carries it may start the phone. A phone switched to
# locked keeps its old open unit (no cap) until install-units rewrites it, and a unit file that changed since the manager
# loaded it is not what would run. So a new session reads the unit as the manager has it loaded, after the old phone is
# stopped and before the new one is started, and refuses (USAGE: run install-units) a unit with no cap, with a cap other
# than the config's, with a reload pending, or with extra run time past the cap (RuntimeRandomizedExtraSec). Real systemd
# prints the cap as `2h` or `infinity` and no extra time as `0` (see fake_systemctl).

UNIT_B = "orion-phone-b.service"

STALE_UNITS = {  # what the manager has loaded of b (the config says 120 minutes), and what the refusal must say
    "no cap at all, as an open phone's unit has": ({"cap": None}, ["no session cap"]),
    "a shorter cap than the config says": ({"cap": 3600}, ["its session cap is 1h", "120 minutes"]),
    "a longer cap than the config says": ({"cap": 9000}, ["its session cap is 2h 30min", "120 minutes"]),
    "a unit file that changed since it was loaded": ({"reload_pending": True}, ["not reloaded", "NeedDaemonReload=yes"]),
    "no cap and a reload pending": ({"cap": None, "reload_pending": True}, ["no session cap", "NeedDaemonReload=yes"]),
    "the right cap and a day of extra run time": ({"extra": 86400}, ["RuntimeRandomizedExtraUSec=1d", "outlast its cap"]),
    "the right cap and an hour and a half of extra run time": ({"extra": 5400}, ["RuntimeRandomizedExtraUSec=1h 30min"]),
    "the right cap and unlimited extra run time": ({"extra_text": "infinity"}, ["RuntimeRandomizedExtraUSec=infinity"]),
    "the right cap and a kill mode that leaves the emulator running": ({"kill_mode": "none"}, ["KillMode=none", "control-group"]),
    "the right cap and a kill mode that spares the emulator": ({"kill_mode": "process"}, ["KillMode=process", "control-group"]),
    "the right cap and a stop time of half a minute": ({"stop_timeout": 30}, ["TimeoutStopUSec=30s", "60 s"]),
    "the right cap and a stop that nothing forces": ({"stop_timeout_text": "infinity"}, ["TimeoutStopUSec=infinity"]),
    "the right cap and a stop that is not forced when its time is up": ({"send_sigkill": "no"}, ["SendSIGKILL=no", "must be yes"]),
    # a drop-in the manager has loaded: it must never start a locked phone again, so none is started on a unit that would
    "the right cap and a drop-in that has the manager start it again on failure": (
        {"restart": "on-failure"}, ["Restart=on-failure", "must be no"]),
    "the right cap and a drop-in that has the manager always start it again": (
        {"restart": "always"}, ["Restart=always", "must be no"]),
}


@pytest.mark.parametrize("loaded, mentions", STALE_UNITS.values(), ids=STALE_UNITS)
def test_a_unit_without_its_session_cap_is_refused_before_any_start(phone_env, loaded, mentions):
    phone_env.set_loaded_unit(UNIT_B, **loaded)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    assert "install-units" in message and UNIT_B in message, out
    assert all(mention in message for mention in mentions), out
    assert phone_env.calls("systemctl") == [STOP, CAP_SHOW]  # the old phone stopped, the unit read, and nothing more
    assert START not in phone_env.calls("systemctl")  # no start ever reaches systemctl
    assert lease_file(phone_env) is None  # freed, like any other start that failed
    assert "emulator-5556" not in devices(phone_env)  # and the phone is off
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["holder"]) == ("lease", "USAGE", "agent-1")
    assert (out["data"]["phone"], out["data"]["unit"]) == ("b", UNIT_B)
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")  # not BUSY: nobody holds a phone that never started
    assert (code, out["code"]) == (2, "USAGE"), out


def test_a_running_phone_is_still_stopped_before_its_stale_unit_is_refused(phone_env):
    phone_env.set_device("emulator-5556", keyguard="false")  # up and unlocked, outside any session
    phone_env.set_loaded_unit(UNIT_B, cap=None)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert phone_env.calls("systemctl") == [STOP, CAP_SHOW]  # stopped first, as a new session always does
    assert "emulator-5556" not in devices(phone_env)  # an unlocked phone is not left running behind the refusal


def test_a_lease_refuses_a_locked_phone_whose_loaded_unit_would_let_it_dump_its_memory(phone_env):
    phone_env.set_loaded_unit(UNIT_B, core="infinity", core_soft="0")  # the manager's defaults: a unit written before LimitCORE
    code, out = start_session(phone_env)
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "LimitCORE=infinity" in out["message"] and "LimitCORESoft=0" in out["message"], out
    assert phone_env.calls("systemctl") == [STOP, CAP_SHOW]  # read after the stop, as always, and never started


def test_a_unit_with_its_session_cap_starts_the_session_as_before(phone_env):
    code, out = start_session(phone_env)
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert phone_env.calls("systemctl") == COLD_BOOT  # the unit is read between the stop and the start
    assert lease_file(phone_env)["ready"] is True


@pytest.mark.parametrize("spelling", ["120min", "7200s", "1h 60min", "2h"], ids=lambda s: s)
def test_any_spelling_of_the_configured_cap_is_the_cap(phone_env, spelling):
    phone_env.set_loaded_unit(UNIT_B, cap_text=spelling)  # arithmetic, not a comparison of strings
    code, out = start_session(phone_env)
    assert (code, out["data"]["state"]) == (5, "starting"), out
    assert START in phone_env.calls("systemctl")


UNREADABLE = {
    "words instead of a time span": {"cap_text": "forever"},
    "an empty cap": {"cap_text": ""},
    "a number with no unit": {"cap_text": "7200"},
    "a unit it does not know": {"cap_text": "2 hours"},
    "half a time span": {"cap_text": "2h 30"},
    "a cap property that is missing": {"omit": ["RuntimeMaxUSec"]},
    "a reload property that is missing": {"omit": ["NeedDaemonReload"]},
    "a reload answer that is neither yes nor no": {"reload_text": "maybe"},
    "an empty reload answer": {"reload_text": ""},
    "an extra-time property that is missing": {"omit": ["RuntimeRandomizedExtraUSec"]},
    "extra run time in words": {"extra_text": "soon"},
    "an empty extra run time": {"extra_text": ""},
    "zero spelled with a unit": {"extra_text": "0s"},
    "a kill mode property that is missing": {"omit": ["KillMode"]},
    "an empty kill mode": {"kill_mode_text": ""},
    "a stop time property that is missing": {"omit": ["TimeoutStopUSec"]},
    "a stop time in words": {"stop_timeout_text": "soon"},
    "a stop time of zero": {"stop_timeout_text": "0"},
    "a SendSIGKILL property that is missing": {"omit": ["SendSIGKILL"]},
    "an empty SendSIGKILL": {"send_sigkill_text": ""},
    "a Restart property that is missing": {"omit": ["Restart"]},
    "an empty Restart": {"restart_text": ""},
}


@pytest.mark.parametrize("loaded", UNREADABLE.values(), ids=UNREADABLE)
def test_a_unit_whose_state_cannot_be_read_is_refused_never_passed(phone_env, loaded):
    phone_env.set_loaded_unit(UNIT_B, **loaded)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "could not read" in out["message"] and "install-units" in out["message"], out
    assert START not in phone_env.calls("systemctl") and lease_file(phone_env) is None


def test_a_unit_that_cannot_be_looked_at_is_not_started(phone_env):
    phone_env.set_fake("systemctl_fail", "show")  # systemctl cannot answer: no answer is not a pass
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert START not in phone_env.calls("systemctl")
    assert lease_file(phone_env) is None
    assert phone_env.audit("b")[-1]["code"] == "PHONE_DOWN"


# A locked phone whose unit is not installed (the first use of a phone before install-units, or a unit file that was removed):
# systemd has no unit to stop or start, `systemctl stop` fails ("not loaded"), and the cause is not a phone that is down but
# a unit that is missing. The refusal is USAGE and says so, with the way out, the same as for a unit that is wrong.


def test_a_locked_phone_whose_unit_is_not_installed_is_refused_with_the_way_out_not_with_a_failed_stop(phone_env):
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    assert "not installed" in message and "install-units" in message and UNIT_B in message, out
    assert "systemctl --user stop" not in message and "failed" not in message, out  # not the text of a failed systemctl
    assert phone_env.calls("systemctl") == [STOP, LOAD_SHOW]  # the stop was tried, and then it was asked why it failed
    assert lease_file(phone_env) is None  # freed, like any other start that failed
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["holder"]) == ("lease", "USAGE", "agent-1")
    assert (out["data"]["phone"], out["data"]["unit"]) == ("b", UNIT_B)
    code, out = phone_env.run("lease", "b", "--holder", "agent-2")  # not BUSY: nobody holds a phone that never started
    assert (code, out["code"]) == (2, "USAGE"), out


def test_after_install_units_the_phone_that_had_no_unit_is_leased(phone_env):
    for unit in ("orion-phone-a.service", UNIT_B):
        phone_env.set_loaded_unit(unit, not_found=True)  # a first install
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["code"] == "USAGE"
    assert phone_env.run("install-units")[0] == 0  # the advice
    code, out = start_session(phone_env)
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert START in phone_env.calls("systemctl")
    assert lease_file(phone_env)["ready"] is True


def test_a_release_for_a_phone_whose_unit_is_not_installed_says_so_and_keeps_the_lease(phone_env):
    unlocked_session(phone_env)
    phone_env.set_loaded_unit(UNIT_B, not_found=True)  # the unit file went away, and the manager forgot the unit
    phone_env.stop_unit()  # the unit's run is over: nothing runs from it any more
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "not installed" in out["message"] and out["message"].endswith("then release it again"), out
    assert lease_file(phone_env)["holder"] == "agent-1"  # a release that could not stop the phone keeps the lease


# A phone whose unit is not installed can still be running: an emulator started by hand, outside any unit, possibly unlocked.
# With no unit there is nothing to stop, but the looks that end every stop (a best-effort emulator kill, then adb and the
# ports) are still made. A phone shown gone is refused with the way out (install-units), and one still there is PHONE_DOWN,
# naming what still answers, with the same way out added: the lease is never freed on a phone that may still be unlocked.

KILL_FOREIGN = ["-s", "emulator-5556", "emu", "kill"]


def test_an_emulator_started_by_hand_is_shut_down_before_a_missing_unit_is_refused(phone_env):
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    phone_env.set_device("emulator-5556", keyguard="false")  # up and unlocked, outside any unit (there is none)
    phone_env.phone_ports()  # with its console open: that is where the kill is sent
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "not installed" in out["message"] and "install-units" in out["message"], out
    assert KILL_FOREIGN in phone_env.calls("adb"), "the emulator was not told to exit"
    assert "emulator-5556" not in devices(phone_env)  # and it is gone: an unlocked phone is not left running
    assert phone_env.calls("systemctl") == [STOP, LOAD_SHOW] and lease_file(phone_env) is None


def test_a_missing_unit_with_nothing_running_is_refused_as_before(phone_env):
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE") and "not installed" in out["message"], out
    assert any(call[2:] == ["devices"] or call == ["devices"] for call in phone_env.calls("adb")), "adb was not asked"


def test_a_foreign_emulator_that_survives_the_kill_is_phone_down_with_the_way_out_added(phone_env):
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.phone_ports()
    phone_env.set_fake("emu_kill_fail", True)  # the kill is sent to the open console and fails, and the phone stays listed
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    message = out["message"]
    assert "still running outside its unit" in message and "adb still lists emulator-5556" in message, out
    assert "not installed" in message and "install-units" in message and message.endswith("then lease it again"), out
    assert "was stopped" not in message, out  # nothing was stopped: there was no unit
    assert KILL_FOREIGN in phone_env.calls("adb"), "the emulator was not told to exit"
    assert (out["data"]["phone"], out["data"]["serial"], out["data"]["unit"]) == ("b", "emulator-5556", UNIT_B), out
    assert phone_env.calls("systemctl") == [STOP, LOAD_SHOW]
    assert lease_file(phone_env) is None  # the session could not start: freed, as for any failed stop
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"]) == ("lease", "PHONE_DOWN")


def test_a_foreign_phone_that_adb_cannot_see_is_found_by_its_ports_when_its_unit_is_missing(phone_env, listening):
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.set_fake("emu_kill_fail", True)
    phone_env.set_fake("adb_blip", 1)  # adb has just restarted: its one look lists nothing
    listening(5556)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "port 5556 still answers" in out["message"] and "install-units" in out["message"], out
    assert out["data"]["port"] == 5556 and lease_file(phone_env) is None


def test_a_release_for_a_missing_unit_with_the_phone_still_running_keeps_the_lease_and_says_so(phone_env):
    unlocked_session(phone_env)
    phone_env.env["ORION_PHONE_GONE_WAIT"] = "0.3"
    phone_env.set_loaded_unit(UNIT_B, not_found=True)  # the unit file went away, and the manager forgot the unit
    phone_env.stop_unit()  # its run is over; and then an emulator by hand stands where the phone was
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.phone_ports()
    phone_env.set_fake("emu_kill_fail", True)
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "still running outside its unit" in out["message"] and "install-units" in out["message"], out
    assert out["message"].endswith("then release it again"), out
    assert lease_file(phone_env)["holder"] == "agent-1"  # a release that could not see the phone gone keeps the lease


def test_a_release_for_a_missing_unit_shuts_a_foreign_emulator_down_before_it_refuses(phone_env):
    unlocked_session(phone_env)
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    phone_env.stop_unit()
    phone_env.set_device("emulator-5556", keyguard="false")
    phone_env.phone_ports()  # with its console open: that is where the kill is sent
    code, out = phone_env.run("release", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE") and "not installed" in out["message"], out
    assert out["message"].endswith("then release it again"), out
    assert "emulator-5556" not in devices(phone_env) and lease_file(phone_env)["holder"] == "agent-1"


def test_the_stop_and_the_looks_raise_unit_not_installed_for_a_missing_unit_whose_phone_is_gone(phone_env, in_process):
    from orion_phone import phone as phone_module

    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    phones = in_process()
    with pytest.raises(phone_module.UnitNotInstalled) as caught:  # a PhoneError that callers can tell from a failed stop
        phones._stop_and_verify(phones.cfg.phones["b"])
    assert caught.value.code is Code.USAGE and "not installed" in caught.value.message


def test_a_phone_shown_gone_under_an_unsafe_image_is_told_as_shut_down_whatever_its_unit(phone_env, in_process, monkeypatch):
    from orion_phone import phone as phone_module

    phones = in_process()
    config = phones.cfg.phones["b"]

    def gone_but_no_unit(self, phone, retry="lease"):
        raise phone_module.UnitNotInstalled(Code.USAGE, "its unit is not installed", {"phone": phone.name})

    monkeypatch.setattr(phone_module.Phones, "_stop_and_verify", gone_but_no_unit)
    phone_env.set_device("emulator-5556", crypto_type="none", debuggable="1")
    with pytest.raises(phone_module.UnsafeImage) as caught:
        phones._require_safe_image(config, phones.adb(config))
    assert caught.value.code is Code.USAGE and "was shut down and its session ended" in caught.value.message
    assert "could not be shut down" not in caught.value.message


def test_a_unit_that_runs_though_its_file_is_gone_is_stopped_and_then_refused_as_not_installed(phone_env):
    phone_env.set_loaded_unit(UNIT_B, not_found=True)
    phone_env.start_unit_by_hand(UNIT_B)  # booted a phone, outside any session
    code, out = start_session(phone_env)
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "not installed" in out["message"] and "install-units" in out["message"], out
    calls = phone_env.calls("systemctl")
    assert calls == [STOP, CAP_SHOW] and START not in calls  # it could be stopped, and the unit read afterwards is missing
    assert "emulator-5556" not in devices(phone_env)  # stopped first, as a new session always does
    assert lease_file(phone_env) is None


def test_a_stop_that_fails_for_another_reason_stays_a_failed_stop(phone_env):
    phone_env.set_fake("systemctl_fail", "stop")  # the unit is installed: it is some other trouble
    code, out = start_session(phone_env)
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "systemctl --user stop" in out["message"] and "not installed" not in out["message"], out
    assert phone_env.calls("systemctl") == [STOP, LOAD_SHOW]  # it was asked, and the unit is there
    assert lease_file(phone_env) is None


def test_a_phone_switched_to_locked_is_not_started_from_its_old_open_unit_until_install_units_runs(phone_env):
    config = Path(phone_env.env["ORION_PHONE_CONFIG"])
    locked = config.read_text()
    config.write_text(locked.replace('tier = "locked"', 'tier = "open"'))  # b is an open phone for now
    assert phone_env.run("install-units")[0] == 0  # its unit has no cap, and the manager loads it
    config.write_text(locked)  # the operator makes b locked, and does not install again
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "install-units" in out["message"] and START not in phone_env.calls("systemctl")
    assert phone_env.run("install-units")[0] == 0  # the advice: the unit is rewritten with its cap and reloaded
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert START in phone_env.calls("systemctl")


# Units written before they named their kill mode and stop time have the manager's defaults, control-group and 90 s, which are
# enough: they start as before. Where the manager's default stop time is shorter than two graces, such a unit is refused, and
# install-units, which writes the stop time out, is the way out.


def written_as_before_it_named_its_stop(env) -> None:
    assert env.run("install-units")[0] == 0
    unit = Path(env.env["ORION_PHONE_UNIT_DIR"]) / UNIT_B
    lines = unit.read_text().splitlines()
    assert any(line.startswith("KillMode=") for line in lines) and any(line.startswith("TimeoutStopSec=") for line in lines)
    unit.write_text("".join(line + "\n" for line in lines if not line.startswith(("KillMode=", "TimeoutStopSec="))))
    subprocess.run(["systemctl", "--user", "daemon-reload"], env=env.env, check=True)  # the manager loads it as it is


def test_a_unit_written_before_it_named_its_kill_mode_and_stop_time_starts_on_the_managers_defaults(phone_env):
    written_as_before_it_named_its_stop(phone_env)
    code, out = start_session(phone_env)
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert START in phone_env.calls("systemctl")


def test_such_a_unit_is_refused_where_the_managers_default_stop_time_is_too_short(phone_env):
    written_as_before_it_named_its_stop(phone_env)
    phone_env.set_loaded_unit(UNIT_B, stop_timeout=30)  # DefaultTimeoutStopSec=30s in the manager's configuration
    code, out = start_session(phone_env)
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "TimeoutStopUSec=30s" in out["message"] and "install-units" in out["message"], out
    assert START not in phone_env.calls("systemctl")
    assert phone_env.run("install-units")[0] == 0  # the advice: the stop time is written out and loaded
    code, out = start_session(phone_env)
    assert (code, out["data"]["state"]) == (5, "starting"), out


def test_a_cap_other_than_the_one_installed_is_noticed_after_a_config_change(phone_env):
    assert phone_env.run("install-units")[0] == 0  # b is installed with its 120 minute cap
    config = Path(phone_env.env["ORION_PHONE_CONFIG"])
    config.write_text(config.read_text().replace("session_cap_minutes = 120", "session_cap_minutes = 30"))
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "its session cap is 2h" in out["message"] and "30 minutes" in out["message"], out
    assert START not in phone_env.calls("systemctl")


def test_a_systemd_that_does_not_print_the_extra_run_time_is_refused_and_the_message_says_why(phone_env):
    phone_env.set_loaded_unit(UNIT_B, omit=["RuntimeRandomizedExtraUSec"])  # systemd before 250 has no such property
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    assert "RuntimeRandomizedExtraUSec" in message and "got nothing" in message, out
    assert "systemd before 250" in message and "never taken as zero" in message, out
    assert START not in phone_env.calls("systemctl") and lease_file(phone_env) is None


def test_extra_run_time_from_a_drop_in_is_refused_until_the_drop_in_is_gone(phone_env):
    phone_env.set_loaded_unit(UNIT_B, extra=86400)  # a drop-in sets RuntimeRandomizedExtraSec; the unit file is untouched
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "RuntimeRandomizedExtraUSec=1d" in out["message"] and "drop-ins" in out["message"], out
    assert phone_env.run("install-units")[0] == 0  # rewriting the unit file does not remove a drop-in's extra run time
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert START not in phone_env.calls("systemctl")
    phone_env.set_loaded_unit(UNIT_B, extra=0)  # the operator removed the drop-in and reloaded
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert START in phone_env.calls("systemctl")


# A locked phone's unit watches the leases in the state directory it was installed with. A command that uses another one
# is refused before the phone starts: every boot it started would be stopped at the watch's first look, because no lease
# is held in the unit's directory. The unit's state directory is read with the rest of the loaded unit.


def test_a_unit_that_watches_another_state_directory_is_refused_before_any_start(phone_env, tmp_path):
    elsewhere = tmp_path / "install-time-state"
    phone_env.set_loaded_unit(UNIT_B, env_state=str(elsewhere))
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    for mention in ("watches the state directory", str(elsewhere), "this command uses", phone_env.env["ORION_PHONE_STATE"]):
        assert mention in message, out
    assert "install-units" in message and "same environment" in message and UNIT_B in message, out
    assert phone_env.calls("systemctl") == [STOP, CAP_SHOW]  # the old phone stopped, the unit read, and nothing more
    assert lease_file(phone_env) is None and "emulator-5556" not in devices(phone_env)  # freed, and the phone is off
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["holder"]) == ("lease", "USAGE", "agent-1")
    assert (out["data"]["phone"], out["data"]["unit"]) == ("b", UNIT_B)


UNIT_STATE_UNREADABLE = {
    "no state directory in the unit": ({"env_state": None}, "names no state directory"),
    "a state directory that is not absolute": ({"env_state": "state/orion"}, "not an absolute path"),
    "an environment in a format not seen": ({"environment_text": 'ORION_PHONE_STATE="/a b'}, "could not read Environment"),
    "no environment property": ({"omit": ["Environment"]}, "could not read Environment"),
}


@pytest.mark.parametrize("loaded, mention", UNIT_STATE_UNREADABLE.values(), ids=UNIT_STATE_UNREADABLE)
def test_a_unit_whose_state_directory_cannot_be_read_is_refused_never_passed(phone_env, loaded, mention):
    phone_env.set_loaded_unit(UNIT_B, **loaded)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert mention in out["message"] and "install-units" in out["message"], out
    assert START not in phone_env.calls("systemctl") and lease_file(phone_env) is None


def test_the_advice_for_a_unit_that_watches_another_state_directory_works(phone_env, tmp_path):
    phone_env.set_loaded_unit(UNIT_B, env_state=str(tmp_path / "old-state"))
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["code"] == "USAGE"
    assert phone_env.run("install-units")[0] == 0  # from this environment: the unit now names this state directory
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert START in phone_env.calls("systemctl")


def test_a_unit_whose_environment_forges_a_cap_line_is_still_refused_for_its_real_cap(phone_env):
    forged = f"ORION_PHONE_STATE={phone_env.env['ORION_PHONE_STATE']} X=a{chr(0x2028)}RuntimeMaxUSec=2h"
    phone_env.set_loaded_unit(UNIT_B, cap=None, environment_text=forged)  # really no cap; a value says there is one
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "no session cap" in out["message"] and START not in phone_env.calls("systemctl"), out
    assert lease_file(phone_env) is None


def test_a_state_directory_with_awkward_characters_is_the_directory_the_unit_watches(phone_env, tmp_path):
    awkward = tmp_path / ("my phones 50% $HOME it's `x` " + chr(92) + "state")  # a space, %, $, a quote, a backtick, a backslash
    phone_env.env["ORION_PHONE_STATE"] = str(awkward)  # the unit, as the fake installs it, carries the caller's own
    code, out = start_session(phone_env)
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert START in phone_env.calls("systemctl")


def test_a_state_directory_reached_through_a_link_is_the_directory_the_unit_watches(phone_env, tmp_path):
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "link-state"
    link.symlink_to(real)
    phone_env.set_loaded_unit(UNIT_B, env_state=str(real))  # installed through the real path
    phone_env.env["ORION_PHONE_STATE"] = str(link)  # and used through the link
    code, out = start_session(phone_env)
    assert (code, out["data"]["state"]) == (5, "starting"), out


# Only the `locked` reply asks the owner to unlock the phone, and it is reached only after the image has been checked.
# The reply to a boot that has only just started cannot ask: the image cannot be read yet, so on an unsafe image the owner
# would type the PIN before the next lease refuses it. (A new phone has no PIN yet; the runbook's first step swipes.)

def retry_that_finds_the_booting_phone_not_listed(env):
    start_session(env)
    env.remove_device("emulator-5556")  # adb lists nothing for it, though its unit is running: the session starts it again
    return env.run("lease", "b", "--holder", "agent-1")


STARTING_PATHS = {
    "the lease that starts the boot": start_session,
    "a retry that finds the booting phone not listed": retry_that_finds_the_booting_phone_not_listed,
}


@pytest.mark.parametrize("start", STARTING_PATHS.values(), ids=STARTING_PATHS)
def test_the_starting_reply_does_not_ask_the_owner_to_unlock(phone_env, start):
    code, out = start(phone_env)
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    message = out["message"]
    assert "Do not unlock it yet" in message and "lease it again" in message, out
    assert "unlock it on its screen" not in message and "lock screen appears" not in message, out


# The dump also prints text an app controls. A lock-state key inside a line of that text is not the phone's lock state: with the
# real line gone, a forged `mKeyguardShowing=false` must not read as an unlocked phone and let an agent's input reach the screen.

FORGED_DUMP = "  Intent { dat=x mKeyguardShowing=false }\n  label=Notes KeyguardShowing=false\n"


def test_a_forged_unlocked_in_the_middle_of_a_dump_line_does_not_unlock_a_phone_for_a_lease(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown", dump_extra=FORGED_DUMP)  # no real line, and two forged ones
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out  # not OK: not unlocked


def test_a_forged_unlocked_in_the_middle_of_a_dump_line_does_not_let_an_action_reach_the_lock_screen(phone_env):
    unlocked_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown", dump_extra=FORGED_DUMP)
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])  # nothing reached the phone


# The lock state is read from the system's own KeyguardController block only (screen.keyguard_showing). App text that starts a
# whole lock-state line, as a raw newline in an intent's data lets it, is not the system's: with the real block gone or
# misread such a line must not read as an unlocked phone, and beside a real block it makes the dump unknown. The fake prints
# the dump in the shape AOSP gives it: app text among the activity records before the block (`dump_extra`) and after it
# (`dump_after`).

FORGED_WHOLE_LINE = "        Intent { act=android.intent.action.VIEW dat=http://x\n    mKeyguardShowing=false cmp=com.evil/.A }\n"
FORGED_WHOLE_BLOCK = "        Intent { dat=x\n  KeyguardController:\n    mKeyguardShowing=false\n    mAodShowing=false\n"


def test_a_forged_whole_unlocked_line_with_no_real_block_does_not_unlock_a_phone_for_a_lease(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown", dump_extra=FORGED_WHOLE_LINE)  # the real block is missing
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out  # not OK: not unlocked


def test_a_forged_whole_unlocked_line_with_no_real_block_does_not_let_an_action_reach_the_lock_screen(phone_env):
    unlocked_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown", dump_after=FORGED_WHOLE_LINE)
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])  # nothing reached the phone


@pytest.mark.parametrize("where", ["dump_extra", "dump_after"], ids=["before the block", "after the block"])
@pytest.mark.parametrize("real", ["true", "false"])
def test_a_forged_whole_line_beside_the_real_block_makes_the_dump_unknown_for_a_lease(phone_env, where, real):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard=real, **{where: FORGED_WHOLE_LINE})
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out  # locked, whatever the block says


@pytest.mark.parametrize("where", ["dump_extra", "dump_after"], ids=["before the block", "after the block"])
def test_a_forged_whole_line_beside_the_real_block_does_not_let_an_action_reach_the_phone(phone_env, where):
    unlocked_session(phone_env)  # the owner unlocked it: the real block says false
    phone_env.set_device("emulator-5556", **{where: FORGED_WHOLE_LINE})
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])


def test_a_forged_whole_block_after_the_real_locked_one_is_never_read_in_its_place(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="true", dump_after=FORGED_WHOLE_BLOCK)  # the last header is the forged one
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out  # not OK: not unlocked


def test_an_unforged_dump_still_reads_the_owners_unlock(phone_env):
    unlocked_session(phone_env)  # the real block says false, and nothing else in the dump is a lock-state line
    assert phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")[0] == 0


# The pinned image is Android 16. Its dump holds a second line for the lock state, before the system's block: the display
# policy prints `isKeyguardShowing=`, and the fake prints it as that image does (the block's value, and false when the block is
# missing, so that a reader taking it for the lock state would read an unknown dump as unlocked). It is not read: the block
# decides, whatever that line says.

def test_the_display_policys_line_does_not_lock_a_phone_whose_block_says_unlocked(phone_env):
    start_session(phone_env)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")  # the lock screen shows, and is seen
    assert (code, out["data"]["state"]) == (5, "locked"), out
    phone_env.set_device("emulator-5556", keyguard="false", display_policy="true")
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0  # the owner's unlock reads as one


def test_the_display_policys_line_does_not_unlock_a_phone_whose_block_says_locked(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="true", display_policy="false")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "locked"), out


def test_the_display_policys_line_does_not_unlock_a_phone_whose_block_is_missing(phone_env):
    unlocked_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown", display_policy="false")  # the fake's default for a missing block
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])  # nothing reached the phone


# The fake prints the per-display line of the block exactly as the pinned image does (three leading spaces, and two before
# `Occluded`), so that what the command line tests run on is what the image says and not a stand-in.
REAL_PER_DISPLAY_LINE = (
    "   KeyguardShowing=%s AodShowing=false KeyguardGoingAway=false DismissalRequested=false  Occluded=false "
    "DismissingKeyguardActivity=null TurnScreenOnActivity=null at display=0\n"
)


@pytest.mark.parametrize("value", ["true", "false"])
def test_the_fake_prints_the_per_display_line_exactly_as_the_pinned_image_does(value):
    assert REAL_PER_DISPLAY_LINE % value in fake_adb.activity_dump({"keyguard": value})


# dumpsys cuts a dump at its timeout and still exits 0, saying so in the dump. A cut dump may hold only text an app controls.
CUT_DUMP = "    mKeyguardShowing=false\n*** SERVICE 'activity' DUMP TIMEOUT (10000ms) EXPIRED ***\n"


def test_a_dump_that_timed_out_with_a_forged_false_does_not_unlock_a_phone_for_a_lease(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown", dump_extra=CUT_DUMP)  # no real block: it was never printed
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out


def test_a_dump_that_timed_out_with_a_forged_false_does_not_let_an_action_reach_the_lock_screen(phone_env):
    unlocked_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="unknown", dump_extra=CUT_DUMP)
    before = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "unknown"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[before:])


def test_a_forged_unlocked_beside_the_real_locked_line_is_still_locked(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="true", dump_extra=FORGED_DUMP)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "locked"), out


def test_the_locked_reply_still_asks_the_owner_to_unlock(phone_env):
    start_session(phone_env)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "locked"), out
    assert "its owner must unlock it on its screen" in out["message"], out  # the image was checked: now it asks


@pytest.mark.parametrize(
    "state, fields",
    [
        ("booting", {"booted": False}), ("unknown", {"keyguard": "unknown"}), ("no-screen-lock", {"credential": "NONE"}),
        ("unknown", {"lock_settings": ""}),  # the screen lock cannot be read
    ],
)
def test_the_other_waiting_replies_do_not_ask_for_an_unlock_either(phone_env, state, fields):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", **fields)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, state), out
    assert "unlock" not in out["message"].lower(), out


# Every start of a locked phone's unit begins a session: the old phone stopped for certain, the loaded unit read, then the
# start. A lease attempt that finds its own session's boot already there starts nothing, even when it then finds the
# phone not running: either adb does not list a booting phone yet, or the unit stopped (the session cap, a crash) between
# the look at the unit and the look at the phone. A start there would run with no stop-and-verify and no cap check, and
# the session's boot on record would not be the one running. It answers `starting`; the next attempt, whose boot is gone,
# begins a new session in full.


def units_running(env) -> list[str]:
    return sorted(json.loads(env.fake_state.read_text()).get("invocations", {}))


def test_a_retry_that_finds_the_booting_phone_not_listed_starts_nothing(phone_env):
    code, out = retry_that_finds_the_booting_phone_not_listed(phone_env)
    assert (code, out["data"]["state"]) == (5, "starting"), out
    assert phone_env.calls("systemctl") == COLD_BOOT + [SHOW]  # the first lease's cold boot, then only the look at its boot
    assert phone_env.calls("systemctl").count(START) == 1


def test_a_unit_that_stops_between_the_two_looks_is_not_started_without_the_cap_check(phone_env):
    start_session(phone_env)
    before = len(phone_env.calls("systemctl"))
    phone_env.set_fake("stop_after_show", True)  # the unit stops (cap expiry, a crash) right after the lease looks at it
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["state"]) == (5, "NEEDS_OPERATOR", "starting"), out
    assert phone_env.calls("systemctl")[before:] == [SHOW]  # the look and nothing else: no start without a stop and a cap check
    assert units_running(phone_env) == [] and "emulator-5556" not in devices(phone_env)  # nothing runs
    assert "Do not unlock it yet" in out["message"], out
    assert lease_file(phone_env)["holder"] == "agent-1"  # the session is still the holder's, to begin again


def test_the_attempt_after_a_unit_stopped_begins_a_new_session_in_full(phone_env):
    start_session(phone_env)
    before = len(phone_env.calls("systemctl"))
    phone_env.set_fake("stop_after_show", True)
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"]["state"] == "starting"
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"]["state"]) == (5, "starting"), out
    # the look at the boot on record finds it gone, then the whole path: stop, read the unit, start, bind. Nothing skipped
    assert phone_env.calls("systemctl")[before + 1 :] == [SHOW, *COLD_BOOT]
    assert lease_file(phone_env)["ready"] is True and units_running(phone_env) == [UNIT_B]


def test_a_unit_that_stopped_between_the_looks_is_not_started_from_a_stale_unit_at_the_next_attempt(phone_env):
    start_session(phone_env)
    phone_env.set_fake("stop_after_show", True)
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"]["state"] == "starting"
    phone_env.set_loaded_unit(UNIT_B, cap=None)  # the unit it would have come back from has lost its cap
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "no session cap" in out["message"] and "install-units" in out["message"], out
    assert phone_env.calls("systemctl").count(START) == 1  # only the first session's start ever happened
    assert units_running(phone_env) == [] and lease_file(phone_env) is None


def test_an_open_phone_that_is_not_running_is_still_down_and_never_started(phone_env):
    phone_env.remove_device("emulator-5554")
    code, out = phone_env.run("lease", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert not any(call[:2] == ["--user", "start"] for call in phone_env.calls("systemctl"))


# The action twin of the refusal that cannot stop the phone: an agent that acts without leasing again after the boot
# meets the same refusal, and the session is just as over, whatever the shutdown's own code is.


def test_an_action_refused_for_its_image_that_cannot_stop_the_phone_tells_both_and_still_frees_the_lease(phone_env):
    boot_from_image(phone_env, "file", "1", keyguard="false")
    phone_env.set_fake("systemctl_fail", "stop")
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out  # still running: the stop's own code
    assert "ro.debuggable reports '1'" in out["message"] and "could not be shut down" in out["message"], out
    assert out["data"]["unit"] == "orion-phone-b.service" and out["data"]["phone"] == "b"
    assert not any(c.startswith("input") for c in phone_env.shell_calls())  # the tap never reached the phone
    assert lease_file(phone_env) is None  # the session is over: the next one stops and boots the phone afresh anyway
    last = phone_env.audit("b")[-1]
    assert (last["action"], last["code"], last["args"]) == ("tap", "PHONE_DOWN", {"x": 5, "y": 5})
    assert last["reported"]["ro.debuggable"] == "'1'" and last["reported"]["ro.crypto.type"] == "'file'"


# A cold boot leaves the display asleep, so the owner a lease asks to unlock the phone would face a dark screen. The lease
# that finds the lock screen wakes the display: a wake shows the lock screen and never dismisses one over a screen lock.
# Only a lease does. An action refused on a locked phone still sends the phone nothing, and a wake that fails changes
# nothing the look decided.
def test_a_lease_that_finds_the_lock_screen_wakes_the_display_for_its_owner(phone_env):
    start_session(phone_env)
    sent = len(phone_env.shell_calls())
    assert phone_env.run("lease", "b", "--holder", "agent-1")[1]["data"].get("state") == "locked"
    assert [c for c in phone_env.shell_calls()[sent:] if c.startswith("input")] == ["input keyevent KEYCODE_WAKEUP"]


def test_an_action_on_a_phone_that_locked_again_sends_it_nothing_not_even_a_wake(phone_env):
    unlocked_session(phone_env)
    phone_env.set_device("emulator-5556", keyguard="true")  # its screen locked again during the session
    sent = len(phone_env.shell_calls())
    code, out = phone_env.run("tap", "b", "5", "5", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "locked"), out
    assert not any(c.startswith("input") for c in phone_env.shell_calls()[sent:])


def test_a_wake_that_dismissed_the_lock_screen_never_lets_the_boot_count_as_seen_locked(phone_env):
    # The wake comes before the look that records the sighting. A wake cannot dismiss a lock screen over a screen lock, but
    # nothing relies on that: were the lock screen gone after the wake, the look would not see it, the boot would never
    # count as seen locked, and the lease would start the phone again instead of asking for an unlock that already happened.
    start_session(phone_env)
    phone_env.set_device("emulator-5556", wake_unlocks=True)
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "starting"), out  # restarted, never "locked"
    assert phone_env.calls("systemctl").count(START) == 2  # the session's boot, then the restart


def test_a_wake_that_fails_changes_nothing_the_lease_decided(phone_env):
    start_session(phone_env)
    phone_env.set_device("emulator-5556", fail="input keyevent KEYCODE_WAKEUP")
    code, out = phone_env.run("lease", "b", "--holder", "agent-1")
    assert (code, out["data"].get("state")) == (5, "locked"), out
    assert lease_file(phone_env)["seen_locked"]  # the sighting is recorded, so the owner's unlock counts
    phone_env.set_device("emulator-5556", fail="", keyguard="false")  # the owner unlocks it from the dark screen
    assert phone_env.run("lease", "b", "--holder", "agent-1")[0] == 0
