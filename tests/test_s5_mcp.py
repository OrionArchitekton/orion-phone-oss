"""S5: the MCP tools mirror the CLI agent commands and go through the CLI, so leases hold."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
from conftest import NOT_INHERITED
from mcp import Client, StdioServerParameters
from mcp.types import ImageContent

from orion_phone import cli, mcp_server, screen


@pytest.fixture
def mcp_env(phone_env, monkeypatch):
    """The CLI runner's environment, applied to this process: the in-memory server's CLI calls inherit it."""
    for key, value in phone_env.env.items():
        monkeypatch.setenv(key, value)
    for key in NOT_INHERITED:  # a developer's ORION_PHONE_HOLDER would stand in for the holder the server passes
        monkeypatch.delenv(key, raising=False)
    return phone_env


def call(name: str, args: dict):
    async def go():
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, args)

    return anyio.run(go)


def test_tool_names_match_the_cli_agent_commands():
    async def go():
        async with Client(mcp_server.mcp) as client:
            return await client.list_tools()

    assert {tool.name for tool in anyio.run(go).tools} == set(cli.AGENT_COMMANDS)


def test_action_before_lease_is_a_tool_error_carrying_the_code(mcp_env):
    result = call("tap", {"phone": "a", "x": 1, "y": 2})
    assert result.is_error
    assert "NOT_HOLDER" in result.content[0].text


def test_lease_tap_shot_release_round_trip(mcp_env):
    assert not call("lease", {"phone": "a"}).is_error
    assert not call("tap", {"phone": "a", "x": 1, "y": 2}).is_error
    assert "input tap 1 2" in mcp_env.shell_calls()
    shot = call("shot", {"phone": "a"})
    assert isinstance(shot.content[0], ImageContent) and shot.content[0].mime_type == "image/png"
    assert not call("release", {"phone": "a"}).is_error


def test_text_starting_with_a_dash_is_typed_not_parsed(mcp_env):
    call("lease", {"phone": "a"})
    assert not call("type", {"phone": "a", "text": "-5 degrees"}).is_error
    assert "input text -5%sdegrees" in mcp_env.shell_calls()  # shlex leaves shell-safe text unquoted


def test_locked_phone_needs_the_operator_through_mcp(mcp_env):
    result = call("lease", {"phone": "b"})
    assert result.is_error and "NEEDS_OPERATOR" in result.content[0].text
    assert "Do not unlock it yet" in result.content[0].text  # the first reply is not a request for the unlock


def test_the_lease_tool_tells_agents_to_relay_an_unlock_request_only_for_a_locked_phone():
    async def go():
        async with Client(mcp_server.mcp) as client:
            return await client.list_tools()

    tool = next(tool for tool in anyio.run(go).tools if tool.name == "lease")
    description = " ".join(tool.description.split())  # an agent reads only the message, which carries no separate state
    assert "starting or still booting" in description and "do not ask anyone to unlock it yet" in description
    assert "says the phone is locked and its owner must unlock it" in description
    # a phone with no screen lock asks its owner to set a PIN on its own screen: that is relayed too, and never done by the agent
    assert "says it has no screen lock and its owner must set one on it" in description
    assert "Never ask anyone for a PIN and never type one." in description


def test_a_phone_another_agent_holds_is_busy_through_mcp(mcp_env):
    mcp_env.run("lease", "a", "--holder", "other-agent")
    result = call("lease", {"phone": "a"})
    assert result.is_error and "BUSY" in result.content[0].text and "other-agent" in result.content[0].text


def test_a_dash_word_without_spaces_is_typed_not_taken_for_an_option(mcp_env):
    """argparse reads any dash text that holds a space as plain text, so only a space-free word needs the `--`."""
    call("lease", {"phone": "a"})
    assert not call("type", {"phone": "a", "text": "-x"}).is_error
    assert "input text -x" in mcp_env.shell_calls()


def test_status_listing_and_the_remaining_tools_reach_the_cli_in_their_own_forms(mcp_env):
    listing = json.loads(call("status", {}).content[0].text)
    assert {entry["phone"] for entry in listing["data"]["phones"]} == {"a", "b"}
    call("lease", {"phone": "a"})
    assert not call("swipe", {"phone": "a", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "ms": 250}).is_error  # --ms before --
    assert not call("key", {"phone": "a", "name": "back"}).is_error
    assert not call("open", {"phone": "a", "target": "https://example.com/x"}).is_error
    shell = mcp_env.shell_calls()
    assert "input swipe 1 2 3 4 250" in shell
    assert "input keyevent KEYCODE_BACK" in shell
    assert "am start -a android.intent.action.VIEW -d https://example.com/x" in shell
    dump = call("ui", {"phone": "a"})
    assert not dump.is_error and dump.content[0].text.splitlines()[0] == screen.UI_HEADER


def test_status_of_an_empty_phone_name_reaches_the_cli_and_is_usage_like_the_cli_says(mcp_env):
    """An empty name is a name (an unknown one), not "no phone": it must not turn into a listing of every phone."""
    code, out = mcp_env.run("status", "")
    assert (code, out["code"]) == (2, "USAGE")
    result = call("status", {"phone": ""})
    assert result.is_error and "USAGE" in result.content[0].text and out["message"] in result.content[0].text


def test_server_serves_over_real_stdio_when_launched_as_a_module(mcp_env):
    """An MCP client config launches `python -m orion_phone.mcp_server`; the in-memory client never reaches that path."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "orion_phone.mcp_server"], env=mcp_env.env)

    async def go():
        with anyio.fail_after(60):  # a server that never answers fails this test instead of hanging the suite
            async with Client(params) as client:
                return await client.list_tools(), await client.call_tool("status", {"phone": "a"})

    tools, status = anyio.run(go)
    assert {tool.name for tool in tools.tools} == set(cli.AGENT_COMMANDS)
    assert not status.is_error and json.loads(status.content[0].text)["data"]["healthy"] is True


