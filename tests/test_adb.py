"""Adb's bounded read: it must stop a runaway output, and otherwise keep the contract of Adb._run."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from orion_phone.adb import Adb, OutputOverLimit
from orion_phone.codes import Code, PhoneError


def stub_adb(tmp_path: Path, body: str) -> Path:
    """A stand-in adb: a script that runs `body`. It is called as adb -L tcp:localhost:5037 -s SERIAL exec-out COMMAND, so
    sys.argv[6] is the command; PID_FILE in the body is a file it may write its pid to, and ARGV_FILE one it may append the
    arguments of its call to (see ARGV)."""
    script = textwrap.dedent(body).replace("PID_FILE", repr(str(tmp_path / "pid")))
    script = script.replace("ARGV_FILE", repr(str(tmp_path / "argv")))
    path = tmp_path / "adb"
    # signal.alarm: a stub that outlives its test (blocked on a pipe nobody reads, say) ends by itself, never hangs the run
    path.write_text(f"#!{sys.executable}\nimport json, os, signal, sys, time\nsignal.alarm(10)\n{script}")
    path.chmod(0o755)
    return path


def gone(tmp_path: Path) -> bool:
    """True when the process the stub recorded no longer exists: it was killed and reaped, not left running."""
    try:
        os.kill(int((tmp_path / "pid").read_text()), 0)
    except ProcessLookupError:
        return True
    return False


STREAM = """
open(PID_FILE, "w").write(str(os.getpid()))
chunk = b"x" * 65536
for _ in range(800):  # about 50 MiB over 4 s, unless it is stopped
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
    time.sleep(0.005)
