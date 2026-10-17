"""S2: lease an open phone, drive it, release it. Every action is lease-checked and audited."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from orion_phone.audit import Audit
from orion_phone.phone import PNG_SIGNATURE, write_private


def lease(env, phone: str = "a", holder: str = "agent-1"):
    return env.run("lease", phone, "--holder", holder)


def test_action_without_a_lease_is_refused_and_touches_nothing(phone_env):
    code, out = phone_env.run("tap", "a", "10", "20", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER")
    assert not any(c.startswith("input") for c in phone_env.shell_calls())


def test_leased_tap_runs_and_is_audited_with_a_screenshot(phone_env):
    assert lease(phone_env)[0] == 0
    code, out = phone_env.run("tap", "a", "10", "20", "--holder", "agent-1")
    assert code == 0
    assert "input tap 10 20" in phone_env.shell_calls()
    records = phone_env.audit("a")
    assert [r["action"] for r in records] == ["lease", "tap"]
    tap = records[1]
    assert (tap["holder"], tap["args"], tap["code"]) == ("agent-1", {"x": 10, "y": 20}, "OK")
    assert Path(tap["screenshot"]).read_bytes().startswith(b"\x89PNG")
    assert out["data"]["screenshot"] == tap["screenshot"]


def test_release_ends_the_lease(phone_env):
    lease(phone_env)
    assert phone_env.run("release", "a", "--holder", "agent-1")[0] == 0
    code, out = phone_env.run("tap", "a", "1", "1", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER")
    assert [r["action"] for r in phone_env.audit("a")] == ["lease", "release"]


def test_release_by_someone_else_is_refused(phone_env):
    lease(phone_env)
    code, out = phone_env.run("release", "a", "--holder", "agent-2")
    assert (code, out["code"]) == (4, "NOT_HOLDER")


def test_missing_holder_is_usage_before_any_state_exists(phone_env):
    code, out = phone_env.run("lease", "a")
    assert (code, out["code"]) == (2, "USAGE")
    assert not Path(phone_env.env["ORION_PHONE_STATE"]).exists()  # holder is checked before config or state


def test_leasing_a_phone_that_is_down_is_phone_down(phone_env):
    phone_env.remove_device("emulator-5554")
    code, out = phone_env.run("lease", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN")


def test_out_of_range_tap_is_usage_and_runs_nothing(phone_env):
    lease(phone_env)
    code, out = phone_env.run("tap", "a", "10", "-5", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE")
    assert not any(c.startswith("input") for c in phone_env.shell_calls())


def test_state_files_are_owner_only(phone_env):
    old = os.umask(0o022)  # a common default: owner-only must not depend on the operator's umask
    try:
        lease(phone_env)
        phone_env.run("tap", "a", "1", "1", "--holder", "agent-1")
    finally:
        os.umask(old)
    state = Path(phone_env.env["ORION_PHONE_STATE"])
    paths = [state, *state.rglob("*")]
    assert any(p.is_file() for p in paths)
    assert [str(p) for p in paths if p.stat().st_mode & 0o077] == []


def test_damaged_lease_file_is_usage_naming_the_file(phone_env):
    leases = Path(phone_env.env["ORION_PHONE_STATE"]) / "leases"
    leases.mkdir(parents=True)
    (leases / "a.json").write_text("")
    code, out = phone_env.run("lease", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE")
    assert str(leases / "a.json") in out["message"]


def test_failed_action_is_reported_and_audited(phone_env):
    lease(phone_env)
    phone_env.set_device("emulator-5554", fail="input tap")
    code, out = phone_env.run("tap", "a", "1", "1", "--holder", "agent-1")
    assert (code, out["code"], out["data"]["phone"]) == (6, "PHONE_DOWN", "a")
    assert phone_env.audit("a")[-1]["code"] == "PHONE_DOWN"


def test_unreadable_screenshot_is_unhealthy_never_ok(phone_env):
    lease(phone_env)
    phone_env.set_device("emulator-5554", screen="garbled")
    code, out = phone_env.run("tap", "a", "1", "1", "--holder", "agent-1")
    assert (code, out["code"]) == (7, "UNHEALTHY")
    assert phone_env.audit("a")[-1]["code"] == "UNHEALTHY"


def test_typed_text_is_quoted_so_it_cannot_run_device_commands(phone_env):
    lease(phone_env)
    code, _ = phone_env.run("type", "a", "--holder", "agent-1", "--", "hello; reboot")
    assert code == 0
    assert "input text 'hello;%sreboot'" in phone_env.shell_calls()


def test_non_ascii_text_is_usage_and_types_nothing(phone_env):
    lease(phone_env)
    code, out = phone_env.run("type", "a", "--holder", "agent-1", "--", "café")
    assert (code, out["code"]) == (2, "USAGE")
    assert not any(c.startswith("input text") for c in phone_env.shell_calls())


@pytest.mark.parametrize(
    "args, expected",
    [
        (("key", "a", "back"), "input keyevent KEYCODE_BACK"),
        (("swipe", "a", "1", "2", "3", "4", "--ms", "250"), "input swipe 1 2 3 4 250"),
        (
            ("open", "a", "https://example.com/x?y=1"),
            "am start -a android.intent.action.VIEW -d 'https://example.com/x?y=1'",
        ),
        (("open", "a", "com.example.app"), "monkey -p com.example.app -c android.intent.category.LAUNCHER 1"),
        (("key", "a", "KEYCODE_WAKEUP"), "input keyevent KEYCODE_WAKEUP"),
        (("swipe", "a", "0", "0", "10000", "10000", "--ms", "10000"), "input swipe 0 0 10000 10000 10000"),
        (("type", "a", "a" * 1000), "input text " + "a" * 1000),
        (("type", "a", "~"), "input text '~'"),
        (("open", "a", "com." + "a" * 251), f"monkey -p com.{'a' * 251} -c android.intent.category.LAUNCHER 1"),
    ],
)
def test_actions_send_exact_device_commands(phone_env, args, expected):
    lease(phone_env)
    code, _ = phone_env.run(*args, "--holder", "agent-1")
    assert code == 0
    assert expected in phone_env.shell_calls()


@pytest.mark.parametrize(
    "args",
    [
        ("key", "a", "rm -rf"),
        ("open", "a", "javascript:alert(1)"),
        ("open", "a", "com.example; reboot"),
        ("open", "a", "com.example.app\n"),
        ("key", "a", "BACK\n"),
        ("swipe", "a", "1", "2", "3", "4", "--ms", "0"),
        # No row may use "--": the "--holder" appended below would become a positional and fail for the wrong reason.
        ("open", "a", "https://example.com/\n"),
        ("open", "a", "https://example.com/it's"),  # no quote characters, even though the URL is shell-quoted
        ("open", "a", 'https://example.com/say"hi'),
        ("open", "a", "https://example.com/a" + chr(92) + "b"),  # and no backslash
        ("open", "a", "https://example.com/" + "a" * 2000),
        ("open", "a", "com." + "a" * 252),  # longer than any installable package name
        ("swipe", "a", "1", "2", "3", "4", "--ms", "10001"),  # a swipe ends well inside adb's 30 s timeout
        ("swipe", "a", "1", "2", "3", "-4"),
        ("swipe", "a", "10001", "2", "3", "4"),
        ("type", "a", ""),
        ("type", "a", "a" * 1001),
        ("type", "a", "line1\nline2"),  # a newline would press ENTER through input text
        ("type", "a", "tab\there"),
        ("type", "a", "del\x7f"),
        ("key", "a", chr(0x131)),  # dotless i (U+0131): upper() would make it KEYCODE_I
    ],
)
def test_bad_arguments_are_usage_and_run_nothing(phone_env, args):
    assert "--" not in args  # see the note in the rows: such a row would fail for the wrong reason
    lease(phone_env)
    code, out = phone_env.run(*args, "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE")
    assert not any(c.startswith(("input", "am ", "monkey")) for c in phone_env.shell_calls())


def test_shot_saves_a_png_and_returns_its_path(phone_env):
    lease(phone_env)
    code, out = phone_env.run("shot", "a", "--holder", "agent-1")
    assert code == 0
    assert Path(out["data"]["path"]).read_bytes().startswith(b"\x89PNG")


def test_ui_returns_compact_text_marked_untrusted(phone_env):
    lease(phone_env)
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert code == 0
    lines = out["data"]["text"].splitlines()
    assert lines[0].startswith("# UI dump: untrusted screen content")
    assert '[0] EditText "Search" id=search clickable center=540,260' in lines
    assert '[1] ImageButton desc="Settings" clickable center=970,120' in lines
    assert Path(out["data"]["path"]).exists()


def device_dump(env) -> Path:
    """Where the fake adb keeps phone a's UI dump file: what a dump leaves behind on the phone."""
    return env.root / "emulator-5554-orion_ui.xml"


