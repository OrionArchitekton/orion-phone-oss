"""S4: a locked phone's unit keeps its emulator only while its session's lease stands.

These tests run `orion-phone _run PHONE` as systemd would (INVOCATION_ID set, a display, a short watch interval) on
tests/fake_emulator.py, and look at real processes: who is alive, who exited with what status, and who was sent which
signal. Every wait has a bound, so a watch that does nothing fails a test instead of hanging it, and a unit left running by
a failing test is killed with its whole process group when the test ends.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import TESTS

from orion_phone import services
from orion_phone.lease import Lease, LeaseStore

INVOCATION = "0123456789abcdef0123456789abcdef"  # what systemd gives one run of a unit: 32 hex characters
OTHER_BOOT = "fedcba9876543210fedcba9876543210"
PATIENCE = 30  # seconds a test waits for something that should happen before it fails
INTERVAL = "0.2"  # seconds between the watch's looks at the lease, in these tests
# `_run` after some statements of the test's own: a shorter stop grace than the real one, so that the kill after the grace
# is a test of a second and not of ten, or a part of the watch replaced by one that fails
PATCHED = (
    "import sys; from orion_phone import services; {patch}; "
    "from orion_phone.cli import main; raise SystemExit(main(['--json', '_run', '{phone}']))"
)


def alive(pid: int) -> bool:
    """Is there a running process with this pid? A zombie, which only waits to be reaped, is not one."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def parent_of(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])


def wait_for(condition, what: str, timeout: float = PATIENCE) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"gave up after {timeout} s waiting for {what}"
        time.sleep(0.02)


class Unit:
    """One run of `orion-phone _run PHONE` as systemd runs it, on the fake emulator."""

    def __init__(
        self, env, phone: str, mode: str, grace: float | None, interval: str | None, cwd: Path | None, more: dict,
        broken_log: bool, patch: str | None,
    ):
        self.env, self.phone = env, phone
        self.dir = env.root / f"watch-{phone}"
        self.dir.mkdir(exist_ok=True)
        emulator = env.root / "sdk" / "emulator" / "emulator"
        emulator.parent.mkdir(parents=True, exist_ok=True)
        emulator.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{TESTS / "fake_emulator.py"}" "$@"\n')
        emulator.chmod(0o755)
        environment = {
            **env.env, "INVOCATION_ID": INVOCATION, "DISPLAY": ":0",
            "FAKE_EMULATOR_DIR": str(self.dir), "FAKE_EMULATOR": mode, **more,
        }
        if interval is not None:
            environment["ORION_PHONE_WATCH_INTERVAL"] = interval
        argv = [sys.executable, "-m", "orion_phone", "--json", "_run", phone]
        statements = [f"services.STOP_GRACE = {grace}"] * (grace is not None) + [patch] * (patch is not None)
        if statements:
            argv = [sys.executable, "-c", PATCHED.format(patch="; ".join(statements), phone=phone)]
        with open(self.dir / "unit.out", "wb") as out, open(self.dir / "unit.err", "wb") as err:
            log = err.fileno()
            if broken_log:  # standard error is a pipe nobody reads: every line written to it fails
                read_end, log = os.pipe()
                os.close(read_end)
            self.proc = subprocess.Popen(
                argv, env=environment, stdin=subprocess.PIPE, stdout=out, stderr=log, cwd=cwd,  # a pipe: not /dev/null
                start_new_session=True,  # its own process group, so that whatever a failing test leaves can be killed whole
            )
            if broken_log:
                os.close(log)
        pid_file = self.dir / "emulator.pid"
        wait_for(lambda: pid_file.exists() or self.proc.poll() is not None, "the emulator to start")
        assert pid_file.exists(), f"the unit ended before it started the emulator: {self.stderr()}"
        self.emulator_pid = int(pid_file.read_text())

    def stderr(self) -> str:
        return (self.dir / "unit.err").read_text()

    def events(self) -> list[str]:
        path = self.dir / "emulator.events"
        return [line.split(" ", 1)[1] for line in path.read_text().splitlines()] if path.exists() else []

    def event_time(self, text: str) -> float:
        for line in (self.dir / "emulator.events").read_text().splitlines():
            stamp, what = line.split(" ", 1)
            if what == text:
                return float(stamp)
        raise AssertionError(f"the emulator logged no {text!r}: {self.events()}")

    def ends_with(self, status: int, within: float = PATIENCE) -> None:
        """The unit ends, and with this status, within `within` seconds."""
        try:
            got = self.proc.wait(timeout=within)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"the unit was still running after {within} s: {self.stderr()}") from None
        assert got == status, f"the unit ended with {got}, not {status}: {self.stderr()}"

    def stays_running(self, seconds: float) -> None:
        """The unit and its emulator are both still there after `seconds`: a wait that ends, bounded, by running out."""
        try:
            self.proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            assert alive(self.emulator_pid), f"the emulator is gone: {self.stderr()}"
            return
        raise AssertionError(f"the unit ended within {seconds} s with {self.proc.returncode}: {self.stderr()}")

    def kill_everything(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)  # the unit's process group: it and whatever it left running
        except ProcessLookupError:
            pass
        self.proc.wait()
        self.proc.stdin.close()


@pytest.fixture
def start_unit(phone_env):
    started: list[Unit] = []

    def start(
        phone="b", *, mode="sleep", grace=None, interval=INTERVAL, cwd=None, broken_log=False, patch=None, **more
    ) -> Unit:
        unit = Unit(phone_env, phone, mode, grace, interval, cwd, more, broken_log, patch)
        started.append(unit)
        return unit

    yield start
    for unit in started:
        unit.kill_everything()


