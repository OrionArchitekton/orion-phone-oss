"""Leases: one holder per phone at a time. flock serializes every read-check-write."""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from orion_phone.codes import Code, PhoneError

_PHONE_RE = re.compile(r"[a-z0-9]{1,16}")  # config's phone-name rule, repeated: the name becomes a file name


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Lease:
    phone: str
    holder: str
    acquired_at: float
    expires_at: float
    ready: bool = False  # a locked phone's session has had its cold boot; a file without the field reads as False
    invocation: str = ""  # the unit's InvocationID for the boot that session started; "" when none is on record
    # The InvocationID of a boot that was seen with no secure screen lock: when it is the session's own boot (it equals
    # `invocation`), that boot never becomes usable, whatever screen lock is set afterwards (see
    # Phones._require_screen_lock). It belongs to the boot: a later boot has another id, and a file without the field
    # reads as "".
    unprotected: str = ""
    # The InvocationID of a boot that was seen at its lock screen: showing, over a secure screen lock. When it is the
    # session's own boot, an unlock of that boot is its owner's (see Phones._require_usable); a boot that was never seen
    # so is not used when it is found unlocked, because nothing shows that anyone unlocked it. Like `unprotected` it belongs
    # to the boot, and a file without the field reads as "" (not seen).
    seen_locked: str = ""


def _parse(raw: bytes) -> Lease:
    """A lease from file bytes. Anything but the known fields with sane values raises; `ready` may be missing."""
    lease = Lease(**json.loads(raw.decode("utf-8")))  # strict UTF-8: json.loads(bytes) also accepts UTF-16 and UTF-32
    if type(lease.phone) is not str or type(lease.holder) is not str:
        raise ValueError("phone and holder must be strings")
    if type(lease.ready) is not bool:
        raise ValueError("ready must be true or false")
    if type(lease.invocation) is not str:
        raise ValueError("invocation must be text")
    if type(lease.unprotected) is not str:
        raise ValueError("unprotected must be text")
    if type(lease.seen_locked) is not str:
        raise ValueError("seen_locked must be text")
    for stamp in (lease.acquired_at, lease.expires_at):
        if type(stamp) not in (int, float):  # bool is an int subclass but never a timestamp
            raise ValueError("timestamps must be numbers")
        iso(stamp)  # raises for NaN, infinity, and any value busy() could not print later
    return lease