# uiautomator exits 0 when it writes nothing ("could not get idle state" on an animating screen), so a failed
# dump must read as unreadable, never as an earlier screen with stale tap centers.
def test_a_silently_failed_ui_dump_never_returns_the_previous_screen(phone_env):
    lease(phone_env)
    assert phone_env.run("ui", "a", "--holder", "agent-1")[0] == 0
    phone_env.set_device("emulator-5554", ui_dump="silent-fail")
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (7, "UNHEALTHY")


def test_a_dump_left_on_the_phone_is_never_returned_when_the_new_dump_fails(phone_env):
    lease(phone_env)
    stale = '<hierarchy><node text="Stale" class="android.widget.Button" clickable="true" bounds="[0,0][9,9]" /></hierarchy>'
    device_dump(phone_env).write_text(stale)  # left behind by a read that never finished
    phone_env.set_device("emulator-5554", ui_dump="silent-fail")
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (7, "UNHEALTHY")
    assert "Stale" not in str(out)  # neither returned nor named: a dump that was not written is never read
    saved = (Path(phone_env.env["ORION_PHONE_STATE"]) / "ui" / "a").glob("*.xml")
    assert not any(b"Stale" in f.read_bytes() for f in saved)  # nor saved
    assert not device_dump(phone_env).exists()  # removed before the dump, so it could never be taken for a new one


