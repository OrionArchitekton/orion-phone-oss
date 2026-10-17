"""Lease store rules that must hold below the CLI: the lease on file, release after a lapse, damaged files,
phone names, and durable writes. Like the race test, these are properties of the store itself."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading

import pytest

from orion_phone.codes import Code, PhoneError
from orion_phone.lease import Lease, LeaseStore

INVOCATION = "a" * 32  # what systemd gives one run of a unit: 32 hex characters


def raw_doc(**raw_fields: str) -> bytes:
    """A lease file as bytes. Field values are raw JSON text, so non-standard literals such as NaN can be written."""
    fields = {"phone": '"a"', "holder": '"h"', "acquired_at": "1000.0", "expires_at": "2000.0", **raw_fields}
    return ("{" + ", ".join(f'"{name}": {value}' for name, value in fields.items()) + "}").encode()


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> LeaseStore:
    return LeaseStore(tmp_path, clock)


def test_current_returns_the_lease_on_file_even_after_it_lapsed(store, clock):
    lease, _ = store.acquire("a", "h", 10)
    assert store.current("a") == lease
    clock.now += 11  # lapsed: peek hides it, current does not
    assert store.peek("a") is None
    assert store.current("a") == lease


def test_current_is_none_when_nothing_is_on_file(store):
    assert store.current("a") is None


def test_a_holder_can_release_its_own_lapsed_lease(store, clock):
    lease, _ = store.acquire("a", "h", 10)
    clock.now += 11  # lapsed, and nobody took it over: still the holder's to give back
    assert store.release("a", "h") == lease
    assert store.current("a") is None


def test_release_after_another_holder_took_over_is_not_holder(store, clock):
    store.acquire("a", "h", 10)
    clock.now += 11
    taken, previous = store.acquire("a", "x", 10)
    assert previous is not None and previous.holder == "h"
    with pytest.raises(PhoneError) as caught:
        store.release("a", "h")
    assert caught.value.code is Code.NOT_HOLDER
    assert "held by x" in caught.value.message
    assert store.current("a") == taken  # the new holder's lease is untouched


def test_release_refuses_a_caller_who_does_not_hold_the_phone(store):
    with pytest.raises(PhoneError) as nobody:
        store.release("a", "h")
    assert nobody.value.code is Code.NOT_HOLDER and "no one holds it" in nobody.value.message
    lease, _ = store.acquire("a", "h", 10)
    with pytest.raises(PhoneError) as other:
        store.release("a", "x")
    assert other.value.code is Code.NOT_HOLDER
    assert store.current("a") == lease


# Every way a lease file can be damaged. Each must be a USAGE error that names the file: never "free" (acquire
# would overwrite it), never BUSY (an Infinity expiry would wedge the phone), never a crash.
DAMAGED = {
    "empty": b"",
    "whitespace only": b" \n",
    "cut off mid-document": raw_doc()[:30],
    "missing closing brace": raw_doc()[:-1],
    "not json": b"not json at all",
    "non-utf8 bytes": b"\xff\xfe\xfd\x80",
    "utf16 encoded lease": raw_doc().decode().encode("utf-16"),  # valid JSON, but json.loads(bytes) would sniff it
    "array": b'[{"phone": "a"}]',
    "null": b"null",
    "string": b'"a lease"',
    "missing field": b'{"phone": "a", "acquired_at": 1000.0, "expires_at": 2000.0}',
    "extra field": raw_doc(extra="1"),
    "ready is a string": raw_doc(ready='"true"'),  # a real boolean or nothing
    "ready is a number": raw_doc(ready="1"),
    "ready is null": raw_doc(ready="null"),
    "ready is a list": raw_doc(ready="[true]"),
    "invocation is a number": raw_doc(invocation="1"),  # the boot id is text, or nothing
    "invocation is null": raw_doc(invocation="null"),
    "invocation is a list": raw_doc(invocation='["x"]'),
    "unprotected is a number": raw_doc(unprotected="1"),  # the id of a boot is text, or nothing
    "unprotected is null": raw_doc(unprotected="null"),
    "unprotected is a list": raw_doc(unprotected='["x"]'),
    "seen_locked is a number": raw_doc(seen_locked="1"),  # likewise: the id of a boot, or nothing
    "seen_locked is null": raw_doc(seen_locked="null"),
    "seen_locked is a list": raw_doc(seen_locked='["x"]'),
    "phone is a number": raw_doc(phone="1"),
    "holder is null": raw_doc(holder="null"),
    "holder is a list": raw_doc(holder='["h"]'),
    "acquired_at is a string": raw_doc(acquired_at='"1000"'),
    "expires_at is a bool": raw_doc(expires_at="true"),
    "expires_at is null": raw_doc(expires_at="null"),
    "expires_at is a list": raw_doc(expires_at="[2000]"),
    "Infinity": raw_doc(expires_at="Infinity"),
    "-Infinity": raw_doc(expires_at="-Infinity"),
    "NaN": raw_doc(acquired_at="NaN"),
    "float overflow": raw_doc(expires_at="1e999"),
    "huge integer": raw_doc(expires_at="1" + "0" * 400),
    "finite but unprintable": raw_doc(expires_at="1e18"),  # busy() renders expires_at, so this would crash it
    "deeply nested": b"[" * 100_000 + b"]" * 100_000,
}
READS = {
    "peek": lambda store: store.peek("a"),
    "current": lambda store: store.current("a"),
    "acquire": lambda store: store.acquire("a", "x", 60),
    "renew": lambda store: store.renew("a", "x", 60),
    "release": lambda store: store.release("a", "x"),
    "mark_ready": lambda store: store.mark_ready("a", "x", INVOCATION),
    "mark_unprotected": lambda store: store.mark_unprotected("a", "x", INVOCATION),
    "mark_seen_locked": lambda store: store.mark_seen_locked("a", "x", INVOCATION),
}


def write_lease_file(tmp_path, content: bytes):
    path = tmp_path / "leases" / "a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("read", READS)
@pytest.mark.parametrize("shape", DAMAGED)
def test_a_damaged_lease_file_is_a_usage_error_never_free_busy_or_a_crash(tmp_path, store, shape, read):
    path = write_lease_file(tmp_path, DAMAGED[shape])
    with pytest.raises(PhoneError) as caught:
        READS[read](store)
    error = caught.value
    assert error.code is Code.USAGE
    assert str(path) in error.message and "delete it and retry" in error.message
    assert error.data["phone"] == "a" and error.data["path"] == str(path)
    assert path.read_bytes() == DAMAGED[shape]  # nothing was healed, overwritten, or deleted behind the operator's back


def test_a_well_formed_lease_file_still_reads_normally(tmp_path, store):
    write_lease_file(tmp_path, raw_doc())
    lease = store.current("a")
    assert lease == Lease("a", "h", 1000.0, 2000.0)
    assert store.peek("a") == lease
    with pytest.raises(PhoneError) as caught:  # a held phone is BUSY, which stays distinct from a damaged file
        store.acquire("a", "x", 60)
    assert caught.value.code is Code.BUSY


def test_integer_timestamps_are_well_formed(tmp_path):
    store = LeaseStore(tmp_path, lambda: 1000)  # a clock that returns ints writes ints
    lease, _ = store.acquire("a", "h", 60)
    assert (lease.acquired_at, lease.expires_at) == (1000, 1060)
    assert store.peek("a") == lease


BAD_PHONE_NAMES = ["../x", "/abs", "", "A", "a.b", "a-b", "a b", "a" * 17, "a\n", "\N{LATIN SMALL LETTER E WITH ACUTE}"]
CALLS = {
    "peek": lambda store, phone: store.peek(phone),
    "current": lambda store, phone: store.current(phone),
    "acquire": lambda store, phone: store.acquire(phone, "h", 60),
    "renew": lambda store, phone: store.renew(phone, "h", 60),
    "release": lambda store, phone: store.release(phone, "h"),
    "mark_ready": lambda store, phone: store.mark_ready(phone, "h", INVOCATION),
}


# The phone name becomes a file name. Config already restricts it; the store does not trust its callers.
@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("name", BAD_PHONE_NAMES, ids=repr)
def test_a_phone_name_that_is_not_a_plain_name_is_refused_before_touching_disk(tmp_path, name, call):
    state = tmp_path / "state"
    with pytest.raises(ValueError, match="invalid phone name"):
        CALLS[call](LeaseStore(state), name)
    assert not state.exists()  # not even the leases directory was created
    assert list(tmp_path.iterdir()) == []  # and nothing landed beside it, as "../x" would have


@pytest.mark.parametrize("name", ["a", "b", "0", "phone01", "a" * 16])
def test_plain_phone_names_are_accepted(store, name):
    lease, _ = store.acquire(name, "h", 60)
    assert store.peek(name) == lease


# The store repeats config's name rule on purpose (defense in depth), so a change to one must show up here.
AGREEMENT_NAMES = [*BAD_PHONE_NAMES, "a", "0", "a" * 16, "phone01", "a_b", "ab-cd", "a" * 15 + "Z"]


@pytest.mark.parametrize("name", AGREEMENT_NAMES, ids=repr)
def test_the_store_and_the_config_agree_on_what_a_phone_name_is(tmp_path, name):
    from orion_phone.config import NAME_RE

    try:
        LeaseStore(tmp_path).peek(name)
        store_accepts = True
    except ValueError:
        store_accepts = False
    assert store_accepts == bool(NAME_RE.fullmatch(name))


def record_syncs(monkeypatch) -> list[tuple[str, int, int | None]]:
    """Every os.fsync and os.replace from now on, in order, as (kind, inode, size): a sync of a file, a sync of a
    directory (told apart by os.fstat of the descriptor), or the rename (the inode of the file being renamed)."""
    events: list[tuple[str, int, int | None]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        info = os.fstat(fd)
        events.append(("fsync directory" if stat.S_ISDIR(info.st_mode) else "fsync file", info.st_ino, info.st_size))
        real_fsync(fd)

    def spy_replace(src, dst):
        events.append(("replace", os.stat(src).st_ino, None))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    return events


# A crash or power loss between the rename and the data reaching disk would leave an empty lease file.
def test_the_lease_is_synced_to_disk_before_it_is_renamed_into_place(tmp_path, monkeypatch):
    events = record_syncs(monkeypatch)
    LeaseStore(tmp_path).acquire("a", "h", 60)
    assert [kind for kind, _, _ in events][:2] == ["fsync file", "replace"]
    assert events[0][1] == events[1][1], "the file that was synced must be the file renamed into place"
    assert events[0][2] == (tmp_path / "leases" / "a.json").stat().st_size, "the buffer must be flushed before the sync"


# The rename itself lives in the directory, and a power loss can undo a rename the directory has not been synced for.
def test_the_leases_directory_is_synced_after_the_rename(tmp_path, monkeypatch):
    events = record_syncs(monkeypatch)
    LeaseStore(tmp_path).acquire("a", "h", 60)
    assert [kind for kind, _, _ in events] == ["fsync file", "replace", "fsync directory"]
    assert events[2][1] == (tmp_path / "leases").stat().st_ino, "the directory that holds the lease must be the one synced"


def test_a_short_write_never_replaces_the_lease_on_file(tmp_path, short_write):
    store = LeaseStore(tmp_path)
    before, _ = store.acquire("a", "h", 60)
    assert short_write(16, lambda: store.acquire("a", "h", 120)) == "raised"
    assert store.current("a") == before


def test_releasing_drops_the_lease_only_when_the_shutdown_succeeds(tmp_path):
    store = LeaseStore(tmp_path)
    store.acquire("b", "agent-1", 60)
    with pytest.raises(RuntimeError):
        with store.releasing("b", "agent-1"):
            raise RuntimeError("stop failed")
    assert store.current("b").holder == "agent-1"
    with store.releasing("b", "agent-1"):
        pass
    assert store.current("b") is None


def test_releasing_refuses_another_holder_and_allows_a_lapsed_own_lease(tmp_path):
    store = LeaseStore(tmp_path)
    store.acquire("b", "agent-1", -1)  # already expired
    with pytest.raises(PhoneError) as err:
        with store.releasing("b", "agent-2"):
            pass
    assert err.value.code is Code.NOT_HOLDER
    with store.releasing("b", "agent-1"):
        pass
    assert store.current("b") is None


def test_a_new_lease_is_not_ready_and_a_live_extension_keeps_the_flag(store, clock):
    lease, _ = store.acquire("a", "h", 10)
    assert lease.ready is False  # the cold boot has not happened
    assert store.mark_ready("a", "h", INVOCATION).ready is True
    assert store.current("a").ready is True  # and it is on file
    clock.now += 5
    extended, previous = store.acquire("a", "h", 10)  # the holder asks again while its lease is live
    assert (extended.ready, previous) == (True, None)
    assert store.renew("a", "h", 10).ready is True
    assert store.current("a").ready is True


def test_a_lapsed_lease_taken_again_or_taken_over_starts_not_ready(store, clock):
    store.acquire("a", "h", 10)
    store.mark_ready("a", "h", INVOCATION)
    clock.now += 11  # lapsed: the same holder's next lease is a new session, not an extension
    again, previous = store.acquire("a", "h", 10)
    assert (again.ready, previous) == (False, None)
    assert again.acquired_at == clock.now
    store.mark_ready("a", "h", INVOCATION)
    clock.now += 11
    taken, previous = store.acquire("a", "x", 10)  # another holder takes over a lapsed lease that was ready
    assert (taken.ready, previous.holder) == (False, "h")
    assert store.current("a").ready is False


def test_mark_ready_refuses_a_caller_who_does_not_hold_the_phone(store):
    with pytest.raises(PhoneError) as nobody:
        store.mark_ready("a", "h", INVOCATION)
    assert nobody.value.code is Code.NOT_HOLDER and "no one holds it" in nobody.value.message
    store.acquire("a", "h", 10)
    with pytest.raises(PhoneError) as other:
        store.mark_ready("a", "x", INVOCATION)
    assert other.value.code is Code.NOT_HOLDER and "held by h" in other.value.message
    assert store.current("a").ready is False  # the holder's lease is untouched


def test_mark_ready_accepts_the_holders_own_lapsed_lease(store, clock):
    store.acquire("a", "h", 10)
    clock.now += 11  # nobody took it over
    assert store.mark_ready("a", "h", INVOCATION).ready is True


def test_a_lease_file_from_before_the_ready_flag_reads_as_not_ready(tmp_path, store):
    path = write_lease_file(tmp_path, raw_doc())  # no "ready" key
    assert store.current("a").ready is False
    assert store.mark_ready("a", "h", INVOCATION).ready is True
    assert json.loads(path.read_text())["ready"] is True  # the flag is written as a real boolean


def test_ready_is_read_back_from_the_file(tmp_path, store):
    write_lease_file(tmp_path, raw_doc(ready="true"))
    assert store.current("a").ready is True


def test_mark_ready_binds_the_session_to_one_boot(store, clock):
    store.acquire("a", "h", 10)
    marked = store.mark_ready("a", "h", INVOCATION)
    assert (marked.ready, marked.invocation) == (True, INVOCATION)
    assert store.current("a") == marked  # it is on file
    clock.now += 5
    extended, _ = store.acquire("a", "h", 10)  # a live extension keeps the boot
    assert (extended.ready, extended.invocation) == (True, INVOCATION)
    assert store.renew("a", "h", 10).invocation == INVOCATION


def test_a_new_session_has_no_boot(store, clock):
    store.acquire("a", "h", 10)
    store.mark_ready("a", "h", INVOCATION)
    clock.now += 11
    again, _ = store.acquire("a", "h", 10)  # lapsed, taken again by the same holder
    assert (again.ready, again.invocation) == (False, "")
    store.mark_ready("a", "h", INVOCATION)
    clock.now += 11
    taken, _ = store.acquire("a", "x", 10)  # lapsed, taken over by another
    assert (taken.ready, taken.invocation) == (False, "")


def test_mark_ready_refuses_an_empty_boot(store):
    store.acquire("a", "h", 10)
    with pytest.raises(ValueError):  # a boot has an id: nothing without one can be ready
        store.mark_ready("a", "h", "")
    assert store.current("a").ready is False


def test_a_lease_file_from_before_boots_were_recorded_has_none(tmp_path, store):
    write_lease_file(tmp_path, raw_doc(ready="true"))  # ready, with no boot on record
    lease = store.current("a")
    assert (lease.ready, lease.invocation) == (True, "")


# Two things are marked, by the boot's id, in the lease. A boot that was seen with no secure screen lock (`unprotected`): a
# screen lock set afterwards would leave that boot unlocked for whoever holds the lease (nothing showed, so there was nothing
# to unlock), so the mark is what keeps the boot from ever being used. And a boot that was seen at its lock screen, showing
# over a secure screen lock (`seen_locked`): the only sighting that lets the unlock after it count as its owner's. Each
# belongs to the boot: a later boot has another id, and a new session has neither. They are kept apart: marking one never
# marks the other.

OTHER_BOOT = "b" * 32
MARKS = {"unprotected": "mark_unprotected", "seen_locked": "mark_seen_locked"}  # the field, and the call that sets it


@pytest.mark.parametrize("field", MARKS)
def test_a_mark_records_the_boot_in_the_lease_and_a_live_extension_keeps_it(store, clock, field):
    store.acquire("a", "h", 10)
    store.mark_ready("a", "h", INVOCATION)
    assert getattr(store.current("a"), field) == ""  # a boot is not marked until it was seen so
    marked = getattr(store, MARKS[field])("a", "h", INVOCATION)
    assert getattr(marked, field) == INVOCATION and store.current("a") == marked  # and it is on file
    clock.now += 5
    assert getattr(store.acquire("a", "h", 10)[0], field) == INVOCATION  # the holder asks again while live: the same boot
    assert getattr(store.renew("a", "h", 10), field) == INVOCATION


@pytest.mark.parametrize("field", MARKS)
def test_a_mark_belongs_to_its_boot_and_a_new_boot_or_session_starts_without_it(store, clock, field):
    mark = getattr(store, MARKS[field])
    store.acquire("a", "h", 10)
    store.mark_ready("a", "h", INVOCATION)
    mark("a", "h", INVOCATION)
    rebooted = store.mark_ready("a", "h", OTHER_BOOT)  # the phone was started again for this session
    assert (rebooted.invocation, getattr(rebooted, field)) == (OTHER_BOOT, "")
    mark("a", "h", OTHER_BOOT)
    clock.now += 11  # lapsed: the same holder's next lease is a new session, then another holder's
    assert getattr(store.acquire("a", "h", 10)[0], field) == ""
    store.mark_ready("a", "h", INVOCATION)
    mark("a", "h", INVOCATION)
    clock.now += 11
    assert getattr(store.acquire("a", "x", 10)[0], field) == ""


def test_the_two_marks_are_kept_apart(store):
    store.acquire("a", "h", 10)
    store.mark_ready("a", "h", INVOCATION)
    seen = store.mark_seen_locked("a", "h", INVOCATION)
    assert (seen.seen_locked, seen.unprotected) == (INVOCATION, "")
    both = store.mark_unprotected("a", "h", INVOCATION)
    assert (both.seen_locked, both.unprotected) == (INVOCATION, INVOCATION)  # one mark does not undo the other
    assert store.current("a") == both


@pytest.mark.parametrize("field", MARKS)
def test_a_mark_refuses_a_caller_who_does_not_hold_the_phone(store, field):
    mark = getattr(store, MARKS[field])
    with pytest.raises(PhoneError) as nobody:
        mark("a", "h", INVOCATION)
    assert nobody.value.code is Code.NOT_HOLDER and "no one holds it" in nobody.value.message
    store.acquire("a", "h", 10)
    store.mark_ready("a", "h", INVOCATION)
    with pytest.raises(PhoneError) as other:
        mark("a", "x", INVOCATION)
    assert other.value.code is Code.NOT_HOLDER and "held by h" in other.value.message
    assert getattr(store.current("a"), field) == ""  # the holder's lease is untouched


@pytest.mark.parametrize("field", MARKS)
def test_a_mark_refuses_an_empty_boot(store, field):
    store.acquire("a", "h", 10)
    with pytest.raises(ValueError):  # a boot has an id: nothing without one can be marked
        getattr(store, MARKS[field])("a", "h", "")


@pytest.mark.parametrize("field", MARKS)
def test_a_mark_records_nothing_for_a_boot_the_lease_is_not_bound_to(store, field):
    store.acquire("a", "h", 10)
    store.mark_ready("a", "h", INVOCATION)
    unchanged = getattr(store, MARKS[field])("a", "h", OTHER_BOOT)  # the boot that was seen is not the session's boot
    assert getattr(unchanged, field) == "" and getattr(store.current("a"), field) == ""


@pytest.mark.parametrize("field", MARKS)
def test_a_lease_file_from_before_the_marks_has_none_and_a_mark_is_written_as_text(tmp_path, store, field):
    path = write_lease_file(tmp_path, raw_doc(ready="true", invocation=f'"{INVOCATION}"'))  # neither key is there
    assert getattr(store.current("a"), field) == ""
    assert getattr(getattr(store, MARKS[field])("a", "h", INVOCATION), field) == INVOCATION
    assert json.loads(path.read_text())[field] == INVOCATION


# `read` is what a watcher uses: the lease on file, with no lock and nothing created, so that it never waits behind anyone.


def test_read_returns_the_lease_on_file_even_after_it_lapsed(store, clock):
    lease, _ = store.acquire("a", "h", 10)
    clock.now += 11
    assert store.read("a") == lease


def test_read_is_none_when_nothing_is_on_file_and_it_makes_nothing(store, tmp_path):
    assert store.read("a") is None
    assert not (tmp_path / "leases").exists()  # not the directory, not a lock file: a look leaves no trace


def test_read_does_not_wait_for_the_lease_lock(store, tmp_path):
    lease, _ = store.acquire("a", "h", 10)
    fd = os.open(tmp_path / "leases" / "a.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # someone is in the middle of a lease operation, or a release is stopping the phone
    try:
        seen: list[object] = []
        thread = threading.Thread(target=lambda: seen.append(store.read("a")), daemon=True)
        thread.start()
        thread.join(10)
        assert not thread.is_alive(), "read waited for the lease lock"
        assert seen == [lease]
    finally:
        os.close(fd)


def test_read_refuses_a_lease_file_that_cannot_be_trusted_like_current_does(store, tmp_path):
    (tmp_path / "leases").mkdir()
    (tmp_path / "leases" / "a.json").write_text("{ not a lease")
    with pytest.raises(PhoneError) as caught:
        store.read("a")
    assert caught.value.code is Code.USAGE and "damaged" in caught.value.message


def test_read_checks_the_phone_name_before_it_touches_the_disk(store, tmp_path):
    for name in ("../x", "/abs", "A", ""):
        with pytest.raises(ValueError):
            store.read(name)
    assert not (tmp_path / "leases").exists()


# try_acting is the watcher's way to the phone's action lock: it never waits, because the process being stopped by a release
# (which holds that lock while it stops the unit) may be the watcher itself.


def try_from_another_thread(store, phone: str, within: float = 5.0) -> bool:
    """What try_acting yields when asked from another thread (another open of the lock file, as another process makes one),
    and the asker is given `within` seconds to answer: an implementation that waits for the lock fails the test instead of
    hanging it."""
    outcome: list[bool] = []

    def attempt() -> None:
        with store.try_acting(phone) as got:
            outcome.append(got)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(within)
    assert outcome, "try_acting waited for the lock instead of answering at once"
    return outcome[0]


def test_try_acting_holds_the_action_lock_while_the_block_runs_and_lets_go_after(store, tmp_path):
    store.acquire("a", "h", 10)  # the leases directory and the lock files exist once a session has been started
    with store.try_acting("a") as got:
        assert got is True
        assert try_from_another_thread(store, "a") is False  # held: another try is refused, not waited for
    assert try_from_another_thread(store, "a") is True  # and the lock was let go of when the block ended


def test_try_acting_is_refused_while_an_action_holds_the_lock_and_does_not_wait(store):
    holder_ready, release = threading.Event(), threading.Event()

    def action():  # an action in flight, as act() holds the lock
        with store.acting("a"):
            holder_ready.set()
            release.wait(10)

    worker = threading.Thread(target=action, daemon=True)
    worker.start()
    assert holder_ready.wait(10)
    try:
        assert try_from_another_thread(store, "a") is False
    finally:
        release.set()
        worker.join(10)
    assert try_from_another_thread(store, "a") is True


def test_the_action_lock_is_not_the_lease_lock(store):
    store.acquire("a", "h", 10)
    with store.acting("a"):  # an action in flight does not stop the lease from being read or written
        assert store.current("a") is not None
    with store.try_acting("a") as got:
        assert got is True
        assert store.current("a") is not None  # and the lease lock is free while the action lock is held here


def test_try_acting_makes_no_directory_and_raises_where_the_lock_cannot_be_opened(store, tmp_path):
    with pytest.raises(OSError):  # no leases directory at all: nothing was ever started, and the lock cannot be had
        with store.try_acting("a"):
            raise AssertionError("the block ran")
    assert not (tmp_path / "leases").exists()


def test_try_acting_checks_the_phone_name_before_it_touches_the_disk(store, tmp_path):
    for name in ("../x", "/abs", "A", ""):
        with pytest.raises(ValueError):
            with store.try_acting(name):
                pass
    assert not (tmp_path / "leases").exists()
