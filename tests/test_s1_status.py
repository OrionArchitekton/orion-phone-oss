"""S1: status reports healthy only with positive proof (booted AND a non-blank fresh frame)."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from orion_phone import config
from orion_phone.adb import Adb
from orion_phone.codes import Code, PhoneError
from orion_phone.phone import Phones


def test_healthy_phone_reports_ok_with_positive_proof(phone_env):
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (0, "OK")
    assert out["data"]["healthy"] is True
    shell = phone_env.shell_calls()
    assert "getprop sys.boot_completed" in shell
    assert "input keyevent KEYCODE_WAKEUP" in shell
    calls = phone_env.calls()
    wake = calls.index(["-s", "emulator-5554", "shell", "input keyevent KEYCODE_WAKEUP"])
    assert calls.index(["-s", "emulator-5554", "exec-out", "screencap"]) > wake  # a sleeping screen is blank


def test_missing_phone_is_phone_down(phone_env):
    phone_env.remove_device("emulator-5554")
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (6, "PHONE_DOWN")
    assert not [c for c in phone_env.calls() if c[:1] == ["-s"]]  # nothing was sent to the phone


SEPARATORS = {
    "a line separator": chr(0x2028), "a paragraph separator": chr(0x2029), "a next line character": chr(0x85),
}


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_listing_row_forged_through_another_serial_does_not_make_a_missing_phone_run(phone_env, separator):
    phone_env.remove_device("emulator-5554")
    # another device whose serial holds a separator and then a row that names this phone: one line of adb's output
    phone_env.set_fake("adb_raw_rows", [f"usb-1234{separator}emulator-5554\tdevice"])
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert not [c for c in phone_env.calls() if c[:1] == ["-s"]]  # nothing was sent to a phone that is not there


def test_a_listing_cut_short_by_a_carriage_return_does_not_make_a_missing_phone_run(phone_env):
    # the row that names the phone ends the listing, and a carriage return is where its newline should be
    phone_env.set_fake("adb_devices_raw", "List of devices attached\nemulator-5554\tdevice\r")
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert not [c for c in phone_env.calls() if c[:1] == ["-s"]]  # nothing was sent to a phone that is not shown running


def test_a_listing_whose_rows_end_in_crlf_is_read_as_clean(phone_env):
    phone_env.set_fake("adb_devices_raw", "List of devices attached\r\nemulator-5554\tdevice\r\n\r\n")
    assert phone_env.run("status", "a")[0] == 0


def test_a_phone_listed_twice_is_not_called_healthy(phone_env):
    phone_env.set_fake("adb_raw_rows", ["emulator-5554\tdevice"])  # the real row, and another that says the same
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "more than once" in out["message"], out
    assert not [c for c in phone_env.calls() if c[:1] == ["-s"]]


@pytest.mark.parametrize("text", ["1" + chr(0x2028), "1" + chr(0x85), "1 ", " 1", "1" + chr(0xA0)], ids=repr)
def test_a_boot_flag_is_not_made_to_read_one_by_what_is_trimmed_from_it(phone_env, text):
    phone_env.set_device("emulator-5554", boot_text=text)
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (7, "UNHEALTHY"), out
    assert "has not finished booting" in out["message"], out


def test_a_boot_flag_that_ends_in_a_carriage_return_is_one(phone_env):
    phone_env.set_device("emulator-5554", boot_text="1" + chr(13))  # a pty gives \r\n for \n
    assert phone_env.run("status", "a")[0] == 0


def test_offline_phone_is_phone_down(phone_env):
    phone_env.set_device("emulator-5554", state="offline")
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (6, "PHONE_DOWN")
    assert not [c for c in phone_env.calls() if c[:1] == ["-s"]]  # nothing was sent to the phone


def test_not_booted_is_unhealthy(phone_env):
    phone_env.set_device("emulator-5554", booted=False)
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (7, "UNHEALTHY")


def test_blank_frame_is_unhealthy(phone_env):
    phone_env.set_device("emulator-5554", screen="blank")
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (7, "UNHEALTHY")


def test_listing_all_phones_carries_each_code(phone_env):
    code, out = phone_env.run("status")
    assert code == 0
    assert {p["phone"]: p["code"] for p in out["data"]["phones"]} == {"a": "OK", "b": "PHONE_DOWN"}


def test_unknown_phone_is_usage(phone_env):
    code, out = phone_env.run("status", "z")
    assert (code, out["code"]) == (2, "USAGE")


def test_missing_adb_is_usage(phone_env):
    (phone_env.root / "bin" / "adb").unlink()
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (2, "USAGE")


def test_state_dir_is_owner_only(phone_env):
    phone_env.run("status", "a")
    assert Path(phone_env.env["ORION_PHONE_STATE"]).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("screen", ["garbled", "empty"])
def test_unreadable_frame_is_unhealthy(phone_env, screen):
    phone_env.set_device("emulator-5554", screen=screen)
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (7, "UNHEALTHY")


def test_failed_adb_call_is_phone_down(phone_env):
    phone_env.set_device("emulator-5554", fail="getprop")
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (6, "PHONE_DOWN")
    assert "adb failed" in out["message"]


def test_adb_timeout_is_phone_down(tmp_path):
    slow = tmp_path / "adb"
    slow.write_text("#!/bin/sh\nexec sleep 5\n")
    slow.chmod(0o755)
    with pytest.raises(PhoneError) as err:
        Adb(slow, "emulator-5554", timeout=0.2).shell("getprop sys.boot_completed")
    assert err.value.code is Code.PHONE_DOWN
    assert "timed out" in err.value.message


def test_listing_without_adb_is_usage(phone_env):
    (phone_env.root / "bin" / "adb").unlink()
    code, out = phone_env.run("status")
    assert (code, out["code"]) == (2, "USAGE")
    assert "phone" not in out["data"]  # the setup is broken for every phone, so the error names none of them


# An answer must always be one JSON object with a real code. These faults used to escape as a traceback and exit 1.
@pytest.mark.parametrize(
    "fault", ["adb_not_executable", "adb_is_a_directory", "adb_missing_interpreter", "state_is_a_file"]
)
def test_broken_setup_is_usage_json(phone_env, fault):
    adb = phone_env.root / "bin" / "adb"
    named, fix = adb, "android.adb"
    if fault == "adb_not_executable":
        adb.chmod(0o644)
    elif fault == "adb_is_a_directory":
        adb.unlink()
        adb.mkdir()
    elif fault == "adb_missing_interpreter":
        adb.write_text("#!/no/such/interpreter\n")  # still mode 755, so only the interpreter lookup fails
    else:
        named, fix = Path(phone_env.env["ORION_PHONE_STATE"]), "ORION_PHONE_STATE"
        named.write_text("not a directory")
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(named) in out["message"] and fix in out["message"], out  # names the path and the setting to fix


def test_adb_in_an_unsearchable_directory_is_usage(phone_env):
    locked = phone_env.root / "locked"
    locked.mkdir()
    adb = locked / "adb"
    adb.write_text("#!/bin/sh\n")
    adb.chmod(0o755)
    cfg = Path(phone_env.env["ORION_PHONE_CONFIG"])
    cfg.write_text(cfg.read_text().replace(str(phone_env.root / "bin" / "adb"), str(adb)))
    locked.chmod(0)
    try:
        if os.access(adb, os.F_OK):
            pytest.skip("this account can search a mode 000 directory (root, or a filesystem without modes)")
        code, out = phone_env.run("status", "a")
    finally:
        locked.chmod(0o700)  # let pytest clean up the temp tree
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(adb) in out["message"] and "android.adb" in out["message"], out


def test_state_dir_owned_by_someone_else_is_usage(tmp_path, monkeypatch):
    state = tmp_path / "state"
    cfg = config.parse({"phones": {"a": {"tier": "open", "console_port": 5554}}}, tmp_path / "config.toml", state)

    def refuse(path, mode):
        raise PermissionError(errno.EPERM, "Operation not permitted")  # what chmod says about another user's directory

    monkeypatch.setattr(os, "chmod", refuse)
    with pytest.raises(PhoneError) as err:
        Phones(cfg)
    assert err.value.code is Code.USAGE
    assert str(state) in err.value.message and "ORION_PHONE_STATE" in err.value.message


# Which phone an answer is about must survive every failure, including a failed adb call (which knows only its argv).
def test_adb_failure_result_still_names_the_phone(phone_env):
    phone_env.set_device("emulator-5556", fail="getprop")
    code, out = phone_env.run("status", "b")
    assert (code, out["code"]) == (6, "PHONE_DOWN")
    assert {key: out["data"][key] for key in ("phone", "tier", "serial")} == {
        "phone": "b",
        "tier": "locked",
        "serial": "emulator-5556",
    }


def test_listing_marks_each_phone_healthy_or_not(phone_env):
    phone_env.set_device("emulator-5556", fail="getprop")  # b answers `devices`, then its next call fails
    code, out = phone_env.run("status")
    assert code == 0
    entries = {p["phone"]: p for p in out["data"]["phones"]}
    assert (entries["a"]["code"], entries["a"]["healthy"]) == ("OK", True)
    assert (entries["b"]["code"], entries["b"]["healthy"]) == ("PHONE_DOWN", False)
    assert (entries["b"]["tier"], entries["b"]["serial"]) == ("locked", "emulator-5556")
