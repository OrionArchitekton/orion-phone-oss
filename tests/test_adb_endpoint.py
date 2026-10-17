"""Every adb call orion-phone makes names the address of the adb server it runs, so no environment sends one elsewhere.

The server listens on loopback port 5037 (`-L tcp:localhost:5037`). An adb client that is given no `-L` follows
`ADB_SERVER_SOCKET`, `ANDROID_ADB_SERVER_ADDRESS` or `ANDROID_ADB_SERVER_PORT` instead: to another server, or to an address with
none. Where the host drops a connect to a port nobody listens on (WSL2's mirrored networking), that call waits out its time
limit although the managed server is healthy; on ordinary Linux a refused connect makes the client start an unmanaged server of
its own. The fake adb stands for the managed server and fails a call that goes anywhere else, so the tests below bind to what a
call does as well as to how it is spelled.
"""

from __future__ import annotations

import json
import subprocess

import pytest

SERVER = ["-L", "tcp:localhost:5037"]  # what every call starts with: the address the managed server listens on
HOLDER = "agent-1"
UNIT_B = "orion-phone-b.service"

# Each variable adb's client reads, set to an endpoint where nothing listens, and all three at once.
OTHER_ENDPOINTS = {
    "ADB_SERVER_SOCKET": "tcp:localhost:5099",
    "ANDROID_ADB_SERVER_ADDRESS": "192.0.2.1",  # a documentation address (RFC 5737): nothing is ever there
    "ANDROID_ADB_SERVER_PORT": "5099",
}
ENVIRONMENTS = {**{name: {name: value} for name, value in OTHER_ENDPOINTS.items()}, "all three": OTHER_ENDPOINTS}


def fake_adb(env, *args: str, **variables: str) -> subprocess.CompletedProcess:
    """The adb the tests' config names, called as a client would be, with `variables` added to the CLI runner's environment."""
    return subprocess.run(
        [str(env.root / "bin" / "adb"), *args], capture_output=True, text=True, env={**env.env, **variables}, timeout=30
    )


# The fake is what makes the tests below more than a spelling check, so its own contract is pinned first.


@pytest.mark.parametrize("variable", OTHER_ENDPOINTS)
def test_the_fake_adb_follows_an_endpoint_variable_unless_it_is_told_where_to_go(phone_env, variable):
    other = {variable: OTHER_ENDPOINTS[variable]}
    assert fake_adb(phone_env, "devices").returncode == 0  # no variable: the default endpoint, where the server is
    assert fake_adb(phone_env, "devices", **other).returncode != 0  # sent to an endpoint with no server
    assert fake_adb(phone_env, *SERVER, "devices", **other).returncode == 0  # told where the server is, it reads no variable


def test_the_fake_adb_fails_a_call_that_names_another_server_whatever_it_asks_for(phone_env):
    assert fake_adb(phone_env, "-L", "tcp:localhost:5099", "devices").returncode != 0
    kill = fake_adb(phone_env, "-L", "tcp:localhost:5099", "-s", "emulator-5554", "emu", "kill")
    assert kill.returncode != 0
    assert "emulator-5554" in json.loads(phone_env.fake_state.read_text())["devices"]  # the kill was never served
    assert phone_env.calls("adb") == []  # nothing that went elsewhere is a served call
    assert [call[:2] for call in phone_env.calls("adb_raw")] == [["-L", "tcp:localhost:5099"]] * 2  # though it was seen


def test_the_fake_adb_logs_a_served_call_without_its_server_address_and_the_raw_call_with_it(phone_env):
    words = ["-s", "emulator-5554", "shell", "getprop sys.boot_completed"]
    assert fake_adb(phone_env, *SERVER, *words).returncode == 0
    assert phone_env.calls("adb") == [words]
    assert phone_env.calls("adb_raw") == [[*SERVER, *words]]


# What orion-phone does with it, from the command line, as a phone is used: a command that is sent the wrong way ends in a
# failure here, and a call that does not begin with the server's address is reported by what it began with.


def run(env, *args: str, code: int = 0) -> dict:
    got, out = env.run(*args)
    assert got == code, f"{args}: exit {got}, expected {code}: {out}"
    return out


def verbs(calls: list[list[str]]) -> set[str]:
    """The adb command of each call, read after the server's address and any `-s SERIAL`."""
    return {call[4] if call[2] == "-s" else call[2] for call in calls}


def status(env) -> None:
    run(env, "status", "a")


def an_open_phone(env) -> None:
    run(env, "lease", "a", "--holder", HOLDER)
    run(env, "tap", "a", "10", "20", "--holder", HOLDER)  # a shell command, and the screenshot taken after it
    run(env, "shot", "a", "--holder", HOLDER)
    run(env, "ui", "a", "--holder", HOLDER)  # the read of the screen that is held to a size
    run(env, "release", "a", "--holder", HOLDER)


def a_locked_phones_session(env) -> None:
    run(env, "lease", "b", "--holder", HOLDER, code=5)  # the cold boot
    assert run(env, "lease", "b", "--holder", HOLDER, code=5)["data"]["state"] == "locked"  # the lock screen shows
    env.set_device("emulator-5556", keyguard="false")  # the owner types the PIN
    run(env, "lease", "b", "--holder", HOLDER)
    run(env, "key", "b", "HOME", "--holder", HOLDER)
    run(env, "release", "b", "--holder", HOLDER)


def an_emulator_that_is_told_to_exit(env) -> None:
    """The kill that ends a stop: a phone running outside its unit, with its console open, which a lease is refused for and
    first tells to exit."""
    env.set_loaded_unit(UNIT_B, not_found=True)
    env.set_device("emulator-5556", keyguard="false")
    env.phone_ports()
    run(env, "lease", "b", "--holder", HOLDER, code=2)
    assert "emulator-5556" not in json.loads(env.fake_state.read_text())["devices"]  # the kill reached the emulator


FLOWS = {  # what a person does, and the adb commands that must have been among its calls (or the flow proves nothing)
    "status": (status, {"devices", "shell"}),
    "an open phone driven": (an_open_phone, {"devices", "shell", "exec-out"}),
    "a locked phone's session": (a_locked_phones_session, {"devices", "shell"}),
    "an emulator told to exit": (an_emulator_that_is_told_to_exit, {"devices", "emu"}),
}


@pytest.mark.parametrize("variables", ENVIRONMENTS.values(), ids=ENVIRONMENTS)
@pytest.mark.parametrize("flow, expected", FLOWS.values(), ids=FLOWS)
def test_no_endpoint_variable_sends_an_adb_call_elsewhere(phone_env, flow, expected, variables):
    phone_env.env.update(variables)
    flow(phone_env)  # each command ends as it does with no variable set: the fake fails a call that goes elsewhere
    calls = phone_env.calls("adb_raw")
    assert calls, "no adb call was made"
    assert [call for call in calls if call[:2] != SERVER] == []  # each names the server first, ahead of -s and the command
    assert expected <= verbs(calls), sorted(verbs(calls))
