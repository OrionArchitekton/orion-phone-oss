"""Test environment: temp config and state, fake adb and systemctl on PATH, a CLI runner."""

from __future__ import annotations

import fcntl
import json
import os
import random
import resource
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import pytest

import fake_systemctl

TESTS = Path(__file__).resolve().parent


def pick_probe_host() -> str:
    """A loopback address nothing listens on, so the port probe never touches a real phone's ports. The listener tests
    bind fixed ports there, so each test session picks its own at random: two suites running at once do not collide.
    Never 127.0.0.1, where real emulators listen, and never inside 127.0.0.0/16, where the system keeps its own
    addresses (127.0.0.53, 127.0.1.1)."""
    r = random.SystemRandom()
    return f"127.{r.randint(100, 199)}.{r.randint(0, 255)}.{r.randint(1, 254)}"


PROBE_HOST = pick_probe_host()  # once per session: every env, every listener, and the CLI's probe agree on it
SERVER_PORT = 5037  # where the adb server listens: on the probe address, never the real 127.0.0.1 where a real server may be
# Variables a developer's shell may carry that would change what the CLI does. INVOCATION_ID is set for processes
# systemd runs, and `_run` refuses to launch an emulator without it; the time limits are ones a test sets for itself, and so
# are the three that name an adb server's address, which the fake adb follows (tests/test_adb_endpoint.py sets them).
NOT_INHERITED = ("ORION_PHONE_HOLDER", "DISPLAY", "WAYLAND_DISPLAY", "INVOCATION_ID",
                 "ORION_PHONE_SYSTEMCTL_TIMEOUT", "ORION_PHONE_GONE_WAIT", "ORION_PHONE_WATCH_INTERVAL", "ANDROID_USER_HOME",
                 "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT")
DEFAULT_DEVICE = {"state": "device", "booted": True, "screen": "content", "keyguard": "false"}
CONFIG = """\
[android]
sdk_root = "{sdk}"
adb = "{adb}"

[lease]
ttl_minutes = {ttl}

[phones.a]
tier = "open"
console_port = 5554

[phones.b]
tier = "locked"
console_port = 5556
session_cap_minutes = 120
"""


def _shim(path: Path, script: str) -> Path:
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{TESTS / script}" "$@"\n')
    path.chmod(0o755)
    return path