def store_of(env) -> LeaseStore:
    return LeaseStore(Path(env.env["ORION_PHONE_STATE"]))


def hold(env, boot: str | None, ttl: float = 1800) -> None:
    """agent-1 holds phone b, with its session bound to `boot` (None: not bound to a boot yet)."""
    store = store_of(env)
    store.acquire("b", "agent-1", ttl)
    if boot:
        store.mark_ready("b", "agent-1", boot)


def lapse(env) -> None:
    store_of(env).acquire("b", "agent-1", -1)  # its own live lease, extended to a time that has passed


def damage(env) -> None:
    path = Path(env.env["ORION_PHONE_STATE"]) / "leases" / "b.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("{ this is not a lease")


def unreadable(env) -> None:
    path = Path(env.env["ORION_PHONE_STATE"]) / "leases" / "b.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.mkdir()  # something stands where the lease file should: reading it fails, whoever is asking


# What the watch decides, as a function of the lease on file.

NOW = 1500.0
STARTED = 1200.0  # when the run of the unit under test started its emulator: after the leases below were taken, before NOW


def lease_of(**fields) -> Lease:
    return Lease("b", "agent-1", 1000.0, 2000.0, **fields)


def lease_taken_at(when: float, **fields) -> Lease:
    return Lease("b", "agent-1", when, 2000.0, **fields)


# A lease bound to a boot is judged by the boot alone. One bound to none is the session whose start this run is part of only if
# it was taken no later than this run started: the lease flow writes a session's lease before it starts the unit, so its own
# boot starts after it. One taken after this boot started is a newer session's, which has not stopped this boot yet.

@pytest.mark.parametrize(
    "lease, now, reason",
    [
        (None, NOW, "no lease"),
        (lease_of(), 2000.0, "expired"),  # the moment it expires is a moment it no longer stands
        (lease_of(ready=True, invocation=INVOCATION), 2500.0, "expired"),
        (lease_of(ready=True, invocation=OTHER_BOOT), NOW, "another boot"),
        (lease_of(), NOW, None),  # not yet bound to a boot, and taken before this run started: its own session
        (lease_taken_at(STARTED), NOW, None),  # taken at the very moment it started: at or before is the session's own
        (lease_taken_at(STARTED + 0.001), NOW, "after this boot started"),
        (lease_taken_at(1300.0), NOW, "after this boot started"),  # a takeover's lease, written before it stops this boot
        (lease_taken_at(1300.0, ready=True, invocation=""), NOW, "after this boot started"),  # ready, but no boot named
        (lease_taken_at(1300.0, ready=True, invocation=INVOCATION), NOW, None),  # bound to this boot: judged by the boot
        (lease_taken_at(1300.0, ready=True, invocation=OTHER_BOOT), NOW, "another boot"),
        (lease_taken_at(1300.0), 2000.0, "expired"),  # an expired lease is expired, whenever it was taken
        (lease_of(ready=True, invocation=INVOCATION), NOW, None),  # bound to this very boot
        (lease_of(ready=True, invocation=""), NOW, None),  # a lease from before leases named a boot: not bound to one
    ],
    ids=[
        "no lease", "just expired", "long expired", "another boot", "not yet bound", "taken as the run started",
        "taken just after the run started", "taken well after the run started", "ready, no boot named, taken after",
        "bound to this boot, taken after", "bound to another boot, taken after", "expired, taken after", "this boot",
        "ready, no boot named",
    ],
)
def test_which_leases_let_the_emulator_run(lease, now, reason):
    got = services.lapse_reason(lease, now, INVOCATION, STARTED)
    assert (got is None) if reason is None else (got is not None and reason in got), got


# Once the watch has seen a lease bound to its own boot, one bound to no boot is never its session: the session that bound
# the lease is the one whose boot this is, and a lease with no boot written after that is a newer session's, whatever the
# clock says its time is. Before that first binding, the time rule above is all there is.

@pytest.mark.parametrize(
    "lease, reason",
    [
        (lease_of(), "bound to no boot"),  # taken long before the boot started: the old rule keeps it, the latch does not
        (lease_taken_at(STARTED), "bound to no boot"),
        (lease_taken_at(STARTED - 3600.0), "bound to no boot"),  # a clock stepped back an hour reads it older still
        (lease_taken_at(1300.0), "bound to no boot"),
        (lease_taken_at(1000.0, ready=True, invocation=""), "bound to no boot"),
        (lease_of(ready=True, invocation=INVOCATION), None),  # still bound to this boot: it keeps it
        (lease_taken_at(1300.0, ready=True, invocation=INVOCATION), None),
        (lease_of(ready=True, invocation=OTHER_BOOT), "another boot"),
        (None, "no lease"),
    ],
    ids=["taken long before the start", "taken as it started", "an hour before the start", "taken after the start",
         "ready, no boot named", "bound to this boot", "bound to this boot, taken after", "bound to another boot", "no lease"],
)
def test_once_the_boot_has_been_bound_a_lease_bound_to_no_boot_never_keeps_it(lease, reason):
    got = services.lapse_reason(lease, NOW, INVOCATION, STARTED, True)
    assert (got is None) if reason is None else (got is not None and reason in got), got


def test_an_expired_lease_is_expired_whatever_the_latch_says():
    assert "expired" in services.lapse_reason(lease_of(), 2000.0, INVOCATION, STARTED, True)


def look_at(tmp_path, boot):
    return services._look_at_the_lease(LeaseStore(tmp_path), "b", boot)


def on_file(tmp_path, lease: Lease) -> None:
    """The lease on file, put there as every write to it is: a whole file, renamed into place."""
    directory = tmp_path / "leases"
    directory.mkdir(parents=True, exist_ok=True)
    LeaseStore._write(directory / "b.json", lease)


FUTURE = time.time() + 3600  # a lease that stands, for the looks below (which read the real clock)


def standing(when: float, **fields) -> Lease:
    return Lease("b", "agent-1", when, FUTURE, **fields)


def test_the_watch_remembers_that_a_lease_was_bound_to_its_boot_and_only_then(tmp_path):
    boot = services._Boot(INVOCATION, STARTED)
    on_file(tmp_path, standing(1000.0))  # the starting phase: taken before the boot, bound to none
    assert look_at(tmp_path, boot) is None and boot.bound is False
    on_file(tmp_path, standing(1000.0, ready=True, invocation=INVOCATION))  # its session binds it
    assert look_at(tmp_path, boot) is None and boot.bound is True
    on_file(tmp_path, standing(STARTED - 3600.0))  # a takeover's lease, and a clock that reads it older than the boot
    assert "bound to no boot" in look_at(tmp_path, boot)
    on_file(tmp_path, standing(1000.0, ready=True, invocation=INVOCATION))  # and were it bound to this boot again: kept
    assert look_at(tmp_path, boot) is None and boot.bound is True


def test_a_lease_bound_to_this_boot_that_has_expired_still_counts_as_a_binding(tmp_path):
    boot = services._Boot(INVOCATION, STARTED)
    on_file(tmp_path, Lease("b", "agent-1", 1000.0, 1.0, ready=True, invocation=INVOCATION))  # bound, and long expired
    assert "expired" in look_at(tmp_path, boot) and boot.bound is True
    on_file(tmp_path, standing(STARTED - 3600.0))  # the next session's lease, which reads older than the boot
    assert "bound to no boot" in look_at(tmp_path, boot)


def test_a_lease_bound_to_another_boot_is_no_binding_of_this_one(tmp_path):
    boot = services._Boot(INVOCATION, STARTED)
    on_file(tmp_path, standing(1000.0, ready=True, invocation=OTHER_BOOT))
    assert "another boot" in look_at(tmp_path, boot) and boot.bound is False
    on_file(tmp_path, standing(1000.0))  # taken before this boot started, bound to none: still the starting phase
    assert look_at(tmp_path, boot) is None and boot.bound is False


@pytest.mark.parametrize("state", ["no lease", "an unreadable lease"])
def test_a_look_that_finds_no_lease_to_read_binds_nothing(tmp_path, state):
    boot = services._Boot(INVOCATION, STARTED)
    if state == "an unreadable lease":
        (tmp_path / "leases").mkdir()
        (tmp_path / "leases" / "b.json").write_text("{ this is not a lease")
    assert look_at(tmp_path, boot) is not None and boot.bound is False


@pytest.mark.parametrize("returncode, status", [(0, 0), (7, 7), (255, 255), (-9, 137), (-15, 143), (-6, 134)])
def test_an_emulator_that_ended_on_its_own_is_given_as_its_status_or_128_plus_its_signal(returncode, status):
    assert services._exit_status(returncode) == status


def test_the_watch_and_the_grace_are_what_the_docs_say():
    assert (services.WATCH_INTERVAL, services.STOP_GRACE, services.MAX_WATCH_INTERVAL) == (15.0, 30.0, 300.0)


# A live lease keeps the emulator, however long.


@pytest.mark.parametrize("boot", [INVOCATION, None], ids=["bound to this boot", "not yet bound to a boot"])
def test_a_live_lease_keeps_the_emulator_across_several_intervals(phone_env, start_unit, boot):
    hold(phone_env, boot)
    unit = start_unit()
    unit.stays_running(2.0)  # ten looks at the lease, and nothing has been stopped


def test_the_emulator_stops_once_a_lease_that_stood_lapses_and_not_before(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    unit.stays_running(1.0)  # it was looked at, and left alone
    lapse(phone_env)
    lapsed = time.monotonic()
    unit.ends_with(0)  # an intended stop: the unit ends inactive, not failed
    assert time.monotonic() - lapsed < 10, "it took far longer than an interval and a stop"
    assert not alive(unit.emulator_pid)
    assert unit.events() == ["started", "SIGTERM"]  # stopped the polite way
    assert "the lease of agent-1 has expired" in unit.stderr()


# Anything but a live lease for this boot stops it, at the first look.

STOPPING = {
    "no lease at all": (lambda env: None, "no lease is held"),
    "an expired lease": (lambda env: hold(env, INVOCATION, ttl=-1), "has expired"),
    "a lease for another boot": (lambda env: hold(env, OTHER_BOOT), "another boot"),
    "a damaged lease file": (damage, "cannot be read"),
    "a lease file that cannot be read at all": (unreadable, "cannot be read"),
}


@pytest.mark.parametrize("arrange, reason", STOPPING.values(), ids=STOPPING)
def test_the_emulator_is_stopped_when(phone_env, start_unit, arrange, reason):
    arrange(phone_env)
    unit = start_unit()
    unit.ends_with(0)
    assert not alive(unit.emulator_pid)
    assert "SIGTERM" in unit.events()
    assert reason in unit.stderr(), unit.stderr()


# The emulator's own end is the unit's.


@pytest.mark.parametrize(
    "mode, status", [("exit:0", 0), ("exit:7", 7), ("die:9", 137), ("die:15", 143)],
    ids=["exits 0", "exits 7", "killed by SIGKILL", "killed by SIGTERM"],
)
def test_an_emulator_that_ends_on_its_own_ends_the_unit_with_its_status(phone_env, start_unit, mode, status):
    hold(phone_env, INVOCATION)  # the lease stands: only the emulator's own end can end the unit
    unit = start_unit(mode=mode)
    unit.ends_with(status)
    assert not alive(unit.emulator_pid)


def test_an_emulator_that_exits_says_so_in_the_unit_log(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(mode="exit:7")
    unit.ends_with(7)
    assert "exited with status 7" in unit.stderr()


# A stop of the unit (systemctl stop, the session cap) is a stop of the emulator, and the unit never outlives it.


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_stop_of_the_unit_stops_the_emulator_and_ends_the_unit_cleanly(phone_env, start_unit, signum):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    os.kill(unit.proc.pid, signum)
    unit.ends_with(0, within=10)
    assert not alive(unit.emulator_pid)
    assert unit.events() == ["started", "SIGTERM"]
    assert "told to stop" in unit.stderr()


def test_a_second_stop_while_the_emulator_is_stopping_changes_nothing(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(mode="ignore-term", grace=1.0)
    os.kill(unit.proc.pid, signal.SIGTERM)
    wait_for(lambda: "SIGTERM ignored" in unit.events(), "the emulator to be told to stop")
    os.kill(unit.proc.pid, signal.SIGTERM)  # systemd sends one to every process of the unit: this is that second one
    unit.ends_with(0, within=10)
    assert not alive(unit.emulator_pid)


# An emulator that does not leave when asked is made to, after the grace, however the stop began.


@pytest.mark.parametrize("how", ["the lease lapsing", "a stop of the unit"])
def test_an_emulator_that_ignores_sigterm_is_killed_after_the_grace(phone_env, start_unit, how):
    hold(phone_env, INVOCATION)
    unit = start_unit(mode="ignore-term", grace=0.6)
    if how == "the lease lapsing":
        lapse(phone_env)
    else:
        os.kill(unit.proc.pid, signal.SIGTERM)
    wait_for(lambda: "SIGTERM ignored" in unit.events(), "the emulator to be told to stop")
    unit.ends_with(0, within=10)  # still an intended stop
    ended = time.time()
    assert not alive(unit.emulator_pid), "the emulator was left running"
    assert 0.5 <= ended - unit.event_time("SIGTERM ignored") < 5  # it was given the grace, and not much more
    assert "ignored SIGTERM for 0.6 s and is killed" in unit.stderr()


def test_an_emulator_that_leaves_when_asked_is_not_killed(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(grace=5.0)
    lapse(phone_env)
    unit.ends_with(0, within=10)
    assert "killed" not in unit.stderr() and unit.events() == ["started", "SIGTERM"]


# The lease is read without its lock: release() holds that lock while it stops the unit, and this process is what is
# being stopped, so a watch that waited for the lock would deadlock the release until systemd gave up on the unit.


def holding_the_lease_lock(env):
    path = Path(env.env["ORION_PHONE_STATE"]) / "leases" / "b.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_lapse_is_seen_while_someone_holds_the_lease_lock(phone_env, start_unit):
    hold(phone_env, INVOCATION, ttl=-1)
    fd = holding_the_lease_lock(phone_env)
    try:
        unit = start_unit()
        unit.ends_with(0)
        assert not alive(unit.emulator_pid)
    finally:
        os.close(fd)


def test_a_stop_is_answered_while_someone_holds_the_lease_lock(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    fd = holding_the_lease_lock(phone_env)  # as release() does while it waits for this very unit to stop
    try:
        unit.stays_running(1.0)  # the looks at the lease go on, and do not wait for the lock
        os.kill(unit.proc.pid, signal.SIGTERM)
        unit.ends_with(0, within=10)
        assert not alive(unit.emulator_pid)
    finally:
        os.close(fd)


# Whatever goes wrong in the watch, the emulator is not left running.


def test_a_lease_read_that_fails_in_any_way_stops_the_emulator(phone_env, start_unit):
    hold(phone_env, INVOCATION)  # a lease that would keep it, if it could be read
    unit = start_unit(patch="services.LeaseStore.read = lambda self, phone: 1 / 0")
    unit.ends_with(0)
    assert not alive(unit.emulator_pid)
    assert "cannot be read (division by zero)" in unit.stderr()


def test_an_error_in_the_watch_itself_still_stops_the_emulator_and_fails_the_unit(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(patch="services._look_at_the_lease = lambda *args: 1 / 0")  # a bug, outside every guard
    unit.ends_with(1)  # an internal error is never a clean stop
    assert not alive(unit.emulator_pid), "the emulator was left running behind a unit that failed"
    assert "ZeroDivisionError" in unit.stderr()
    assert unit.events() == ["started", "SIGTERM"]


# A line that cannot be written is lost, and the phone is stopped all the same.


def test_a_log_that_cannot_be_written_neither_keeps_the_phone_running_nor_stops_the_unit_from_stopping_it(
    phone_env, start_unit
):
    hold(phone_env, INVOCATION)
    unit = start_unit(broken_log=True)  # even the first line, written before the emulator starts, fails
    lapse(phone_env)
    unit.ends_with(0)
    assert not alive(unit.emulator_pid)


# How the emulator is started, and which phones are watched at all.


def test_a_locked_phones_emulator_is_a_child_of_the_unit_process(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    assert unit.emulator_pid != unit.proc.pid and parent_of(unit.emulator_pid) == unit.proc.pid


# `Unit` waits for the fake emulator's pid file to exist and then reads it, and the fake writes it last, once it can be
# signalled. A file that shows up before its content is there (it is created, and then written) reads as empty, and
# int("") fails: it did, once, in a clean run under load. The file must be whole or absent. The real gap is microseconds
# wide; FAKE_EMULATOR_PID_DELAY makes it half a second, so that the fault shows every time and not once in a while.


def test_the_pid_file_is_whole_or_absent_so_the_helper_never_reads_an_empty_one(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    began = time.monotonic()
    unit = start_unit(FAKE_EMULATOR_PID_DELAY="0.5")
    assert time.monotonic() - began >= 0.4  # the file was held back, and the helper waited for it: the gap was there
    assert unit.emulator_pid != unit.proc.pid and parent_of(unit.emulator_pid) == unit.proc.pid  # and it is the emulator's


def test_the_supervised_emulator_is_given_the_same_command_line_environment_directory_and_a_null_stdin(
    phone_env, start_unit, tmp_path
):
    phone_env.env["DISPLAY"] = ":0"
    printed = phone_env.run("_run", "b", "--print")[1]["data"]["argv"]  # what a launch is to do
    hold(phone_env, INVOCATION)
    here = tmp_path / "started-here"
    here.mkdir()
    unit = start_unit(cwd=here)
    seen = json.loads((unit.dir / "emulator.json").read_text())
    assert seen["argv"] == printed[1:]
    assert seen["env"] == {
        "ANDROID_SDK_ROOT": str(phone_env.root / "sdk"),
        "ANDROID_AVD_HOME": str(Path(phone_env.env["ANDROID_AVD_HOME"]).resolve()),
        "INVOCATION_ID": INVOCATION,
        "DISPLAY": ":0",
        "ORION_PHONE_STATE": phone_env.env["ORION_PHONE_STATE"],
    }
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(here)
    assert seen["stdin_is_dev_null"] is True


def test_an_open_phones_emulator_replaces_the_unit_process_and_is_not_watched(phone_env, start_unit):
    unit = start_unit("a")  # no lease on phone a at all, and a watch interval of a fifth of a second
    assert unit.emulator_pid == unit.proc.pid  # exec: the unit's process is the emulator
    unit.stays_running(1.5)  # it was never looked at: a lease is not what keeps an open phone running


def test_the_watch_says_how_often_it_looks(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    assert "looked at every 0.2 s" in unit.stderr()


def test_the_watch_looks_every_fifteen_seconds_unless_told_otherwise(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(interval=None)
    assert "looked at every 15 s" in unit.stderr()


@pytest.mark.parametrize(
    "value, shown", [("300", "300"), ("301", "300"), ("86400", "300"), ("1e9", "300"), ("299.5", "299.5"), ("60", "60")]
)
def test_the_watch_interval_is_held_to_five_minutes(phone_env, start_unit, value, shown):
    hold(phone_env, INVOCATION)
    unit = start_unit(interval=value)  # a lapse is never noticed later than this, whatever the variable says
    assert f"looked at every {shown} s" in unit.stderr()


@pytest.mark.parametrize("value", ["0", "-3", "soon", "nan", "inf", ""], ids=repr)
def test_a_bad_watch_interval_is_the_default_never_no_watch(phone_env, start_unit, value):
    hold(phone_env, INVOCATION)
    unit = start_unit(interval=value)
    assert "looked at every 15 s" in unit.stderr()


# The watch does not stop a phone under an action in flight. Release and a new session wait for the phone's action lock, and so
# does the stop that a lapsed lease asks for: it tries for the lock every fifth of a second (never blocking on it, so that a
# stop of the unit is answered at once, as release() holds the lock while it stops the unit), up to a bound, then reads the
# lease again under the lock and stops only if it still does not stand, holding the lock until the emulator is gone. If the
# bound runs out, the stop goes ahead anyway: an action that never ends does not keep a lapsed phone running.

WAITING = "waiting for an action"


def test_the_wait_for_an_action_is_what_the_docs_say():
    assert (services.ACTION_LOCK_WAIT, services.ACTION_LOCK_POLL) == (180.0, 0.2)  # three minutes, tried every fifth of a second


def action_lock_is_free(env) -> bool:
    """Can the phone's action lock be taken this moment from here? (And it is let go of at once.)"""
    path = Path(env.env["ORION_PHONE_STATE"]) / "leases" / "b.act"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_an_action_in_flight_delays_the_stop_until_it_ends(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    with phone_env.holding_the_action_lock("b"):  # an action is running on the phone
        lapse(phone_env)
        wait_for(lambda: WAITING in unit.stderr(), "the stop to say that it waits for the action")
        unit.stays_running(1.0)  # the lease lapsed, the look was made, and the phone is not shut down under the action
        assert unit.events() == ["started"]
        assert unit.stderr().count(WAITING) == 1, "the log says that it waits at every try"  # once, not every fifth of a second
        ended_at = time.time()  # the action ends as this block does
    unit.ends_with(0, within=10)
    assert not alive(unit.emulator_pid)
    assert unit.events() == ["started", "SIGTERM"]
    assert unit.event_time("SIGTERM") >= ended_at - 0.002, "the emulator was stopped before the action ended"
    assert "the lease of agent-1 has expired" in unit.stderr()


def test_a_stop_with_no_action_in_flight_does_not_wait(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    lapse(phone_env)
    unit.ends_with(0, within=10)
    assert WAITING not in unit.stderr() and unit.events() == ["started", "SIGTERM"]


def test_a_lease_renewed_while_the_stop_waits_keeps_the_phone_and_the_lock_is_let_go(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit()
    with phone_env.holding_the_action_lock("b"):
        lapse(phone_env)
        wait_for(lambda: WAITING in unit.stderr(), "the stop to say that it waits for the action")
        store_of(phone_env).acquire("b", "agent-1", 1800)  # the session carries on: its lease stands again,
        store_of(phone_env).mark_ready("b", "agent-1", INVOCATION)  # for this very boot
    unit.stays_running(1.5)  # the lock was free for the watch, which read the lease again, and found it standing
    assert unit.events() == ["started"]
    assert action_lock_is_free(phone_env), "the watch kept the lock after it decided to keep the phone"


def test_a_lease_that_stands_after_all_sends_the_watch_back_to_looking_once_an_interval(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    log = phone_env.root / "looks.log"
    # the first look says that the lease has lapsed and every look after it says that it stands, each look being logged
    unit = start_unit(
        interval="1.0",
        patch=(
            "services._look_at_the_lease = (lambda c, f: (lambda *a: (c.append(1), open(f, 'a').write('look\\n'), "
            f"'the lease has lapsed (test)' if len(c) == 1 else None)[2]))([], {str(log)!r})"
        ),
    )
    unit.stays_running(4.5)
    looks = len(log.read_text().splitlines())
    # the lapse (about 1 s in), the read under the lock, and then one look a second: about five, and not one for every try
    # at the lock, which would be about seventeen
    assert 3 <= looks <= 8, f"{looks} looks at the lease in 4.5 s"
    assert action_lock_is_free(phone_env)


def test_the_lease_is_read_again_under_the_lock_before_the_phone_is_stopped(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    # the first look says the lease has lapsed, and every look after it says that it stands: what a lease taken up again
    # between the look and the lock would show
    unit = start_unit(patch="services._look_at_the_lease = (lambda c: (lambda *a: (c.append(1), 'the lease has lapsed (test)' if len(c) == 1 else None)[1]))([])")
    unit.stays_running(1.5)
    assert unit.events() == ["started"]
    assert "stopping the emulator" not in unit.stderr()
    assert action_lock_is_free(phone_env)


def test_a_stop_of_the_unit_while_the_stop_waits_for_an_action_ends_both_at_once(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(patch="services.ACTION_LOCK_WAIT = 120.0")  # so long that only the signal can be what ends it
    with phone_env.holding_the_action_lock("b"):  # and the lock is held for the whole test: release() holds it as it stops
        lapse(phone_env)
        deadline = time.monotonic() + 3  # time to see the lapse and begin to wait: a watch that blocks on the lock never says so
        while WAITING not in unit.stderr() and time.monotonic() < deadline:
            time.sleep(0.02)
        sent = time.monotonic()
        os.kill(unit.proc.pid, signal.SIGTERM)  # systemctl stop, from a release that holds the lock
        unit.ends_with(0, within=5)
        assert time.monotonic() - sent < 3, "a stop of the unit waited for the action lock"
    assert WAITING in unit.stderr(), "it was not waiting for the action when it was told to stop"
    assert not alive(unit.emulator_pid)
    assert unit.events() == ["started", "SIGTERM"]
    assert "told to stop" in unit.stderr()


def test_a_phone_is_stopped_when_the_action_outlasts_the_bound(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(patch="services.ACTION_LOCK_WAIT = 1.0")
    with phone_env.holding_the_action_lock("b"):  # an action that does not end
        lapse(phone_env)
        lapsed = time.time()
        unit.ends_with(0, within=10)  # the bound ran out: the stop goes ahead, the action still running
        assert not alive(unit.emulator_pid)
    assert unit.events() == ["started", "SIGTERM"]
    assert unit.event_time("SIGTERM") - lapsed >= 0.9, "it did not wait for the action at all"
    assert "stopping the emulator of phone b anyway" in unit.stderr()


def test_a_lease_that_stands_again_when_the_bound_runs_out_keeps_the_phone(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(patch="services.ACTION_LOCK_WAIT = 1.0")
    with phone_env.holding_the_action_lock("b"):
        lapse(phone_env)
        wait_for(lambda: WAITING in unit.stderr(), "the stop to say that it waits for the action")
        store_of(phone_env).acquire("b", "agent-1", 1800)
        store_of(phone_env).mark_ready("b", "agent-1", INVOCATION)
        unit.stays_running(2.5)  # past the bound: the lease was read again, and it stands
    assert unit.events() == ["started"] and "anyway" not in unit.stderr()


def test_the_phone_lock_is_held_until_the_emulator_is_gone(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(mode="ignore-term", grace=2.0)
    lapse(phone_env)
    wait_for(lambda: "SIGTERM ignored" in unit.events(), "the emulator to be told to stop")
    assert action_lock_is_free(phone_env) is False  # the stop is under way, and an action or a release waits for it
    unit.ends_with(0, within=10)
    assert not alive(unit.emulator_pid)
    assert action_lock_is_free(phone_env)  # and the lock went with the process


def test_a_release_that_waits_for_the_lock_gets_it_once_the_emulator_is_gone_and_does_not_deadlock(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(mode="ignore-term", grace=1.5)
    lapse(phone_env)
    wait_for(lambda: "SIGTERM ignored" in unit.events(), "the emulator to be told to stop")
    got = threading.Event()

    def release_like():  # what release() and a new session do: wait for the action lock, blocking
        with store_of(phone_env).acting("b"):
            got.set()

    worker = threading.Thread(target=release_like, daemon=True)
    worker.start()
    worker.join(0.5)
    assert not got.is_set(), "it got the lock while the watch was still stopping the emulator"
    unit.ends_with(0, within=10)
    worker.join(10)
    assert got.is_set() and not alive(unit.emulator_pid)


def lapse_without_the_lease_lock(env) -> None:
    """The lease expires while someone holds the lease lock, as release() does while it stops the unit: its file is replaced
    whole, the way every write to it is made, and nothing waits."""
    path = Path(env.env["ORION_PHONE_STATE"]) / "leases" / "b.json"
    doc = json.loads(path.read_text())
    doc["expires_at"] = 1.0
    tmp = path.with_name("b.json.tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)


def test_a_release_that_holds_both_locks_while_it_stops_the_unit_does_not_deadlock_with_the_watch(phone_env, start_unit):
    # the real release command: it takes the action lock, then the lease lock, and then stops the unit, which waits for the
    # unit's process to end (the fake systemctl signals the group the test started, as systemd does). The watch sees the
    # lease lapse in the middle of that and tries for the action lock, which the release holds: it must answer the stop.
    hold(phone_env, INVOCATION)
    unit = start_unit(patch="services.ACTION_LOCK_WAIT = 120.0")  # so long that only the stop of the unit can end the wait
    phone_env.set_fake("unit_pids", {"orion-phone-b.service": unit.proc.pid})
    phone_env.set_fake("stop_delay", 1.5)  # systemd takes a moment to reach the unit: the release holds its locks that long
    phone_env.set_fake("stop_wait", 5)  # and gives the unit five seconds to end before it kills it (systemd gives it ninety)
    release = phone_env.popen("release", "b", "--holder", "agent-1")
    try:
        wait_for(lambda: not action_lock_is_free(phone_env), "the release to take the action lock")
        lapse_without_the_lease_lock(phone_env)  # the lease lapses with the release under way
        deadline = time.monotonic() + 1.0  # time for the watch to see it and begin to wait (a watch that blocks never says so)
        while WAITING not in unit.stderr() and time.monotonic() < deadline:
            time.sleep(0.02)
        out, err = release.communicate(timeout=30)
    finally:
        if release.poll() is None:
            release.kill()
            release.wait()
    assert release.returncode == 0, (out, err)
    unit.ends_with(0, within=5)  # on its own, at the SIGTERM: a watch that blocked on the lock would be killed, status 137
    assert json.loads(phone_env.fake_state.read_text()).get("stop_needed_kill") is None, "the stop had to kill the unit"
    assert "told to stop" in unit.stderr() and not alive(unit.emulator_pid)
    assert WAITING in unit.stderr(), "the watch was not waiting for the action lock while the release held it"
    assert not (Path(phone_env.env["ORION_PHONE_STATE"]) / "leases" / "b.json").exists()  # and the lease was released


def test_a_new_session_waits_for_the_watch_to_finish_stopping_the_emulator_and_then_starts(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(mode="ignore-term", grace=2.0)
    lapse(phone_env)
    wait_for(lambda: "SIGTERM ignored" in unit.events(), "the emulator to be told to stop")  # the watch holds the lock now
    lease = phone_env.popen("lease", "b", "--holder", "agent-2")  # the real command: it waits for the action lock
    try:
        time.sleep(0.7)
        assert lease.poll() is None, "a new session did not wait for the phone to be shut down"
        unit.ends_with(0, within=10)
        out, err = lease.communicate(timeout=30)
    finally:
        if lease.poll() is None:
            lease.kill()
            lease.wait()
    assert lease.returncode == 5 and '"state": "starting"' in out, (out, err)  # the phone was off, and is booted afresh


def test_a_lock_that_cannot_be_opened_does_not_keep_the_phone_running(phone_env, start_unit):
    hold(phone_env, INVOCATION)
    unit = start_unit(patch="services.LeaseStore.try_acting = lambda self, phone: (_ for _ in ()).throw(PermissionError('no lock for you'))")
    lapse(phone_env)
    unit.ends_with(0, within=10)  # waiting for a lock that cannot be had would be waiting for nothing
    assert not alive(unit.emulator_pid)
    assert "no lock for you" in unit.stderr() and unit.events() == ["started", "SIGTERM"]


# A takeover writes its lease before it stops the boot it replaces, and the lease it writes is bound to no boot. If that command
# dies, or hangs, between the write and the stop, the old boot is still running (possibly unlocked, for a session that has
# lapsed) under a live lease that names no boot: the old rule took that for the session whose start it was, and kept the old
# boot running until the new lease expired. The watch now knows when its own run started, and a lease bound to no boot that
# was taken after that is a newer session's: it stops the boot it has.


def take_over_and_die_at_the_stop(env, holder: str) -> None:
    """`lease b` as a takeover: the lease is written first, and the fake systemctl SIGKILLs the command as it reaches `stop`."""
    env.set_fake("systemctl_kill_parent", "stop")
    proc = env.popen("lease", "b", "--holder", holder)
    proc.communicate(timeout=60)
    assert proc.returncode == -signal.SIGKILL, proc.returncode
    env.set_fake("systemctl_kill_parent", None)


@pytest.mark.parametrize("holder", ["agent-2", "agent-1"], ids=["a takeover by another agent", "the same agent leasing again"])
def test_a_takeover_whose_command_dies_before_it_stops_the_old_boot_does_not_leave_that_boot_running(
    phone_env, start_unit, holder
):
    hold(phone_env, INVOCATION)  # agent-1's session, bound to this very boot
    unit = start_unit(interval="6")  # the old boot: its first look at its lease is six seconds in
    began = time.monotonic()
    lapse(phone_env)  # agent-1's lease expires, and the watch has not looked since
    take_over_and_die_at_the_stop(phone_env, holder)
    taken = store_of(phone_env).current("b")
    assert taken.holder == holder and taken.invocation == "" and taken.expires_at > time.time(), taken  # live, bound to none
    assert time.monotonic() - began < 4, "the command was too slow for this test: the watch had looked at the lease already"
    unit.ends_with(0, within=15)  # at the first look, within the interval: stopped though the lease is live and unbound
    assert not alive(unit.emulator_pid)
    assert unit.events() == ["started", "SIGTERM"]
    assert "after this boot started" in unit.stderr(), unit.stderr()


def test_a_lease_taken_before_the_boot_started_keeps_it_before_and_after_it_is_bound(phone_env, start_unit):
    hold(phone_env, None)  # the session's lease, written before its unit starts, bound to no boot yet
    unit = start_unit()
    unit.stays_running(1.0)  # looked at five times: it is the session whose start this is
    store_of(phone_env).mark_ready("b", "agent-1", INVOCATION)  # the lease flow binds it, just after the start
    unit.stays_running(1.0)
    assert unit.events() == ["started"]


def test_a_lease_taken_after_the_boot_started_and_bound_to_this_boot_keeps_it(phone_env, start_unit):
    hold(phone_env, INVOCATION)  # the boot is under way, and its session binds the lease
    unit = start_unit()
    unit.stays_running(1.0)
    store_of(phone_env).acquire("b", "agent-1", 1800)  # the same holder, its own live lease extended: still bound, still this boot's
    unit.stays_running(1.0)
    assert unit.events() == ["started"] and store_of(phone_env).current("b").invocation == INVOCATION


# The clock residual of the start-time rule, closed once the boot has been bound: the watch remembers that a lease was bound to
# its boot, and from then on a lease bound to no boot is a newer session's, whatever its time reads (a clock stepped back could
# make a takeover's lease read as taken before the boot started). Before the first binding, the time rule is all there is.


def write_a_takeover_lease(env, holder: str, taken: float) -> None:
    """A takeover's lease as it stands before the takeover stops the old boot: live, bound to no boot, and with `taken` as the
    time it was taken (a time a clock that had stepped back would have given)."""
    directory = Path(env.env["ORION_PHONE_STATE"]) / "leases"
    LeaseStore._write(directory / "b.json", Lease("b", holder, taken, time.time() + 1800))


def test_after_it_has_been_bound_a_boot_never_keeps_an_unbound_lease_whatever_the_clock_says(phone_env, start_unit):
    hold(phone_env, INVOCATION)  # the session's lease, bound to this very boot
    unit = start_unit()
    unit.stays_running(1.0)  # looked at five times: the watch has seen the lease bound to its boot
    started = time.time()
    write_a_takeover_lease(phone_env, "agent-2", started - 3600.0)  # taken, by a clock stepped back, an hour before this boot
    unit.ends_with(0, within=10)  # the next look, whatever the lease's time: stopped
    assert not alive(unit.emulator_pid) and unit.events() == ["started", "SIGTERM"]
    assert "bound to no boot" in unit.stderr(), unit.stderr()


def test_before_it_has_been_bound_the_time_rule_alone_decides_and_an_older_lease_keeps_the_boot(phone_env, start_unit):
    hold(phone_env, None)  # the starting phase: a lease taken before the boot, bound to none, and no binding yet
    unit = start_unit()
    unit.stays_running(1.0)  # an unbound lease taken before this boot started is its own session's
    write_a_takeover_lease(phone_env, "agent-1", time.time() - 3600.0)  # still before the first binding: the time rule keeps it
    unit.stays_running(1.0)
    assert unit.events() == ["started"]


def test_a_lease_bound_to_this_boot_after_an_unbound_phase_keeps_it_and_the_latch_then_holds(phone_env, start_unit):
    hold(phone_env, None)
    unit = start_unit()
    unit.stays_running(0.6)  # the starting phase
    store_of(phone_env).mark_ready("b", "agent-1", INVOCATION)  # the session binds the lease to this boot
    unit.stays_running(1.0)  # kept, and the watch has now seen the binding
    assert unit.events() == ["started"]
    write_a_takeover_lease(phone_env, "agent-2", time.time() - 3600.0)  # a newer session's lease, with an older time
    unit.ends_with(0, within=10)
    assert unit.events() == ["started", "SIGTERM"] and "bound to no boot" in unit.stderr()