def serve(env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "orion_phone.mcp_server"], env=env)


def test_each_server_process_is_its_own_holder_whatever_the_shell_exports(mcp_env):
    """MCP clients may hand a server their whole environment; a shell's holder must not make sessions one holder."""

    async def go():
        with anyio.fail_after(60):
            async with Client(serve({**mcp_env.env, "ORION_PHONE_HOLDER": "shared-shell-name"})) as client:
                return json.loads((await client.call_tool("lease", {"phone": "a"})).content[0].text)

    assert anyio.run(go)["data"]["holder"].startswith("mcp-")


DECOY = 'import json\nprint(json.dumps({"ok": True, "code": "OK", "exit": 0, "message": "decoy", "data": {}}))\n'


@pytest.mark.parametrize("entry", [None, "", "."], ids=["plain", "empty-pythonpath-entry", "dot-pythonpath-entry"])
def test_an_orion_phone_in_the_agents_project_never_answers_for_the_cli(mcp_env, tmp_path, monkeypatch, entry):
    project = tmp_path / "project"
    (project / "orion_phone").mkdir(parents=True)
    (project / "orion_phone" / "__init__.py").write_text("")
    (project / "orion_phone" / "__main__.py").write_text(DECOY)
    monkeypatch.chdir(project)  # MCP clients start a server in the agent's project
    if entry is not None:  # "" is what `export PYTHONPATH=$PYTHONPATH:/x` leaves when PYTHONPATH was empty
        monkeypatch.setenv("PYTHONPATH", entry + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = call("tap", {"phone": "a", "x": 1, "y": 2})
    assert result.is_error and "NOT_HOLDER" in result.content[0].text  # the real CLI answered: nothing is leased


@pytest.mark.parametrize("text", ["a\x00b", "x" * 131_072, "\ud800"], ids=["nul-byte", "over-128-kib", "surrogate"])
def test_text_no_command_line_can_carry_is_a_usage_tool_error(mcp_env, text):
    call("lease", {"phone": "a"})
    result = call("type", {"phone": "a", "text": text})
    assert result.is_error and "USAGE" in result.content[0].text


def test_a_cli_that_never_answers_is_an_internal_tool_error(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_server, "cli_timeout", lambda: 0.001)
    result = call("status", {"phone": "a"})
    assert result.is_error and "INTERNAL" in result.content[0].text and "no answer" in result.content[0].text


# The CLI reads its time limits from the environment, and the server's CLI child inherits it, so the server's own limit
# on a CLI call must follow the same two variables: a slow machine that raises them must not have a legitimate lease
# stopped by the server. Each case: ORION_PHONE_SYSTEMCTL_TIMEOUT, ORION_PHONE_GONE_WAIT, and the call limit that gives.
LIMIT_CASES = {
    "defaults": (None, None, 720),
    "systemctl raised by 180 s": ("300", None, 720 + 5 * 180),  # five systemctl calls in a new locked session
    "gone-wait raised by 60 s": (None, "65", 720 + 60),
    "both raised": ("300", "65", 720 + 5 * 180 + 60),
}


def set_limits(monkeypatch, systemctl: str | None, gone: str | None) -> None:
    for name, value in (("ORION_PHONE_SYSTEMCTL_TIMEOUT", systemctl), ("ORION_PHONE_GONE_WAIT", gone)):
        if value is None:  # the defaults, whatever the developer's shell exports
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.mark.parametrize("systemctl, gone, expected", LIMIT_CASES.values(), ids=LIMIT_CASES)
def test_the_cli_call_limit_follows_the_clis_own_time_limits(monkeypatch, systemctl, gone, expected):
    seen = {}

    def run(argv, **kwargs):  # the CLI itself is stubbed: only the limit the server hands subprocess.run matters
        seen.update(kwargs)
        answer = {"ok": True, "code": "OK", "exit": 0, "message": "done", "data": {}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")

    set_limits(monkeypatch, systemctl, gone)
    monkeypatch.setattr(mcp_server.subprocess, "run", run)
    mcp_server._cli("status", holder=False)
    assert seen["timeout"] == expected


def test_the_no_answer_message_names_the_limit_that_was_used(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    set_limits(monkeypatch, "300", None)
    monkeypatch.setattr(mcp_server.subprocess, "run", run)
    with pytest.raises(mcp_server.CliError) as caught:
        mcp_server._cli("status", holder=False)
    assert caught.value.code == "INTERNAL" and "no answer in 1620 s " in str(caught.value)  # 720 s + 5 x 180 s


@pytest.mark.parametrize("systemctl, gone", [case[:2] for case in LIMIT_CASES.values()], ids=LIMIT_CASES)
def test_the_cli_limit_outlasts_the_slowest_legitimate_locked_lease(monkeypatch, systemctl, gone):
    """A new locked session whose boot ended: an InvocationID look, stop, emu kill, the gone-wait (its last adb call
    may start just before the deadline), port looks, a look at the loaded unit, start, another InvocationID look. The
    limit is for lock waits."""
    from orion_phone import phone, services
    from orion_phone.adb import Adb

    set_limits(monkeypatch, systemctl, gone)
    adb_call = inspect.signature(Adb).parameters["timeout"].default
    gone_wait = services.limit("ORION_PHONE_GONE_WAIT", phone.GONE_WAIT)  # read as the CLI reads it
    wait_for_gone = gone_wait + adb_call + phone.GONE_POLL + 4 * services.PROBE_TIMEOUT
    slowest = 5 * services.limit("ORION_PHONE_SYSTEMCTL_TIMEOUT", services.SYSTEMCTL_TIMEOUT) + adb_call + wait_for_gone
    assert mcp_server.cli_timeout() > slowest + 30  # and room for the interpreter and the record writes


def test_a_cli_that_cannot_be_started_is_an_internal_tool_error(mcp_env, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-such-python"))
    result = call("status", {"phone": "a"})
    assert result.is_error and "INTERNAL" in result.content[0].text and "cannot run orion-phone" in result.content[0].text


def test_nothing_the_server_starts_reads_its_stdin(mcp_env):
    """adb forwards its stdin to the phone, and a stdio server's stdin is the MCP pipe."""
    adb = mcp_env.root / "bin" / "adb"
    seen = mcp_env.root / "stdin.log"
    shebang, *rest = adb.read_text().splitlines()
    log = f'echo "cli=$(readlink /proc/$PPID/fd/0) adb=$(readlink /proc/$$/fd/0)" >> "{seen}"'
    adb.write_text("\n".join([shebang, log, *rest]) + "\n")
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)  # this process's stdin is now a pipe, as a stdio server's is
    try:
        assert not call("status", {"phone": "a"}).is_error
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)
    assert set(seen.read_text().split()) == {"cli=/dev/null", "adb=/dev/null"}
    seen.unlink()
    cli_run = subprocess.run(  # the CLI on its own, given a stdin, still gives adb none
        [sys.executable, "-m", "orion_phone", "--json", "status", "a"],
        input="not for adb", capture_output=True, text=True, env=mcp_env.env, timeout=60,
    )
    assert json.loads(cli_run.stdout)["ok"]
    assert {word for word in seen.read_text().split() if word.startswith("adb=")} == {"adb=/dev/null"}


def free(env, phone: str) -> bool:
    """True when no one holds the phone, asked through a release the CLI refuses before touching anything."""
    return "no one holds it" in env.run("release", phone, "--holder", "probe")[1]["message"]


@pytest.mark.parametrize("stop_seconds", [0, 3], ids=["quick-stop", "stop-outlasting-the-clients-grace"])
def test_a_server_that_stops_gives_back_the_phones_it_holds(mcp_env, stop_seconds):
    """The SDK's stdio client closes stdin, waits 2 s, then kills the server's process group: the give-back must
    outlive that, or a locked phone's slow shutdown would be cut off and the phone kept until its lease expires."""
    mcp_env.phone_ports()  # b's console opens once it is up: a hung stop is ended by the kill sent to it
    if stop_seconds:
        mcp_env.set_fake("systemctl_hang", "stop")
        mcp_env.set_fake("hang_seconds", stop_seconds)

    async def session():
        with anyio.fail_after(60):
            async with Client(serve(mcp_env.env)) as client:
                assert not (await client.call_tool("lease", {"phone": "a"})).is_error
                reserved = await client.call_tool("lease", {"phone": "b"})
                assert reserved.is_error and "NEEDS_OPERATOR" in reserved.content[0].text  # held from this moment

    anyio.run(session)
    deadline = time.monotonic() + 20
    while not (free(mcp_env, "a") and free(mcp_env, "b")):
        assert time.monotonic() < deadline, "a stopped server's phones are still held"
        time.sleep(0.2)
    stops = [c for c in mcp_env.calls("systemctl") if c[1:] == ["stop", "orion-phone-b.service"]]
    assert len(stops) == 2  # one before the session's cold boot, one when the server gave the locked phone back
    if not stop_seconds:  # the fake's hung stop sleeps and changes nothing: only the quick stop shows the unit gone
        assert "orion-phone-b.service" not in json.loads(mcp_env.fake_state.read_text())["invocations"]
    assert mcp_env.run("lease", "a", "--holder", "other-agent")[0] == 0  # another agent can take a phone given back


def release_children(env) -> list[int]:
    """Pids of the `release` commands this test's give-back started, found through /proc (another run's are not)."""
    mine = f"ORION_PHONE_STATE={env.env['ORION_PHONE_STATE']}".encode()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            args = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
            environ = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
        except OSError:  # it ended while we looked, or it is not ours to read
            continue
        if b"orion_phone" in args and b"release" in args and mine in environ:
            found.append(int(entry))
    return found


def test_the_give_back_is_a_detached_process(mcp_env):
    """It must outlive the server: lead a session of its own (a group kill cannot reach it), hold no pipe of the MCP
    session, and not run from the agent's project."""
    mcp_env.phone_ports()  # b's console opens once it is up: the give-back's hung stop is ended by the kill sent to it
    mcp_env.set_fake("systemctl_hang", "stop")
    mcp_env.set_fake("hang_seconds", 4)  # keeps the release alive long enough to look at

    async def session():
        with anyio.fail_after(60):
            async with Client(serve(mcp_env.env)) as client:
                await client.call_tool("lease", {"phone": "b"})  # reserved from this moment: the give-back releases it

    anyio.run(session)
    looking = time.monotonic() + 3
    children = release_children(mcp_env)
    while not children and time.monotonic() < looking:
        time.sleep(0.05)
        children = release_children(mcp_env)
    assert children, "the server stopped without starting a release"
    pid = children[0]
    assert os.getsid(pid) == pid  # the leader of its own session
    assert [os.readlink(f"/proc/{pid}/fd/{n}") for n in (0, 1, 2)] == ["/dev/null"] * 3
    assert os.readlink(f"/proc/{pid}/cwd") == "/"
    finished = time.monotonic() + 20
    while not free(mcp_env, "b"):  # let it finish: nothing outlives the test
        assert time.monotonic() < finished, "the release never finished"
        time.sleep(0.2)


def test_the_server_tells_every_agent_how_to_share_a_phone():
    async def go():
        async with Client(mcp_server.mcp) as client:
            return client.instructions

    text = anyio.run(go) or ""
    assert "release" in text and "untrusted" in text and "PIN" in text


def test_a_session_that_ends_during_a_slow_lease_still_gives_the_phone_back(mcp_env):
    """The client closes stdin, waits 2 s, then sends SIGTERM to the server's process group: a lease still running
    then never reaches the server's `finally`, and its phone was never marked held."""
    mcp_env.set_fake("systemctl_hang", "stop")
    mcp_env.set_fake("hang_seconds", 6)  # the cold boot's stop outlasts the client's grace

    async def session():
        with anyio.fail_after(60):
            async with Client(serve(mcp_env.env)) as client:
                async with anyio.create_task_group() as calls:
                    calls.start_soon(client.call_tool, "lease", {"phone": "b"})
                    await anyio.sleep(1)  # the lease holds the phone and is inside its slow stop
                    calls.cancel_scope.cancel()  # the agent's session ends now

    anyio.run(session)
    deadline = time.monotonic() + 30
    while not free(mcp_env, "b"):
        assert time.monotonic() < deadline, "a phone leased when the session ended is still held"
        time.sleep(0.2)


# SIGTERM runs the server's handler on the main thread. That thread holds the held-phones lock inside its own
# `finally: _give_back()` (the snapshot of the held phones), so a handler landing there takes a lock its own thread holds.
# The handler ends the process (os._exit), so it is never run here; this is its situation, on a thread. It runs in a
# child interpreter: a lock that does deadlock must fail this test, not stay held in the test process.
GIVE_BACK_UNDER_ITS_OWN_LOCK = """
import sys
import threading

from orion_phone import mcp_server

mcp_server._held.clear()  # nothing to release: only the locking is under test
finished = threading.Event()


def hold_the_lock_and_give_back():
    with mcp_server._held_lock:
        mcp_server._give_back()
    finished.set()


threading.Thread(target=hold_the_lock_and_give_back, daemon=True).start()
sys.exit(0 if finished.wait(5) else 1)
"""


def test_the_give_back_can_run_on_the_thread_that_already_holds_the_lock():
    proc = subprocess.run(
        [sys.executable, "-c", GIVE_BACK_UNDER_ITS_OWN_LOCK], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"_give_back waited for a lock its own thread already holds: {proc.stderr}"


# Tools run on worker threads, and `_held` is one set: a release of a phone that ends, a lease of the same phone that
# then succeeds, and the release's discard after it would drop a live lease from the set the shutdown gives back.
# Lease and release of one phone therefore take turns. These tests call the tool functions with the CLI call replaced.

DONE = {"ok": True, "code": "OK", "exit": 0, "message": "done", "data": {}}


class Stuck:
    """A CLI call that logs when it starts and ends, and that a release can be held inside until the test says so."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.in_release = threading.Event()
        self.let_release_end = threading.Event()

    def __call__(self, command: str, *positionals: str, **kwargs):
        self.events.append(f"{command} {positionals[0]} starts")
        if command == "release":
            self.in_release.set()
            assert self.let_release_end.wait(10), "the test never let the release end"
        self.events.append(f"{command} {positionals[0]} ends")
        return DONE

    def start_release(self, phone: str) -> threading.Thread:
        thread = threading.Thread(target=mcp_server.release, args=(phone,), daemon=True)
        thread.start()
        assert self.in_release.wait(10), "the release never reached its CLI call"
        return thread


def test_a_lease_that_starts_during_a_release_of_the_same_phone_stays_held(monkeypatch):
    cli = Stuck()
    monkeypatch.setattr(mcp_server, "_cli", cli)
    monkeypatch.setattr(mcp_server, "_held", {"a"})  # this session holds a, and is releasing it
    releasing = cli.start_release("a")
    leasing = threading.Thread(target=mcp_server.lease, args=("a",), daemon=True)
    leasing.start()
    leasing.join(0.5)  # a bounded look: the lease must still be waiting for the release, not past it
    cli.let_release_end.set()
    for thread in (releasing, leasing):
        thread.join(10)
        assert not thread.is_alive()
    assert cli.events == ["release a starts", "release a ends", "lease a starts", "lease a ends"]
    assert mcp_server._held == {"a"}  # the new lease is held: the shutdown will give it back


def test_a_lease_of_another_phone_does_not_wait_for_a_release(monkeypatch):
    cli = Stuck()
    monkeypatch.setattr(mcp_server, "_cli", cli)
    monkeypatch.setattr(mcp_server, "_held", {"a"})
    releasing = cli.start_release("a")
    leased = threading.Event()

    def lease_b() -> None:
        mcp_server.lease("b")
        leased.set()

    threading.Thread(target=lease_b, daemon=True).start()
    try:
        assert leased.wait(5), "a lease of phone b waited for a release of phone a"
    finally:
        cli.let_release_end.set()
        releasing.join(10)
    assert mcp_server._held == {"b"}


def test_the_give_back_never_waits_behind_a_tool_call(monkeypatch):
    cli = Stuck()
    started: list[list[str]] = []
    monkeypatch.setattr(mcp_server, "_cli", cli)
    monkeypatch.setattr(mcp_server, "_held", {"a"})
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda argv, **kwargs: started.append(argv))
    releasing = cli.start_release("a")  # holds phone a's turn, and so would hold up anything that waited for it
    giving = threading.Thread(target=mcp_server._give_back, daemon=True)
    giving.start()
    giving.join(5)
    try:
        assert not giving.is_alive(), "the give-back waited behind a tool call"
    finally:
        cli.let_release_end.set()
        releasing.join(10)
    assert any("release" in argv and "a" in argv for argv in started)


def test_the_per_phone_turns_do_not_pile_up(monkeypatch):
    monkeypatch.setattr(mcp_server, "_cli", lambda *args, **kwargs: DONE)
    monkeypatch.setattr(mcp_server, "_held", set())
    for name in ("a", "b", "no-such-phone"):
        mcp_server.lease(name)
        mcp_server.release(name)
    assert mcp_server._phone_locks == {}  # a name an agent made up leaves nothing behind