@dataclass
class Env:
    root: Path
    env: dict[str, str] = field(repr=False)  # a copy of os.environ: keep it out of pytest failure output
    opened_ports: list = field(default_factory=list, repr=False)  # the ports opened for a test (FollowedPorts), closed after

    def phone_ports(self, console_port: int = 5556) -> PhonePorts:
        """The ports of a phone that follow the fake adb's listing of it (see PhonePorts): call it once, whether the phone is
        there yet or not, for the console port of the phone (5556, the locked one, by default)."""
        self.opened_ports.append(PhonePorts(self, console_port))
        return self.opened_ports[-1]

    def _cli(self, args: tuple[str, ...]) -> list[str]:
        return [sys.executable, "-m", "orion_phone", "--json", *args]

    def run(self, *args: str, timeout: float = 60) -> tuple[int, dict]:
        proc = subprocess.run(self._cli(args), capture_output=True, text=True, env=self.env, timeout=timeout)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise AssertionError(f"no JSON (exit {proc.returncode}): {proc.stdout!r} {proc.stderr!r}") from None
        assert proc.returncode == out["exit"], f"exit code {proc.returncode} != JSON exit {out['exit']}: {out}"
        return proc.returncode, out

    def popen(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            self._cli(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env
        )

    @property
    def fake_state(self) -> Path:
        return Path(self.env["FAKE_ADB_STATE"])

    def set_device(self, serial: str, **fields: object) -> None:
        state = json.loads(self.fake_state.read_text())
        state["devices"].setdefault(serial, dict(DEFAULT_DEVICE)).update(fields)
        self.fake_state.write_text(json.dumps(state))

    def remove_device(self, serial: str) -> None:
        state = json.loads(self.fake_state.read_text())
        state["devices"].pop(serial, None)
        self.fake_state.write_text(json.dumps(state))

    def set_fake(self, key: str, value: object) -> None:
        state = json.loads(self.fake_state.read_text())
        state[key] = value
        self.fake_state.write_text(json.dumps(state))

    def stop_unit(self, unit: str = "orion-phone-b.service") -> None:
        """The unit ends without orion-phone asking, as at the session cap: its run is over and its phone is gone."""
        state = json.loads(self.fake_state.read_text())
        fake_systemctl.stop_unit(state, unit)
        self.fake_state.write_text(json.dumps(state))

    def set_loaded_unit(self, unit: str = "orion-phone-b.service", **fields: object) -> None:
        """Change what the service manager has loaded of a unit: `cap` (seconds, None for no cap), `extra` (seconds of
        randomized extra run time past the cap, as a drop-in would set; a reload keeps it), `reload_pending` (its file
        changed since), `kill_mode` (control-group by default), `stop_timeout` (seconds, 90 by default) and `send_sigkill`
        (yes by default: what the manager has for a unit whose file names none of them), `restart` (what the manager does
        when the unit ends: no for a locked phone's unit, on-failure for an open phone's), `pending` (a dict of `cap`,
        `extra`, `kill_mode`, `stop_timeout`, `send_sigkill` and/or `restart` that a drop-in on disk sets and the manager has
        not loaded: a daemon-reload loads it, into a running unit's definition too), `env_state` (the ORION_PHONE_STATE the
        unit carries, None for none; by default the caller's own, and a reload takes it from the unit file), `core` and
        `core_soft` (its hard and soft core limits as `show` prints them: 1 and 1 for a locked phone's unit, infinity and 0,
        the manager's defaults, for any other; a reload takes them from the unit file's LimitCORE), `cap_text`,
        `extra_text`, `kill_mode_text`, `stop_timeout_text`, `send_sigkill_text`, `restart_text`, `reload_text`,
        `state_text`, `load_text` and `environment_text` (printed as RuntimeMaxUSec, RuntimeRandomizedExtraUSec, KillMode,
        TimeoutStopUSec, SendSIGKILL, Restart, NeedDaemonReload, ActiveState, LoadState and Environment instead of what they
        would say),
        `omit` (properties that `show` leaves out), `not_found` (the manager has no file for the unit, as before a first
        install: it answers for it with its defaults, and a daemon-reload that finds the unit file loads it)."""
        state = json.loads(self.fake_state.read_text())
        fake_systemctl.loaded_unit(state, unit).update(fields)
        self.fake_state.write_text(json.dumps(state))

    def start_unit_by_hand(self, unit: str = "orion-phone-b.service") -> None:
        """The operator starts the unit themselves: a new run with its own InvocationID, booted to its lock screen."""
        state = json.loads(self.fake_state.read_text())
        fake_systemctl.start_unit(state, unit)
        self.fake_state.write_text(json.dumps(state))

    @contextmanager
    def holding_the_action_lock(self, phone: str) -> Iterator[None]:
        """Hold a phone's action lock from this process, as an action in flight does."""
        path = Path(self.env["ORION_PHONE_STATE"]) / "leases" / f"{phone}.act"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def calls(self, tool: str = "adb") -> list[list[str]]:
        log = self.root / f"{tool}.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def shell_calls(self) -> list[str]:
        return [c[3] for c in self.calls() if len(c) > 3 and c[2] == "shell"]

    def audit(self, phone: str) -> list[dict]:
        folder = Path(self.env["ORION_PHONE_STATE"]) / "audit" / phone
        return [json.loads(line) for f in sorted(folder.glob("*.jsonl")) for line in f.read_text().splitlines()]


def make_env(root: Path, ttl: float = 30) -> Env:
    root.mkdir(parents=True, exist_ok=True)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (root / "home").mkdir()
    (root / "avd").mkdir()  # the real avdmanager and emulator ignore an ANDROID_AVD_HOME that is not there yet
    adb = _shim(bin_dir / "adb", "fake_adb.py")
    _shim(bin_dir / "systemctl", "fake_systemctl.py")
    sdk = root / "sdk"
    tools = sdk / "cmdline-tools" / "latest" / "bin"
    tools.mkdir(parents=True)
    _shim(tools / "sdkmanager", "fake_sdkmanager.py")
    _shim(tools / "avdmanager", "fake_avdmanager.py")
    config = root / "config.toml"
    config.write_text(CONFIG.format(sdk=sdk, adb=adb, ttl=ttl))
    fake_state = root / "fake_state.json"
    fake_state.write_text(json.dumps({"devices": {"emulator-5554": dict(DEFAULT_DEVICE)}, "log_dir": str(root)}))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(root / "home"),
        "ORION_PHONE_CONFIG": str(config),
        "ORION_PHONE_STATE": str(root / "state"),
        "ORION_PHONE_UNIT_DIR": str(root / "units"),
        "ANDROID_AVD_HOME": str(root / "avd"),
        "FAKE_ADB_STATE": str(fake_state),
        "ORION_PHONE_PROBE_HOST": PROBE_HOST,
        # Setup waits for the adb server it started to be up (see services.wait_for_adb_server): until it has stayed up for
        # the settle, and for at most the bound, which the real defaults set at 2 and 10 seconds. The many tests that run
        # setup would each wait seconds for that, so they run with a settle that outlasts nothing and a short bound; the
        # tests that pin the wait set their own values, or the code's defaults.
        "ORION_PHONE_SERVER_SETTLE": "0.05",
        "ORION_PHONE_SERVER_WAIT": "3",
    }
    for name in NOT_INHERITED:
        env.pop(name, None)
    return Env(root, env)


