"""The CLI contract: one JSON object on stdout, and the exit code equals the result code."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from orion_phone import cli

LEASED_COMMANDS = [
    ["lease", "a"],
    ["release", "a"],
    ["shot", "a"],
    ["ui", "a"],
    ["tap", "a", "1", "2"],
    ["type", "a", "x"],
    ["swipe", "a", "1", "2", "3", "4"],
    ["key", "a", "HOME"],
    ["open", "a", "https://example.com"],
]
OTHER_COMMANDS = [["status"], ["status", "a"], ["install-units"], ["setup"], ["_run", "a"]]


def test_unknown_command_is_usage_json(phone_env):
    code, out = phone_env.run("frobnicate")
    assert (code, out["code"], out["ok"]) == (2, "USAGE", False)


PUBLIC_COMMANDS = {
    "status", "lease", "release", "shot", "ui", "tap", "type", "swipe", "key", "open", "init", "install-units", "setup",
}


def test_help_lists_every_public_command_and_never_the_internal_one(phone_env):
    # Plain text, not JSON: the runner always asks for JSON, so this calls the CLI itself. `_run` is what the units
    # start, not a command for people: it must not be listed, and argparse must not print its SUPPRESS marker.
    proc = subprocess.run(
        [sys.executable, "-m", "orion_phone", "--help"], capture_output=True, text=True, env=phone_env.env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "_run" not in proc.stdout and "SUPPRESS" not in proc.stdout, proc.stdout
    rows = set(re.findall(r"^    (\S+)\s{2,}\S", proc.stdout, re.M))  # one indented row per listed command
    assert rows == PUBLIC_COMMANDS
    choices = re.search(r"\{([^}]*)\}", proc.stdout)  # the usage line's list of commands
    assert choices and set(choices.group(1).split(",")) == PUBLIC_COMMANDS


def test_missing_argument_is_usage_json(phone_env):
    code, out = phone_env.run("tap", "a", "10", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE")


def test_non_integer_coordinate_is_usage_json(phone_env):
    code, out = phone_env.run("tap", "a", "ten", "20", "--holder", "agent-1")
    assert (code, out["code"]) == (2, "USAGE")


# The config is removed in both tests below. A missing config is also USAGE, so which complaint comes back
# shows whether the holder check ran first (leased commands) or never ran (everything else).
@pytest.mark.parametrize("argv", LEASED_COMMANDS, ids=lambda argv: argv[0])
def test_every_leased_command_requires_a_holder(phone_env, argv):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).unlink()
    code, out = phone_env.run(*argv)
    assert (code, out["code"]) == (2, "USAGE")
    assert "--holder" in out["message"]


@pytest.mark.parametrize("argv", OTHER_COMMANDS, ids=lambda argv: " ".join(argv))
def test_other_commands_do_not_ask_for_a_holder(phone_env, argv):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).unlink()
    code, out = phone_env.run(*argv)
    assert (code, out["code"]) == (2, "USAGE")
    assert "--holder" not in out["message"] and "no config at" in out["message"]


HOLDER_RULE = "holder must be 1 to 64 characters"
BAD_HOLDERS = {
    "space": "a b",
    "65 characters": "a" * 65,
    "semicolon": "x;y",
    "embedded newline": "x\ny",
    "trailing newline": "ok\n",  # fullmatch, not match or $: a trailing newline must not slip through
}
GOOD_HOLDERS = ["mcp-0123abcd", "smoke-20260930", "dan-setup", "a" * 64, "Ab9._:@-x"]


# The holder ends up in lease files and audit lines, so its format is checked before anything else runs. The config
# is removed in these tests too: which complaint comes back shows whether the holder check ran first.
@pytest.mark.parametrize("holder", list(BAD_HOLDERS.values()), ids=list(BAD_HOLDERS))
def test_a_holder_with_a_bad_format_is_usage(phone_env, holder):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).unlink()
    code, out = phone_env.run("lease", "a", "--holder", holder)
    assert (code, out["code"]) == (2, "USAGE")
    assert HOLDER_RULE in out["message"]


@pytest.mark.parametrize("argv", LEASED_COMMANDS, ids=lambda argv: argv[0])
def test_every_leased_command_checks_the_holder_format(phone_env, argv):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).unlink()
    code, out = phone_env.run(*argv, "--holder", "x;y")
    assert (code, out["code"]) == (2, "USAGE")
    assert HOLDER_RULE in out["message"]


@pytest.mark.parametrize("holder", GOOD_HOLDERS)
def test_ordinary_holder_names_pass_the_format_check(phone_env, holder):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).unlink()  # passing the holder check means the config complaint comes next
    code, out = phone_env.run("lease", "a", "--holder", holder)
    assert (code, out["code"]) == (2, "USAGE")
    assert HOLDER_RULE not in out["message"] and "no config at" in out["message"]


def test_the_holder_format_applies_to_the_environment_variable_too(phone_env):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).unlink()
    phone_env.env["ORION_PHONE_HOLDER"] = "x;y"
    code, out = phone_env.run("lease", "a")
    assert (code, out["code"]) == (2, "USAGE")
    assert HOLDER_RULE in out["message"]


def boom(argv):
    raise RuntimeError("boom")


# A bug must not break the contract: still one JSON object and a real exit code, with the traceback on stderr.
def test_an_unexpected_error_still_prints_one_json_object(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run", boom)
    assert cli.main(["--json", "status"]) == 1
    captured = capsys.readouterr()
    out = json.loads(captured.out)  # exactly one JSON document and nothing else on stdout
    assert (out["code"], out["exit"], out["ok"]) == ("INTERNAL", 1, False)
    assert "RuntimeError: boom" in out["message"]
    assert "Traceback" in captured.err and "RuntimeError: boom" in captured.err  # the bug stays diagnosable


def test_an_unexpected_error_in_text_mode_names_the_code_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run", boom)
    assert cli.main(["status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "INTERNAL" in captured.err


def test_the_catch_all_does_not_swallow_version_or_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "orion-phone" in capsys.readouterr().out
