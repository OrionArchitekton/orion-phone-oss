"""The per-phone action lock: anything that would touch a phone waits for the action in flight on it.

These are properties of Phones below the CLI, like the lease race. The device layer is a stub and the test holds one
real act() open, so the order of events is exact instead of a matter of timing.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from orion_phone import config, services
from orion_phone.codes import Code, PhoneError
from orion_phone.phone import Phones

PATIENCE = 30  # seconds a thread may wait before the test gives up, so a broken lock fails instead of hanging
STOP = "systemctl stop orion-phone-b.service"
START = "systemctl start orion-phone-b.service"
ACTION = ["action started", "action finished"]
NL = chr(10)  # a newline


class Rig:
    def __init__(self, phones: Phones, now: list[float], shot: Path) -> None:
        self.phones, self.now, self.shot = phones, now, shot
        self.events: list[str] = []
        self.started = threading.Event()  # an action has reached the phone
        self.proceed = threading.Event()  # the test lets the actions on the phone finish
        self.waiting = threading.Event()  # the waiter thread is about to block on a lock someone else holds
        self.running = True  # does the stub device answer?
        self.boots = 1  # how many times the unit has run: each run has its own InvocationID

    def invocation(self) -> str:
        """The unit's InvocationID: one per run, and none while it is not running."""
        return f"run-{self.boots}" if self.running else ""

    def start(self, name: str, fn: Callable[[], object]) -> tuple[threading.Thread, dict]:
        outcome: dict = {}

        def target() -> None:
            try:
                outcome["result"] = fn()
            except BaseException as exc:  # the test reports it; a thread would swallow it
                outcome["error"] = exc

        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread, outcome

    def hold_an_action(self, phone: str) -> tuple[threading.Thread, dict]:
        """Agent-1 holds the phone and has an action on it that does not finish until the test says so."""
        self.phones.leases.acquire(phone, "agent-1", self.phones.cfg.ttl_seconds)
        if self.phones.cfg.phone(phone).tier == "locked":
            self.phones.leases.mark_ready(phone, "agent-1", self.invocation())  # its holder has been through a cold boot
        actor, acted = self.start("actor", lambda: self.phones.act(phone, "agent-1", "tap", {"x": 1, "y": 1}))
        assert self.started.wait(PATIENCE), "the action never reached the phone"
        return actor, acted

    def block(self, waiter: threading.Thread, what: str) -> None:
        """Return once the waiter is parked behind the action in flight; fail if it ran past it instead."""
        deadline = time.monotonic() + PATIENCE
        while not self.waiting.is_set() and waiter.is_alive() and time.monotonic() < deadline:
            time.sleep(0.005)  # it either blocks on a lock or runs to the end
        assert self.waiting.is_set(), f"{what} did not wait for the action in flight: {self.events}"
        time.sleep(0.2)  # a lock that was only brief contention would have let it through by now
        assert waiter.is_alive() and self.events == ["action started"]

    def finish(self, *threads: threading.Thread) -> None:
        self.proceed.set()
        for thread in threads:
            thread.join(PATIENCE)
            assert not thread.is_alive()

    def park(self, waiter: threading.Thread, what: str) -> None:
        """Return once the waiter is blocked on a lock someone else holds; fail if it ran past it instead. The waiter
        cannot get past a lock held by another thread, so no pause is needed to know it is still there."""
        deadline = time.monotonic() + PATIENCE
        while not self.waiting.is_set() and waiter.is_alive() and time.monotonic() < deadline:
            waiter.join(0.005)
        assert self.waiting.is_set(), f"{what} did not wait for the lock: {self.events}"
        assert waiter.is_alive(), f"{what} finished while the lock was still held: {self.events}"

    def records(self, phone: str) -> list[dict]:
        folder = self.phones.cfg.state_dir / "audit" / phone
        return [json.loads(line) for f in sorted(folder.glob("*.jsonl")) for line in f.read_text().splitlines()]

    def audited(self, phone: str) -> list[str]:
        return [record["action"] for record in self.records(phone)]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    cfg = config.parse(
        {"phones": {"a": {"tier": "open", "console_port": 5554}, "b": {"tier": "locked", "console_port": 5556}}},
        tmp_path / "config.toml",
        tmp_path / "state",
    )
    now = [1000.0]
    rig = Rig(Phones(cfg, lambda: now[0]), now, tmp_path / "shot.png")

    def perform(phone, lease, action, command):  # an action on the phone: it stays there until the test lets it go
        rig.events.append("action started")
        rig.started.set()
        assert rig.proceed.wait(PATIENCE), "the test never let the action finish"
        rig.events.append("action finished")
        return {"phone": phone.name, "screenshot": str(rig.shot)}

    def systemctl(*args: str) -> None:
        rig.events.append("systemctl " + " ".join(args))
        if args[0] == "start" and not rig.running:
            rig.boots += 1  # a new run of the unit
        rig.running = args[0] == "start"

    class Device:
        @staticmethod
        def is_running() -> bool:
            return rig.running

        @staticmethod
        def is_listed() -> bool:
            return rig.running

        @staticmethod
        def emu_kill() -> None:  # nothing outside the unit to kill
            pass

    real_flock = fcntl.flock

    def flock(fd, operation):
        if threading.current_thread().name == "waiter":
            try:
                return real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                rig.waiting.set()  # a lock another thread holds: this one has to wait for it
        return real_flock(fd, operation)

    monkeypatch.setattr(rig.phones, "_perform", perform)
    monkeypatch.setattr(rig.phones, "adb", lambda phone: Device())
    monkeypatch.setattr(services, "systemctl", systemctl)
    monkeypatch.setattr(services, "invocation_id", lambda unit: rig.invocation())
    monkeypatch.setattr(services, "unit_cap_problem", lambda phone, state_dir: None)  # the unit is the right one: no systemctl here
    monkeypatch.setattr(services, "answering_port", lambda phone: None)  # no ports in the stub world
    monkeypatch.setattr(fcntl, "flock", flock)
    yield rig
    rig.proceed.set()  # never leave a thread parked inside the phone when a test fails