@pytest.fixture
def phone_env(tmp_path: Path) -> Iterator[Env]:
    env = make_env(tmp_path / "env")
    env.opened_ports.append(ServerPort(env))  # setup waits for the adb server's port, which opens once its unit is active
    yield env
    for ports in env.opened_ports:
        ports.close()


@pytest.fixture
def short_lease_env(tmp_path: Path) -> Env:
    return make_env(tmp_path / "short", ttl=0.02)


@pytest.fixture
def three_second_lease_env(tmp_path: Path) -> Env:
    return make_env(tmp_path / "three", ttl=0.05)


@pytest.fixture
def short_write() -> Callable[[int, Callable[[], object]], str]:
    """Run fn in a forked child that may grow any file to at most `limit` bytes. A write crossing the limit is
    accepted only in part, as on a full disk. Answers "raised" when fn refused with OSError."""

    def run(limit: int, fn: Callable[[], object]) -> str:
        pid = os.fork()
        if pid == 0:
            verdict = 3
            try:
                signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # EFBIG instead of a killed child
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
                fn()
                verdict = 1
            except OSError:
                verdict = 0
            finally:
                os._exit(verdict)
        _, status = os.waitpid(pid, 0)
        return {0: "raised", 1: "returned"}.get(os.waitstatus_to_exitcode(status), "crashed")

    return run


@pytest.fixture
def in_process(phone_env: Env, monkeypatch: pytest.MonkeyPatch) -> Callable[[], object]:
    """A factory for Phones built in this process, on the same fakes and state the CLI runner uses. The runner's
    environment is applied to this process too, so config.load() works. For tests that must reach inside a call:
    an answer made stale between two steps, or an interpreter path no CLI subprocess could be given."""
    from orion_phone import config
    from orion_phone.phone import Phones

    for key, value in phone_env.env.items():
        if os.environ.get(key) != value:
            monkeypatch.setenv(key, value)
    for key in NOT_INHERITED:
        monkeypatch.delenv(key, raising=False)
    return lambda: Phones(config.load())