class LeaseStore:
    """One JSON file per phone. Refusals are PhoneError: BUSY or NOT_HOLDER for ordinary conflicts, USAGE when a
    lease file is damaged. A phone name that is not a plain name is a caller bug and raises ValueError."""

    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = state_dir / "leases"
        self.clock = clock

    @contextmanager
    def _locked(self, phone: str, kind: str = "lock") -> Iterator[Path]:
        if not _PHONE_RE.fullmatch(phone):  # before any disk access: "../x" or "/abs" must not leave the lease dir
            raise ValueError(f"invalid phone name {phone!r}")
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.dir / f"{phone}.{kind}", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self.dir / f"{phone}.json"
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    @staticmethod
    def _read(path: Path, phone: str) -> Lease | None:
        """The lease on file, or None when there is none. A file that cannot be trusted raises USAGE: it must
        never read as free (acquire would overwrite it) or as busy (a bogus expiry would wedge the phone)."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return _parse(raw)
        except (ValueError, TypeError, RecursionError, OverflowError, OSError) as exc:  # OSError: fromtimestamp
            raise damaged(path, phone) from exc

    @staticmethod
    def _write(path: Path, lease: Lease) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            fh.write(json.dumps(asdict(lease)).encode())  # buffered: writes everything or raises, never a short write
            fh.flush()
            os.fsync(fh.fileno())  # durable before the rename makes it visible
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)  # the rename is an entry in the directory: a power loss can undo it until this is synced
        finally:
            os.close(directory)

    def acting(self, phone: str) -> AbstractContextManager[Path]:
        """The phone's action lock: one operation at a time touches the phone, so each screenshot shows only its
        own action and a shutdown never lands mid-action, and a lease never begins (a takeover included) while the
        previous holder's action is still on the phone: its lease can lapse mid-action, across a host suspend for
        instance. A separate lock from the lease's, so refusals (BUSY, NOT_HOLDER) never wait behind an action in flight.

        Lock order: this lock first, the lease lock inside it, never the reverse, and never twice in one process (a
        second open of the same lock file blocks forever). A lease read before taking it may only refuse; every
        decision that touches the phone uses lease state read while holding it. The lease watch of a locked phone's unit
        is one of those decisions: it tries for this lock (try_acting, which never waits) before it shuts the phone down
        for a lapsed lease, and reads the lease again under it.
        """
        return self._locked(phone, "act")

    def action_lock_file(self, phone: str) -> Path:
        """The file the phone's action lock is taken on (it is made when it is missing)."""
        return self.dir / f"{phone}.act"

    @contextmanager
    def try_acting(self, phone: str, make_dir: bool = False) -> Iterator[bool]:
        """The phone's action lock if it is free at this moment, without waiting for it: yields True while the lock is held
        here (it is let go of when the block ends) and False when an action, a release or a new session holds it. For a
        watcher that must never wait behind anyone: release() and a new session hold this lock while they stop the unit, and
        the process being stopped may be the watcher, which has to answer that stop whatever it is waiting for. The caller
        tries again later; two tries from one process are two opens of the file, so the second finds the first's hold.

        Like `read`, it makes no directory: raises the OSError of a lock file that cannot be opened (none, when nothing was
        ever started in this state directory). The lock file itself is made when it is missing, as acting() would make it.
        A caller that has no lease flow before it to have made the directory (install-units, stopping a phone) says
        `make_dir`, and gets the directory acting() makes, owner only, and the OSError of one that cannot be made."""
        if not _PHONE_RE.fullmatch(phone):  # before any disk access, as _locked does
            raise ValueError(f"invalid phone name {phone!r}")
        if make_dir:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.action_lock_file(phone), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except BlockingIOError:  # held by someone else
                got = False
            yield got
        finally:
            os.close(fd)  # closing the descriptor releases the flock, if it was taken

    def current(self, phone: str) -> Lease | None:
        """The lease on file, expired or not (peek hides an expired one)."""
        with self._locked(phone) as path:
            return self._read(path, phone)

    def read(self, phone: str) -> Lease | None:
        """The lease on file, expired or not, read WITHOUT the lease lock, and without making the directory or the lock
        file. For a watcher that must never wait behind anyone: release() holds the lease lock while a locked phone is
        stopped, and the process being stopped may be the watcher. Every write here is a whole-file rename, so this sees
        one complete lease, the one before a write or the one after, never a torn one. Raises as current() does: USAGE for
        a lease file that cannot be trusted, and the OSError of a file that cannot be read at all."""
        if not _PHONE_RE.fullmatch(phone):  # before any disk access, as _locked does
            raise ValueError(f"invalid phone name {phone!r}")
        return self._read(self.dir / f"{phone}.json", phone)

    def peek(self, phone: str) -> Lease | None:
        """The current unexpired lease, if any."""
        lease = self.current(phone)
        return lease if lease and lease.expires_at > self.clock() else None

    def acquire(self, phone: str, holder: str, ttl: float) -> tuple[Lease, Lease | None]:
        """Grant or extend a lease. Also returns another holder's expired lease that it took over, if any.

        Only the holder's own live lease is extended, and it keeps `ready` and its boot. Anything else starts a new
        session, not ready: a free phone, a takeover, or the holder's own lease taken again after it lapsed.
        """
        with self._locked(phone) as path:
            now = self.clock()
            current = self._read(path, phone)
            if current and current.holder != holder and current.expires_at > now:
                raise busy(current)
            if current and current.holder == holder and current.expires_at > now:
                lease, previous = replace(current, expires_at=now + ttl), None
            else:
                lease = Lease(phone, holder, now, now + ttl)
                previous = current if current and current.holder != holder else None
            self._write(path, lease)
            return lease, previous

    def renew(self, phone: str, holder: str, ttl: float) -> Lease:
        """Extend the caller's own unexpired lease, or refuse with NOT_HOLDER."""
        with self._locked(phone) as path:
            now = self.clock()
            current = self._read(path, phone)
            if not current or current.holder != holder or current.expires_at <= now:
                raise not_held(phone, holder, current)
            lease = replace(current, expires_at=now + ttl)
            self._write(path, lease)
            return lease

    def mark_ready(self, phone: str, holder: str, invocation: str) -> Lease:
        """Record that the phone has been cold booted for the caller's session, and which boot: the unit's InvocationID.

        NOT_HOLDER unless the lease on file is the caller's own (a lapsed one counts: nobody took it over). A boot has
        an id, so an empty one is a caller bug (ValueError): nothing without an id can be ready. A new boot starts without
        the marks of an earlier one (see mark_unprotected and mark_seen_locked).
        """
        if not invocation:
            raise ValueError("a boot has an InvocationID")
        with self._locked(phone) as path:
            current = self._read(path, phone)
            if not current or current.holder != holder:
                raise not_held(phone, holder, current)
            lease = replace(current, ready=True, invocation=invocation, unprotected="", seen_locked="")
            self._write(path, lease)
            return lease

    def mark_unprotected(self, phone: str, holder: str, invocation: str) -> Lease:
        """Record that the boot `invocation` of the caller's session was seen with no secure screen lock.

        A screen lock set later in that boot would leave the phone unlocked (nothing showed, so there was nothing to unlock)
        for whoever holds the lease, so a marked boot is never used. The mark is the boot's id, and it counts only while it
        equals the lease's own: a lease bound to another boot records nothing (the boot that was seen is not its boot), and
        mark_ready for a new boot clears it. NOT_HOLDER unless the lease on file is the caller's own; an empty id is a
        caller bug (ValueError), as for mark_ready. Written whole, like every lease write.
        """
        return self._mark_boot(phone, holder, invocation, "unprotected")

    def mark_seen_locked(self, phone: str, holder: str, invocation: str) -> Lease:
        """Record that the boot `invocation` of the caller's session was seen at its lock screen: showing, over a secure
        screen lock (the caller has just read both).

        That sighting is the evidence that an unlock of the boot is its owner's: a phone found unlocked in a boot that was
        never seen locked may have been unlocked by whoever set its screen lock, with no lock screen to unlock, so such a boot
        is not used. Nothing else records it: a boot still under way, a report that cannot be read, a lock screen over no
        screen lock, and a boot found unlocked are none of them a sighting. Kept as for mark_unprotected: the boot's id,
        counted only while it equals the lease's own, and cleared by mark_ready. Nothing is written when it is on file
        already (a lock screen that stays up is seen again and again), and the same refusals apply.
        """
        return self._mark_boot(phone, holder, invocation, "seen_locked")

    def _mark_boot(self, phone: str, holder: str, invocation: str, field: str) -> Lease:
        """Set one of the per-boot marks of the caller's lease to `invocation`, written whole."""
        if not invocation:
            raise ValueError("a boot has an InvocationID")
        with self._locked(phone) as path:
            current = self._read(path, phone)
            if not current or current.holder != holder:
                raise not_held(phone, holder, current)
            if current.invocation != invocation or getattr(current, field) == invocation:
                return current  # not the session's boot, or already on record: nothing to write
            lease = replace(current, **{field: invocation})
            self._write(path, lease)
            return lease

    def release(self, phone: str, holder: str) -> Lease:
        """Give back the caller's own lease, even a lapsed one. A lease another holder took over is theirs."""
        with self._locked(phone) as path:
            current = self._read(path, phone)
            if not current or current.holder != holder:
                raise not_held(phone, holder, current)
            path.unlink()
            return current

    @contextmanager
    def releasing(self, phone: str, holder: str) -> Iterator[Lease]:
        """Hold the phone's lock while the caller shuts it down; drop the lease only if that succeeds.

        A holder may release its own expired lease; a lease another holder took over is NOT_HOLDER.
        """
        with self._locked(phone) as path:
            current = self._read(path, phone)
            if not current or current.holder != holder:
                raise not_held(phone, holder, current)
            yield current
            path.unlink()


def busy(lease: Lease) -> PhoneError:
    return PhoneError(
        Code.BUSY,
        f"phone {lease.phone} is held by {lease.holder} until {iso(lease.expires_at)}",
        {"phone": lease.phone, "holder": lease.holder, "expires_at": iso(lease.expires_at)},
    )


def damaged(path: Path, phone: str) -> PhoneError:
    return PhoneError(
        Code.USAGE,
        f"lease file {path} is damaged; if no agent is using phone {phone}, delete it and retry",
        {"phone": phone, "path": str(path)},
    )


def not_held(phone: str, holder: str, current: Lease | None) -> PhoneError:
    if current is None:
        why = "no one holds it"
    elif current.holder != holder:
        why = f"it is held by {current.holder}"
    else:
        why = "the lease expired"
    return PhoneError(
        Code.NOT_HOLDER, f"{holder} does not hold phone {phone}: {why}; lease it first", {"phone": phone}
    )


def not_ready(phone: str, holder: str) -> PhoneError:
    return PhoneError(
        Code.NOT_HOLDER,
        f"{holder} holds phone {phone}, but this session's boot is not the one running (it never finished starting, "
        "or the phone was stopped or restarted since); lease it again",
        {"phone": phone},
    )