@pytest.mark.parametrize(
    "phone, operation, after, audited",
    [
        ("a", "act", ACTION, ["tap", "tap"]),
        ("a", "release", [], ["tap", "release"]),
        ("b", "release", [STOP], ["tap", "release"]),
        ("b", "new session", [STOP, START], ["tap", "lease"]),  # the old holder's lease lapsed: a cold boot follows
        ("a", "new session", [], ["tap", "lease"]),  # the old holder's lease lapsed: its action is still on the phone
    ],
    ids=[
        "another action", "release of an open phone", "release of a locked phone", "new session on a locked phone",
        "new session on an open phone",
    ],
)
def test_an_operation_on_a_phone_waits_for_the_action_in_flight(rig, phone, operation, after, audited):
    phones = rig.phones
    actor, acted = rig.hold_an_action(phone)
    if operation == "new session":
        rig.now[0] += 10 * phones.cfg.ttl_seconds  # agent-1's lease lapses while its last action is still running
    calls = {
        "act": lambda: phones.act(phone, "agent-1", "tap", {"x": 2, "y": 2}),
        "release": lambda: phones.release(phone, "agent-1"),
        "new session": lambda: phones.lease(phone, "agent-2"),
    }
    waiter, waited = rig.start("waiter", calls[operation])
    rig.block(waiter, operation)
    rig.finish(actor, waiter)
    assert "error" not in acted, acted
    assert rig.events == [*ACTION, *after]
    if operation == "new session" and phones.cfg.phone(phone).tier == "locked":
        assert isinstance(waited["error"], PhoneError) and waited["error"].code is Code.NEEDS_OPERATOR
    else:
        assert "error" not in waited, waited
    assert rig.audited(phone) == audited