class Listener:
    """Listening sockets on the probe address, standing in for a foreign emulator's console and adb ports. It accepts
    and drops every connection, as the real ports would, and `first` is set when the first one arrives."""

    def __init__(self, ports: tuple[int, ...]) -> None:
        self.first = threading.Event()
        self.stopping = threading.Event()
        self.socks: list[socket.socket] = []
        self.thread: threading.Thread | None = None
        for port in ports:
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((PROBE_HOST, port))
            except OSError as exc:
                sock.close()
                self.close()
                pytest.skip(f"cannot listen on {PROBE_HOST}:{port}: {exc}")
            sock.listen(8)
            sock.settimeout(0.05)
            self.socks.append(sock)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopping.is_set():
            for sock in self.socks:
                try:
                    conn, _ = sock.accept()
                except OSError:  # nothing arrived in this slice, or the socket was closed
                    continue
                self.first.set()
                conn.close()

    def close(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(2)
        for sock in self.socks:
            sock.close()


@pytest.fixture
def listening():
    """Open listening sockets on the probe address, as a foreign emulator's ports; they close when the test ends."""
    opened: list[Listener] = []

    def open_ports(*ports: int) -> Listener:
        opened.append(Listener(ports))
        return opened[-1]

    yield open_ports
    for listener in opened:
        listener.close()


class FollowedPorts:
    """Listening sockets on the probe address that are open exactly while something in the fake's state says they should be:
    they open when it does and close when it stops, as a real listener's do with its process, and each accepts and drops every
    connection, as the real ports would. A subclass says what to follow (`_listed`: True, False, or None when the state file
    could not be read this time)."""

    def __init__(self, env: Env, ports: tuple[int, ...]) -> None:
        self.env = env
        self.ports = ports
        self.socks: list[socket.socket] = []
        self.stopping = threading.Event()
        listed = self._listed()
        if listed:  # it is there already: the ports answer by the time the test goes on
            self._open(skip_if_taken=True)
        self.thread = threading.Thread(target=self._follow, daemon=True)
        self.thread.start()

    def _listed(self) -> bool | None:
        raise NotImplementedError

    def _open(self, skip_if_taken: bool = False) -> None:
        socks = []
        for port in self.ports:
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((PROBE_HOST, port))
            except OSError as exc:
                sock.close()
                for each in socks:
                    each.close()
                if skip_if_taken:
                    pytest.skip(f"cannot listen on {PROBE_HOST}:{port}: {exc}")
                return  # taken for a moment: the next look tries again
            sock.listen(8)
            sock.settimeout(0.02)
            socks.append(sock)
        self.socks = socks

    def _close(self) -> None:
        for sock in self.socks:
            sock.close()
        self.socks = []

    def _follow(self) -> None:
        while not self.stopping.is_set():
            listed = self._listed()
            if listed and not self.socks:
                self._open()
            elif listed is False and self.socks:
                self._close()
            for sock in list(self.socks):
                try:
                    conn, _ = sock.accept()
                except OSError:  # nothing arrived in this slice, or the socket was closed
                    continue
                conn.close()
            time.sleep(0.01)

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(2)
        self._close()


class PhonePorts(FollowedPorts):
    """The console and adb ports of a phone, on the probe address, answering exactly while the fake adb lists the phone (in any
    state): they open when it appears and close when it goes, as an emulator's do with its process. A test that has a phone
    running needs them, since a stop sends `adb emu kill` only to a console that answers, and the kill that works (or a unit
    stop that does) is what closes them again."""

    def __init__(self, env: Env, console_port: int) -> None:
        self.serial = f"emulator-{console_port}"
        super().__init__(env, (console_port, console_port + 1))

    def _listed(self) -> bool | None:
        try:
            return self.serial in json.loads(self.env.fake_state.read_text())["devices"]
        except (OSError, ValueError, KeyError):  # a state file caught mid-write: no answer yet
            return None


class ServerPort(FollowedPorts):
    """The adb server's port (5037) on the probe address, open exactly while the fake systemctl says the server's adb has bound
    it: its unit is active, and its adb has neither aborted nor been made to never listen (see fake_systemctl.server_listening).
    It opens a moment after the unit starts and closes when the unit stops. Setup waits for the port before it starts an open
    phone, so every test that runs setup needs it, and `phone_env` has one. A test that stands another server on the port
    (`listening(5037)`) holds it first: this one then finds it taken and tries again, and never opens. Never the real 5037."""

    def __init__(self, env: Env) -> None:
        super().__init__(env, (SERVER_PORT,))

    def _listed(self) -> bool | None:
        try:
            return fake_systemctl.server_listening(json.loads(self.env.fake_state.read_text()))
        except (OSError, ValueError, KeyError):  # a state file caught mid-write: no answer yet
            return None


@pytest.fixture(autouse=True)
def isolated_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may touch a real emulator's ports: the port probe looks at an address nothing listens on."""
    monkeypatch.setenv("ORION_PHONE_PROBE_HOST", PROBE_HOST)