# Right after a window changes (an app starting), Android's accessibility bridge can have no window to give uiautomator, which
# then writes nothing, prints "ERROR: null root node returned by UiTestAutomationBridge." on stderr, and still exits 0
# (measured on the pinned image: a dump at once after an app's start wrote nothing, one a second later succeeded, under heavy
# host load too). So a dump that wrote nothing is tried once more, after a pause, and one that writes nothing again is
# UNHEALTHY in the tool's own words, with what to do next.
DUMP = re.compile(  # the marker is new for every call, so nothing the phone shows can print it ahead
    r"rm -f /data/local/tmp/orion_ui\.xml && uiautomator dump /data/local/tmp/orion_ui\.xml 2>&1 "
    r"&& test -s /data/local/tmp/orion_ui\.xml && echo (orion-ui-dump-written-[0-9a-f]{16}) \|\| true"
)


def dumps(env) -> list[str]:
    """The marker of each UI dump command sent to the phone, in order."""
    return [found.group(1) for found in map(DUMP.fullmatch, env.shell_calls()) if found]


def test_a_dump_that_writes_nothing_once_is_tried_again_and_the_screen_returned(phone_env):
    lease(phone_env)
    phone_env.set_device("emulator-5554", ui_dump_failures=1)
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (0, "OK"), out
    assert '"Search"' in out["data"]["text"]  # the screen, read on the second try
    markers = dumps(phone_env)
    assert len(markers) == 2 and markers[0] != markers[1]  # a new marker for each try


def test_a_dump_that_writes_nothing_twice_says_why_in_the_tools_own_words(phone_env):
    lease(phone_env)
    phone_env.set_device("emulator-5554", ui_dump="silent-fail")
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (7, "UNHEALTHY"), out
    assert "null root node returned by UiTestAutomationBridge" in out["message"], out
    assert "ui again" in out["message"] and "shot" in out["message"], out  # what to do next
    assert len(dumps(phone_env)) == 2  # tried once more, and no more


def test_a_marker_that_is_not_the_one_this_call_sent_never_counts_as_a_written_dump(phone_env):
    lease(phone_env)
    # uiautomator writes nothing but prints the marker's fixed part as its last line: only the call's own marker counts
    phone_env.set_device("emulator-5554", ui_dump="silent-fail", ui_dump_said="orion-ui-dump-written\n")
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (7, "UNHEALTHY"), out
    assert len(dumps(phone_env)) == 2


def test_ui_leaves_no_screen_text_on_the_phone(phone_env):
    lease(phone_env)
    assert phone_env.run("ui", "a", "--holder", "agent-1")[0] == 0
    assert not device_dump(phone_env).exists()  # it holds other apps' text: removed once read