def test_an_action_that_waited_checks_the_lease_again_afterwards(rig):
    phones = rig.phones
    actor, acted = rig.hold_an_action("a")
    waiter, waited = rig.start("waiter", lambda: phones.act("a", "agent-1", "tap", {"x": 2, "y": 2}))
    rig.block(waiter, "a second action")
    phones.leases.release("a", "agent-1")  # the lease goes while the second action is still waiting its turn
    rig.finish(actor, waiter)
    assert isinstance(waited["error"], PhoneError) and waited["error"].code is Code.NOT_HOLDER, waited
    assert rig.events == ACTION  # it never reached the phone: a lease checked before the wait would not have stopped it
    assert rig.audited("a") == ["tap"]


def test_a_session_start_that_waited_is_judged_by_the_lease_as_it_is_after_the_wait(rig):
    phones = rig.phones
    actor, acted = rig.hold_an_action("b")
    waiter, waited = rig.start("waiter", lambda: phones.lease("b", "agent-1"))  # its own lease is live: a resume
    rig.block(waiter, "a session start")
    rig.now[0] += 10 * phones.cfg.ttl_seconds  # but the lease lapses before the action ends
    rig.finish(actor, waiter)
    error = waited.get("error")
    assert isinstance(error, PhoneError) and error.code is Code.NEEDS_OPERATOR, waited
    assert rig.events == [*ACTION, STOP, START]  # a lapsed session is never resumed: the phone cold boots again


def test_a_takeover_after_the_wait_is_never_treated_as_a_resume(rig, monkeypatch):
    phones = rig.phones

    class Device:  # running, booted, and unlocked: agent-1's session as the operator left it
        @staticmethod
        def is_running() -> bool:
            return rig.running

        @staticmethod
        def is_listed() -> bool:
            return rig.running

        @staticmethod
        def emu_kill() -> None:
            pass

        @staticmethod
        def shell(command: str) -> str:
            return {"getprop sys.boot_completed": "1" + NL,
                    "dumpsys activity activities": "  KeyguardController:" + NL + "    mKeyguardShowing=false" + NL}[command]

    real_acquire = phones.leases.acquire

    def acquire(phone, holder, ttl):
        if holder == "agent-2":  # agent-1's lease lapses between agent-2's look at the lease and its claim
            rig.now[0] += 10 * phones.cfg.ttl_seconds
        return real_acquire(phone, holder, ttl)

    monkeypatch.setattr(phones, "adb", lambda phone: Device())
    monkeypatch.setattr(phones.leases, "acquire", acquire)
    with phones.leases.acting("b"):  # an operation on b is in flight; agent-2 finds no lease, so no BUSY
        waiter, waited = rig.start("waiter", lambda: phones.lease("b", "agent-2"))
        deadline = time.time() + PATIENCE
        while not rig.waiting.is_set() and time.time() < deadline:
            time.sleep(0.005)
        assert rig.waiting.is_set(), "agent-2 never waited for the action lock"
        phones.leases.acquire("b", "agent-1", phones.cfg.ttl_seconds)  # agent-1's session holds b meanwhile
    waiter.join(PATIENCE)
    assert not waiter.is_alive()
    assert rig.events == [STOP, START], (rig.events, waited)  # a takeover always cold boots
    error = waited.get("error")
    assert isinstance(error, PhoneError) and error.code is Code.NEEDS_OPERATOR, waited


# An open phone's lease waits for the action in flight too. The previous holder's lease can lapse while its last action is
# still running (a host suspend, a lease time-to-live near its minimum), and a lease that returned then would hand the phone
# to a second agent while the first one's action is still on it.


def test_a_takeover_of_an_open_phone_waits_for_the_previous_holders_action_and_is_audited_after_it(rig):
    phones = rig.phones
    actor, acted = rig.hold_an_action("a")
    rig.now[0] += 10 * phones.cfg.ttl_seconds  # agent-1's lease lapses while its action is still running
    waiter, waited = rig.start("waiter", lambda: phones.lease("a", "agent-2"))
    rig.block(waiter, "a takeover")
    assert phones.leases.current("a").holder == "agent-1"  # nothing was granted while the action ran
    rig.finish(actor, waiter)
    assert "error" not in acted and "error" not in waited, (acted, waited)
    assert waited["result"].data["holder"] == "agent-2" and waited["result"].data["took_over_from"] == "agent-1"
    assert phones.leases.current("a").holder == "agent-2"
    assert rig.audited("a") == ["tap", "lease"]  # the action's record, then the takeover's: never the other way round
    assert rig.records("a")[-1]["took_over_from"] == "agent-1"