"""


def test_a_read_stops_and_kills_adb_the_moment_the_output_passes_the_limit(tmp_path):
    adb = Adb(stub_adb(tmp_path, STREAM), "emulator-5554")
    started = time.monotonic()
    with pytest.raises(OutputOverLimit) as caught:
        adb.exec_out_limited("cat x", 1000)
    assert time.monotonic() - started < 2  # the stub would have gone on for 4 s
    assert caught.value.limit == 1000
    assert gone(tmp_path)


def test_output_exactly_at_the_limit_is_returned_and_one_byte_more_is_refused(tmp_path):
    adb = Adb(stub_adb(tmp_path, "sys.stdout.buffer.write(b'x' * int(sys.argv[6]))"), "emulator-5554")
    assert adb.exec_out_limited("1000", 1000) == b"x" * 1000
    with pytest.raises(OutputOverLimit):
        adb.exec_out_limited("1001", 1000)


def test_a_chatty_stderr_never_blocks_the_read(tmp_path):
    body = "sys.stderr.buffer.write(b'e' * 2_000_000); sys.stderr.flush(); sys.stdout.buffer.write(b'0123456789')"
    adb = Adb(stub_adb(tmp_path, body), "emulator-5554", timeout=10)
    started = time.monotonic()
    assert adb.exec_out_limited("x", 100) == b"0123456789"  # a pipe nobody reads fills at 64 KiB and stalls adb
    assert time.monotonic() - started < 5


def test_adb_that_outlasts_the_time_limit_is_phone_down_and_is_killed(tmp_path):
    adb = Adb(stub_adb(tmp_path, "open(PID_FILE, 'w').write(str(os.getpid())); time.sleep(30)"), "emulator-5554", timeout=0.5)
    started = time.monotonic()
    with pytest.raises(PhoneError) as caught:
        adb.exec_out_limited("x", 100)
    assert caught.value.code is Code.PHONE_DOWN and "timed out" in caught.value.message
    assert caught.value.data == {"args": ["-s", "emulator-5554", "exec-out", "x"]}
    assert time.monotonic() - started < 5
    assert gone(tmp_path)


CALLS = {"shell": lambda adb: adb.shell("x"), "bounded read": lambda adb: adb.exec_out_limited("x", 100)}


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS)
def test_an_adb_that_times_out_says_which_unit_runs_the_server_and_how_to_start_it(tmp_path, call):
    # with no server running, every adb call ends this way under WSL2's mirrored networking: the message is where an
    # operator learns what to start
    adb = Adb(stub_adb(tmp_path, "time.sleep(30)"), "emulator-5554", timeout=1)
    with pytest.raises(PhoneError) as caught:
        call(adb)
    message = caught.value.message
    assert caught.value.code is Code.PHONE_DOWN and message.startswith("adb timed out after 1s"), message
    assert "systemctl --user start orion-phone-adb-server.service" in message, message


# Every call names the adb server's address before its own words. The server listens on loopback port 5037, and a client that is
# given no address follows ADB_SERVER_SOCKET, ANDROID_ADB_SERVER_ADDRESS or ANDROID_ADB_SERVER_PORT: to another server, or to
# one that is not there (where a connect to a port nobody listens on is dropped, the call then waits out its time limit
# although the server is healthy). Given `-L`, adb reads none of the three. The error of a failed call still shows the
# call's own words, as it always has.

ARGV = "open(ARGV_FILE, 'a').write(json.dumps(sys.argv[1:]) + '\\n')"
SERVER = ["-L", "tcp:localhost:5037"]
CALLS_AND_THEIR_WORDS = {
    "a shell command": (lambda adb: adb.shell("getprop x"), ["-s", "emulator-5554", "shell", "getprop x"]),
    "an exec-out": (lambda adb: adb.exec_out("screencap"), ["-s", "emulator-5554", "exec-out", "screencap"]),
    "the bounded read": (lambda adb: adb.exec_out_limited("cat x", 100), ["-s", "emulator-5554", "exec-out", "cat x"]),
    "the kill": (lambda adb: adb.emu_kill(), ["-s", "emulator-5554", "emu", "kill"]),
    "the listing": (lambda adb: adb.is_listed(), ["devices"]),
}


@pytest.mark.parametrize("call, words", CALLS_AND_THEIR_WORDS.values(), ids=CALLS_AND_THEIR_WORDS)
def test_every_call_names_the_adb_server_before_its_own_words(tmp_path, call, words):
    call(Adb(stub_adb(tmp_path, ARGV), "emulator-5554"))
    assert [json.loads(line) for line in (tmp_path / "argv").read_text().splitlines()] == [[*SERVER, *words]]


def test_the_error_of_a_failed_call_shows_its_own_words_and_not_the_servers_address(tmp_path):
    adb = Adb(stub_adb(tmp_path, "sys.exit(1)"), "emulator-5554")
    for call in (lambda: adb.shell("x"), lambda: adb.exec_out_limited("x", 100)):
        with pytest.raises(PhoneError) as caught:
            call()
        assert caught.value.data["args"][0] == "-s"


def test_a_nonzero_exit_is_phone_down_with_adbs_own_words(tmp_path):
    adb = Adb(stub_adb(tmp_path, "sys.stderr.write('error: closed\\n'); sys.exit(1)"), "emulator-5554")
    with pytest.raises(PhoneError) as caught:
        adb.exec_out_limited("x", 100)
    assert caught.value.code is Code.PHONE_DOWN and caught.value.message == "adb failed: error: closed"
    assert caught.value.data == {"args": ["-s", "emulator-5554", "exec-out", "x"]}


def test_a_nonzero_exit_without_a_message_names_its_status(tmp_path):
    adb = Adb(stub_adb(tmp_path, "sys.exit(3)"), "emulator-5554")
    with pytest.raises(PhoneError) as caught:
        adb.exec_out_limited("x", 100)
    assert caught.value.message == "adb failed: exit 3"


def test_a_flood_on_stderr_is_not_kept_whole(tmp_path):
    adb = Adb(stub_adb(tmp_path, "sys.stderr.buffer.write(b'e' * 500_000); sys.exit(1)"), "emulator-5554")
    with pytest.raises(PhoneError) as caught:
        adb.exec_out_limited("x", 100)
    assert caught.value.code is Code.PHONE_DOWN and len(caught.value.message) < 20_000


def test_adb_that_is_missing_or_cannot_run_is_usage(tmp_path):
    with pytest.raises(PhoneError) as missing:
        Adb(tmp_path / "no-such-adb", "emulator-5554").exec_out_limited("x", 100)
    assert missing.value.code is Code.USAGE and "adb not found" in missing.value.message
    plain = tmp_path / "adb"
    plain.write_text("#!/bin/sh\nexit 0\n")  # not executable
    with pytest.raises(PhoneError) as unrunnable:
        Adb(plain, "emulator-5554").exec_out_limited("x", 100)
    assert unrunnable.value.code is Code.USAGE and "cannot run adb" in unrunnable.value.message


def test_adb_is_never_given_the_callers_stdin(tmp_path):
    adb = Adb(stub_adb(tmp_path, "sys.stdout.write(os.readlink('/proc/self/fd/0'))"), "emulator-5554")
    assert adb.exec_out_limited("x", 100) == b"/dev/null"  # adb forwards its stdin to the phone


# `adb devices` lists one `serial<TAB>state` line per device, and the serial of a device is not ours to trust: it can hold
# a line or paragraph separator, which `str.splitlines` and `str.split` also break on, and so forge a line that names our
# phone. The listing is read on newlines and tabs only, and a phone listed twice cannot be told from a forged line.

SERIAL = "emulator-5556"


def listing_adb(tmp_path: Path, listing: str) -> Adb:
    return Adb(stub_adb(tmp_path, f"sys.stdout.write({listing!r})"), SERIAL)


def devices(*rows: str) -> str:
    return "List of devices attached\n" + "".join(row + "\n" for row in rows) + "\n"


@pytest.mark.parametrize(
    "rows, running, listed",
    [
        ([f"{SERIAL}\tdevice"], True, True),
        ([f"{SERIAL}\toffline"], False, True),
        ([f"{SERIAL}\tunauthorized"], False, True),
        (["emulator-5554\tdevice"], False, False),
        ([], False, False),
        (["emulator-5554\tdevice", f"{SERIAL}\tdevice"], True, True),
        ([f"{SERIAL}x\tdevice"], False, False),  # another serial that starts the same
    ],
    ids=["device", "offline", "unauthorized", "another phone", "nothing", "two phones", "a longer serial"],
)
def test_the_listing_says_whether_this_phone_is_running_and_whether_it_is_listed(tmp_path, rows, running, listed):
    adb = listing_adb(tmp_path, devices(*rows))
    assert (adb.is_running(), adb.is_listed()) == (running, listed)


def test_a_listing_with_the_lines_ends_a_terminal_would_give_is_read_the_same(tmp_path):
    adb = listing_adb(tmp_path, "* daemon not running; starting now at tcp:5037\r\n* daemon started successfully\r\n"
                      + "List of devices attached\r\n" + f"{SERIAL}\tdevice\r\n\r\n")
    assert adb.is_running() and adb.is_listed()


SEPARATORS = {"a line separator": chr(0x2028), "a paragraph separator": chr(0x2029), "a next line character": chr(0x85)}


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_serial_with_a_separator_in_it_cannot_forge_a_line_that_names_this_phone(tmp_path, separator):
    forged = f"usb-1234{separator}{SERIAL}\tdevice"  # one device, whose serial is made to look like two lines
    adb = listing_adb(tmp_path, devices(forged))
    assert (adb.is_running(), adb.is_listed()) == (False, False)


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_separator_inside_a_field_does_not_split_the_field(tmp_path, separator):
    adb = listing_adb(tmp_path, devices(f"{SERIAL}{separator}x\tdevice"))  # not our serial: a different one
    assert (adb.is_running(), adb.is_listed()) == (False, False)


def test_a_phone_listed_twice_is_refused_as_running_and_still_counts_as_listed(tmp_path):
    adb = listing_adb(tmp_path, devices(f"{SERIAL}\toffline", f"{SERIAL}\tdevice"))  # which one is the phone?
    with pytest.raises(PhoneError) as caught:
        adb.is_running()
    assert caught.value.code is Code.PHONE_DOWN and "more than once" in caught.value.message, caught.value.message
    assert adb.is_listed()  # whichever line is real, the phone is there: a release must not take it for gone


# A line end is a newline, or a carriage return and a newline: one carriage return, no more. A second is part of the state, which
# is then not one this knows, so the phone is listed (it is there) and not running.

@pytest.mark.parametrize(
    "ending, running",
    [("", True), ("\r", True), ("\r\r", False), ("\r\r\r", False), (" ", False), ("\rx", False)],
    ids=["a newline", "one carriage return", "two carriage returns", "three", "a space", "a carriage return and a letter"],
)
def test_one_carriage_return_ends_a_row_and_nothing_else_is_trimmed_from_its_state(tmp_path, ending, running):
    adb = listing_adb(tmp_path, devices(f"{SERIAL}\tdevice{ending}"))
    assert (adb.is_running(), adb.is_listed()) == (running, True)


# A carriage return is part of a line end only immediately before a newline. A listing that stops short of its newline, with a
# carriage return where it should be, ends in a row that no newline ended: its state keeps the carriage return, so it is not a
# state this knows (the phone is listed, and not running).

LISTING_HEAD = "List of devices attached\n"


@pytest.mark.parametrize(
    "listing, running",
    [
        (LISTING_HEAD + f"{SERIAL}\tdevice\n\n", True),
        (LISTING_HEAD + f"{SERIAL}\tdevice\r\n\r\n", True),
        (LISTING_HEAD + f"{SERIAL}\tdevice\r\n", True),
        (LISTING_HEAD + f"{SERIAL}\tdevice\n", True),
        (LISTING_HEAD + f"{SERIAL}\tdevice\r", False),
        (LISTING_HEAD + f"emulator-5554\tdevice\n{SERIAL}\tdevice\r", False),
        (LISTING_HEAD + f"{SERIAL}\tdevice\r\r", False),
    ],
    ids=["newlines", "CR LF", "CR LF last row", "newline last row", "last row ends in a lone CR", "last of two rows ends in a CR",
         "last row ends in two CRs"],
)
def test_the_last_row_keeps_a_carriage_return_that_no_newline_follows(tmp_path, listing, running):
    adb = listing_adb(tmp_path, listing)
    assert (adb.is_running(), adb.is_listed()) == (running, True)


def test_a_state_that_is_not_recognized_is_not_a_state_it_could_be_made_into(tmp_path):
    adb = listing_adb(tmp_path, devices(f"{SERIAL}\toffline\r\r"))
    assert (adb.is_running(), adb.is_listed()) == (False, True)