@pytest.mark.parametrize(
    "xml",
    ["<hierarchy><node", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><hierarchy/>'],
    ids=["malformed", "dtd"],
)
def test_unreadable_ui_dump_is_unhealthy_and_audited(phone_env, xml):
    lease(phone_env)
    phone_env.set_device("emulator-5554", ui_xml=xml)
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (7, "UNHEALTHY")
    assert Path(out["data"]["path"]).exists()
    assert phone_env.audit("a")[-1]["code"] == "UNHEALTHY"


def a_dump_of(size: int) -> str:
    """A well-formed UI dump of exactly `size` bytes: one node with a very long text."""
    head, tail = '<hierarchy><node text="', '" class="android.widget.TextView" clickable="true" bounds="[0,0][2,2]" /></hierarchy>'
    return head + "a" * (size - len(head) - len(tail)) + tail


UI_LIMIT = 4 * 1024 * 1024  # the documented size (README): a dump over it is refused


def test_the_ui_dump_limit_is_the_documented_four_mebibytes():
    from orion_phone.phone import UI_DUMP_MAX_BYTES

    assert UI_DUMP_MAX_BYTES == UI_LIMIT


# A hostile screen can inflate its own dump, and an agent can call ui in a loop: a dump over the limit is refused
# before it is saved or parsed, so it cannot fill the state directory.
@pytest.mark.parametrize("extra, code, name", [(0, 0, "OK"), (1, 7, "UNHEALTHY")], ids=["at the limit", "one byte over"])
def test_a_ui_dump_over_the_size_limit_is_refused_before_it_is_saved(phone_env, extra, code, name):
    lease(phone_env)
    phone_env.set_device("emulator-5554", ui_xml=a_dump_of(UI_LIMIT + extra))
    got, out = phone_env.run("ui", "a", "--holder", "agent-1", timeout=120)
    assert (got, out["code"]) == (code, name), out
    saved = list((Path(phone_env.env["ORION_PHONE_STATE"]) / "ui" / "a").glob("*"))
    last = phone_env.audit("a")[-1]
    if not extra:
        assert len(saved) == 1 and last["ui"] == out["data"]["path"]
        return
    assert saved == []  # not written, so nothing to fill the disk with
    assert f"more than {UI_LIMIT} bytes" in out["message"]  # not an exact size: the rest was never read
    assert "not saved" in out["message"] and "hostile" in out["message"] and "shot" in out["message"]
    assert out["data"] == {"phone": "a", "limit": UI_LIMIT}
    assert (last["action"], last["code"], last["holder"], last["screenshot"]) == ("ui", "UNHEALTHY", "agent-1", None)
    assert "ui" not in last  # no saved dump to name


# The size is checked while the dump is read, not after: a screen whose dump never ends must not make the CLI read
# until adb's own time limit (30 s) and hold everything it was sent in memory meanwhile.
def test_a_ui_dump_that_never_ends_is_refused_as_soon_as_it_passes_the_limit(phone_env):
    lease(phone_env)
    phone_env.set_device("emulator-5554", ui_dump_stream="endless")  # the fake adb streams the dump without stopping
    started = time.monotonic()
    code, out = phone_env.run("ui", "a", "--holder", "agent-1", timeout=60)
    took = time.monotonic() - started
    assert (code, out["code"]) == (7, "UNHEALTHY"), out
    assert took < 10, f"the dump was read for {took:.0f} s: the limit did not stop the read"
    assert f"more than {UI_LIMIT} bytes" in out["message"] and "not saved" in out["message"]
    assert not list((Path(phone_env.env["ORION_PHONE_STATE"]) / "ui" / "a").glob("*"))
    assert not device_dump(phone_env).exists()  # adb was stopped before the phone's own rm: the CLI removed the dump
    assert phone_env.audit("a")[-1]["code"] == "UNHEALTHY"


def test_a_failing_adb_while_the_ui_dump_is_read_is_phone_down_with_its_message(phone_env):
    lease(phone_env)
    phone_env.set_device("emulator-5554", fail="cat")  # adb exits 1 for the read ("error: closed"), not for the dump
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert (code, out["code"]) == (6, "PHONE_DOWN"), out
    assert "adb failed" in out["message"] and "closed" in out["message"]
    assert phone_env.audit("a")[-1]["code"] == "PHONE_DOWN"


def test_ui_audit_record_names_the_saved_dump(phone_env):
    lease(phone_env)
    code, out = phone_env.run("ui", "a", "--holder", "agent-1")
    assert code == 0
    assert phone_env.audit("a")[-1]["ui"] == out["data"]["path"]


def test_an_action_pushes_the_lease_expiry_out(phone_env):
    lease(phone_env)
    path = Path(phone_env.env["ORION_PHONE_STATE"]) / "leases" / "a.json"
    before = json.loads(path.read_text())["expires_at"]
    assert phone_env.run("tap", "a", "1", "1", "--holder", "agent-1")[0] == 0
    assert json.loads(path.read_text())["expires_at"] > before


def test_the_screenshot_is_taken_after_the_action(phone_env):
    lease(phone_env)
    phone_env.run("tap", "a", "10", "20", "--holder", "agent-1")
    device = [" ".join(c[2:]) for c in phone_env.calls() if c[:1] == ["-s"]]
    assert device.index("shell input tap 10 20") < device.index("exec-out screencap -p")


def test_the_audit_record_carries_time_and_phone(phone_env):
    lease(phone_env)
    phone_env.run("tap", "a", "1", "1", "--holder", "agent-1")
    record = phone_env.audit("a")[-1]
    assert record["phone"] == "a"
    assert abs(time.time() - datetime.fromisoformat(record["ts"]).timestamp()) < 60


def test_a_failure_after_the_phone_was_touched_is_still_audited(phone_env):
    lease(phone_env)
    (Path(phone_env.env["ORION_PHONE_STATE"]) / "shots").write_bytes(b"")  # the screenshot cannot be saved
    code, out = phone_env.run("tap", "a", "7", "7", "--holder", "agent-1")
    assert (code, out["code"]) == (1, "INTERNAL")
    assert "input tap 7 7" in phone_env.shell_calls()
    last = phone_env.audit("a")[-1]
    assert (last["action"], last["args"], last["code"], last["screenshot"]) == ("tap", {"x": 7, "y": 7}, "INTERNAL", None)


def test_an_action_that_cannot_be_audited_is_never_reported_as_done(phone_env):
    lease(phone_env)
    audit = Path(phone_env.env["ORION_PHONE_STATE"]) / "audit"
    shutil.rmtree(audit)
    audit.write_bytes(b"")  # no record can be written: the error must reach the agent, never OK
    code, out = phone_env.run("tap", "a", "7", "7", "--holder", "agent-1")
    assert (code, out["code"]) == (1, "INTERNAL")
    assert str(audit) in out["message"]


def test_a_short_write_is_an_error_never_a_truncated_screenshot(tmp_path, short_write):
    assert short_write(64, lambda: write_private(tmp_path / "shot.png", PNG_SIGNATURE + bytes(5000))) == "raised"


def test_a_short_write_is_an_error_never_a_truncated_audit_record(tmp_path, short_write):
    audit = Audit(tmp_path, clock=lambda: 1_700_000_000.0)  # one day file throughout
    audit.record("a", "agent-1", "lease")
    size = (tmp_path / "audit" / "a" / "2023-11-14.jsonl").stat().st_size
    assert short_write(size + 20, lambda: audit.record("a", "agent-1", "tap")) == "raised"


def test_screenshots_and_audit_records_are_synced_whole(tmp_path, monkeypatch):
    synced, real_fsync = [], os.fsync

    def spy(fd):
        synced.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    write_private(tmp_path / "shot.png", PNG_SIGNATURE)
    Audit(tmp_path, clock=lambda: 1_700_000_000.0).record("a", "agent-1", "tap")
    assert synced == [len(PNG_SIGNATURE), (tmp_path / "audit" / "a" / "2023-11-14.jsonl").stat().st_size]


def test_one_holders_concurrent_actions_never_overlap_on_the_phone(phone_env):
    lease(phone_env)
    procs = [phone_env.popen("tap", "a", "1", str(i), "--holder", "agent-1") for i in range(6)]
    for proc in procs:
        proc.communicate(timeout=60)
    assert [proc.returncode for proc in procs] == [0] * 6
    device = ["input" if c[3].startswith("input") else c[3] for c in phone_env.calls() if c[:1] == ["-s"]]
    assert device == ["input", "screencap -p"] * 6  # every screenshot shows exactly one tap


def test_a_failed_action_that_cannot_be_audited_surfaces_the_audit_error(phone_env):
    lease(phone_env)
    audit = Path(phone_env.env["ORION_PHONE_STATE"]) / "audit"
    shutil.rmtree(audit)
    audit.write_bytes(b"")  # no record can be written
    phone_env.set_device("emulator-5554", fail="input tap")  # and the action fails first
    code, out = phone_env.run("tap", "a", "7", "7", "--holder", "agent-1")
    assert (code, out["code"]) == (1, "INTERNAL")  # the lost record is reported, never hidden behind PHONE_DOWN
    assert str(audit) in out["message"]


def test_a_record_after_a_failed_partial_append_starts_on_a_fresh_line(tmp_path, short_write):
    audit = Audit(tmp_path, clock=lambda: 1_700_000_000.0)
    audit.record("a", "agent-1", "lease")
    day = tmp_path / "audit" / "a" / "2023-11-14.jsonl"
    assert short_write(day.stat().st_size + 20, lambda: audit.record("a", "agent-1", "tap")) == "raised"
    assert not day.read_bytes().endswith(b"\n")  # the failed append left half a line behind
    audit.record("a", "agent-1", "key")
    lines = day.read_text().splitlines()
    assert len(lines) == 3  # the torn half stays on its own line instead of swallowing the next record
    assert [json.loads(lines[0])["action"], json.loads(lines[-1])["action"]] == ["lease", "key"]


def test_a_successful_type_keeps_its_text_in_the_audit(phone_env):
    lease(phone_env)
    assert phone_env.run("type", "a", "--holder", "agent-1", "--", "hello")[0] == 0
    last = phone_env.audit("a")[-1]
    assert (last["action"], last["code"], last["args"]) == ("type", "OK", {"text": "hello"})  # kept for post review


@pytest.mark.parametrize("fault, expected", [("adb", "PHONE_DOWN"), ("garbled", "UNHEALTHY"), ("shots", "INTERNAL")])
def test_a_failed_type_audits_only_the_text_length(phone_env, fault, expected):
    lease(phone_env)
    state = Path(phone_env.env["ORION_PHONE_STATE"])
    if fault == "adb":
        phone_env.set_device("emulator-5554", fail="input text")  # the phone refuses the input
    elif fault == "garbled":
        phone_env.set_device("emulator-5554", screen="garbled")  # typed, then the screenshot is unreadable
    else:
        (state / "shots").write_bytes(b"")  # typed, then the screenshot cannot be saved
    code, out = phone_env.run("type", "a", "--holder", "agent-1", "--", "hunter2")
    assert out["code"] == expected
    assert "hunter2" not in "".join(f.read_text() for f in (state / "audit" / "a").glob("*.jsonl"))
    last = phone_env.audit("a")[-1]
    assert (last["code"], last["args"]) == (expected, {"text_length": 7})


@pytest.mark.parametrize("other_holds", [False, True], ids=["free", "held-by-another"])
@pytest.mark.parametrize(
    "args",
    [("tap", "a", "10", "-5"), ("open", "a", "com.example; reboot"), ("type", "a", "a" * 1001)],
    ids=["tap", "open", "type"],
)
def test_malformed_arguments_without_the_lease_are_not_holder(phone_env, args, other_holds):
    if other_holds:
        assert lease(phone_env, holder="agent-2")[0] == 0
    code, out = phone_env.run(*args, "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER")  # the lease is checked first, as act() documents
    assert not [c for c in phone_env.calls() if c[:1] == ["-s"]]  # nothing reached the phone


def test_rejected_arguments_from_the_holder_are_audited(phone_env):
    lease(phone_env)
    code, out = phone_env.run("open", "a", "com.example; reboot", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE")
    assert not [c for c in phone_env.calls() if c[:1] == ["-s"]]  # nothing reached the phone
    last = phone_env.audit("a")[-1]
    assert (last["action"], last["code"], last["args"]) == ("open", "USAGE", {"target": "com.example; reboot"})


def test_a_rejected_type_is_audited_with_only_the_text_length(phone_env):
    lease(phone_env)
    secret = "hunter2" * 200  # 1400 characters: too long to type
    code, out = phone_env.run("type", "a", secret, "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE")
    state = Path(phone_env.env["ORION_PHONE_STATE"])
    assert "hunter2" not in "".join(f.read_text() for f in (state / "audit" / "a").glob("*.jsonl"))
    last = phone_env.audit("a")[-1]
    assert (last["action"], last["code"], last["args"]) == ("type", "USAGE", {"text_length": 1400})