def test_a_lease_of_a_free_open_phone_waits_for_an_action_lock_held_by_anyone(rig):
    phones = rig.phones
    assert phones.leases.current("a") is None  # no one holds a lease on it
    with phones.leases.acting("a"):  # an operation on a is in flight, whoever began it
        waiter, waited = rig.start("waiter", lambda: phones.lease("a", "agent-2"))
        rig.park(waiter, "a lease of a free phone")
        assert phones.leases.current("a") is None  # nothing was granted while the operation ran
    waiter.join(PATIENCE)
    assert not waiter.is_alive()
    assert "error" not in waited and waited["result"].data["holder"] == "agent-2", waited
    assert "took_over_from" not in waited["result"].data  # a free phone has no one to take over from
    assert phones.leases.current("a").holder == "agent-2" and rig.audited("a") == ["lease"]


def test_an_open_phone_lease_that_waited_judges_the_phone_as_it_is_after_the_wait(rig):
    phones = rig.phones
    actor, acted = rig.hold_an_action("a")
    rig.now[0] += 10 * phones.cfg.ttl_seconds
    waiter, waited = rig.start("waiter", lambda: phones.lease("a", "agent-2"))
    rig.block(waiter, "a takeover")
    rig.running = False  # the phone goes down while agent-2 waits: a look taken before the wait would still say it is up
    rig.finish(actor, waiter)
    error = waited.get("error")
    assert isinstance(error, PhoneError) and error.code is Code.PHONE_DOWN, waited
    assert phones.leases.current("a").holder == "agent-1"  # no lease was granted on a phone that is down
    assert rig.audited("a") == ["tap"]


def action_lock_is_held(phones: Phones, phone: str) -> bool:
    """Is the phone's action lock held right now, by anyone (this thread included)? Another open file description cannot
    take a flock the first one holds, so a non-blocking attempt on a fresh one tells."""
    phones.leases.dir.mkdir(parents=True, exist_ok=True, mode=0o700)  # the store makes it on first use
    fd = os.open(phones.leases.dir / f"{phone}.act", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # and with it any lock this attempt took


def test_an_open_phone_is_looked_at_and_its_lease_claimed_only_while_the_action_lock_is_held(rig, monkeypatch):
    phones = rig.phones
    held = {}
    real_acquire = phones.leases.acquire

    def acquire(phone, holder, ttl):
        held["claim"] = action_lock_is_held(phones, phone)
        return real_acquire(phone, holder, ttl)

    class Device:
        @staticmethod
        def is_running() -> bool:
            held["look"] = action_lock_is_held(phones, "a")
            return True

    monkeypatch.setattr(phones.leases, "acquire", acquire)
    monkeypatch.setattr(phones, "adb", lambda phone: Device())
    assert not action_lock_is_held(phones, "a")  # the probe sees a free lock as free
    phones.lease("a", "agent-1")
    assert held == {"look": True, "claim": True}  # the probe sees the lock held at both steps, in the one lease call
    assert not action_lock_is_held(phones, "a")  # and the lock is let go afterwards


def test_an_open_phone_lease_that_waited_and_lost_is_refused_without_a_look_at_the_phone(rig, monkeypatch):
    phones = rig.phones
    looks: list[str] = []

    class Device:
        @staticmethod
        def is_running() -> bool:
            looks.append("adb devices")
            return True

    monkeypatch.setattr(phones, "adb", lambda phone: Device())
    with phones.leases.acting("a"):  # an operation on a is in flight, and no one holds a lease yet
        waiter, waited = rig.start("waiter", lambda: phones.lease("a", "agent-2"))
        rig.park(waiter, "a lease of a free phone")
        phones.leases.acquire("a", "agent-3", phones.cfg.ttl_seconds)  # agent-3 claimed it while agent-2 was waiting
    waiter.join(PATIENCE)
    assert not waiter.is_alive()
    error = waited.get("error")
    assert isinstance(error, PhoneError) and error.code is Code.BUSY and error.data["holder"] == "agent-3", waited
    assert looks == []  # a loser does not run `adb devices` for a phone it cannot have


@pytest.mark.parametrize("phone", ["a", "b"], ids=["an open phone", "a locked phone"])
def test_a_lease_held_by_someone_else_is_busy_at_once_while_an_action_is_in_flight(rig, phone):
    actor, acted = rig.hold_an_action(phone)  # agent-1's lease is live, and its action is on the phone
    waiter, waited = rig.start("waiter", lambda: rig.phones.lease(phone, "agent-2"))
    waiter.join(PATIENCE)
    assert not waiter.is_alive() and not rig.waiting.is_set()  # answered while the action is still on the phone
    assert isinstance(waited["error"], PhoneError) and waited["error"].code is Code.BUSY, waited
    assert waited["error"].data["holder"] == "agent-1"
    rig.finish(actor)
    assert "error" not in acted and rig.events == ACTION


def test_a_release_by_someone_who_does_not_hold_the_phone_is_refused_without_waiting(rig):
    actor, acted = rig.hold_an_action("a")
    waiter, waited = rig.start("waiter", lambda: rig.phones.release("a", "agent-2"))
    waiter.join(PATIENCE)
    assert not waiter.is_alive() and not rig.waiting.is_set()  # answered while the action is still on the phone
    assert isinstance(waited["error"], PhoneError) and waited["error"].code is Code.NOT_HOLDER, waited
    rig.finish(actor)
    assert "error" not in acted and rig.events == ACTION


@pytest.mark.parametrize("args", [{"x": 2, "y": 2}, {"x": 2, "y": -5}], ids=["well-formed", "malformed"])
def test_an_action_by_someone_who_does_not_hold_the_phone_is_refused_without_waiting(rig, args):
    actor, acted = rig.hold_an_action("a")
    waiter, waited = rig.start("waiter", lambda: rig.phones.act("a", "agent-2", "tap", args))
    waiter.join(PATIENCE)
    assert not waiter.is_alive() and not rig.waiting.is_set()  # answered while the action is still on the phone
    assert isinstance(waited["error"], PhoneError) and waited["error"].code is Code.NOT_HOLDER, waited
    rig.finish(actor)
    assert "error" not in acted and rig.events == ACTION


def test_a_session_that_never_started_is_refused_without_waiting_and_recorded(rig):
    phones = rig.phones
    phones.leases.acquire("b", "agent-1", phones.cfg.ttl_seconds)  # a lease whose cold boot never happened
    with phones.leases.acting("b"):  # an operation on b is in flight
        waiter, waited = rig.start("waiter", lambda: phones.act("b", "agent-1", "tap", {"x": 2, "y": 2}))
        waiter.join(5)
        assert not waiter.is_alive() and not rig.waiting.is_set()  # it did not queue behind the operation
    assert isinstance(waited["error"], PhoneError) and waited["error"].code is Code.NOT_HOLDER, waited
    assert rig.events == [] and rig.audited("b") == ["tap"]  # nothing reached the phone; the refusal is on record


def test_an_action_after_the_unit_ran_again_is_not_holder(rig):
    actor, acted = rig.hold_an_action("b")  # a session bound to the unit's first run
    rig.finish(actor)
    assert "error" not in acted
    rig.boots += 1  # the unit stopped and ran again: the phone that is up is another boot
    with pytest.raises(PhoneError) as caught:
        rig.phones.act("b", "agent-1", "tap", {"x": 1, "y": 1})
    assert caught.value.code is Code.NOT_HOLDER
    assert rig.events == ACTION  # only the first action ever reached the phone
