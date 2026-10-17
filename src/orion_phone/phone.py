"""What each command does. Every action checks the lease first and leaves an audit record."""

from __future__ import annotations

import os
import re
import secrets
import shlex
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable
from xml.etree.ElementTree import ParseError

from orion_phone import screen, services
from orion_phone.adb import Adb, OutputOverLimit
from orion_phone.audit import Audit
from orion_phone.codes import Code, PhoneError, Result
from orion_phone.config import Config, PhoneConfig
from orion_phone.lease import Lease, LeaseStore, busy, iso, not_held, not_ready

MAX_COORD = 10000
# Always fullmatch these: agent input reaches a device shell, and `$` alone accepts a trailing newline.
KEY_RE = re.compile(r"KEYCODE_[A-Z0-9_]{1,40}")
PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
URL_RE = re.compile(r"https?://[^\s'\"\\]{1,2000}")
MAX_PACKAGE = 255  # Android makes the package name a directory name, and Linux caps those at 255 bytes
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
UI_DUMP = "/data/local/tmp/orion_ui.xml"  # shell-owned (0771): no app can plant a dump here
UI_DUMP_RETRY_WAIT = 1.0  # seconds before the one more try of a dump that wrote nothing (see Phones._dump_ui)
UI_DUMP_WRITTEN = "orion-ui-dump-written"  # the fixed part of the marker the dump command prints once the dump has content
UI_DUMP_MAX_BYTES = 4 * 1024 * 1024  # a dump is never read past this: a hostile screen can inflate its own


# How long a stopped phone may take to leave `adb devices`, and how often to look, in seconds.
# ORION_PHONE_GONE_WAIT overrides the wait.
GONE_WAIT = 5.0
GONE_POLL = 0.2

# What a locked phone's image must report. File-based encryption keeps app data sealed until the first unlock after boot,
# and a build that is not debuggable gives adb no root shell: the locked tier rests on both.
LOCKED_CRYPTO_TYPE = "file"  # ro.crypto.type
LOCKED_DEBUGGABLE = "0"  # ro.debuggable
SHOWN_MAX = 32  # characters of a reported value kept in a message or a record
RESTART_REASON = "restart-needed"  # the state an action says, and what the audit record of a lease that restarts a phone gives
RESTART_CAUSES = {  # why a boot is not used, by the stable short value the replies and the audit records carry, and the end of
    # the sentence about the boot that a lease's reply says
    "seen-with-none": "was seen with no screen lock, and one was set after that",
    "never-seen-locked": "was never seen at its lock screen",
}


class UnitNotInstalled(PhoneError):
    """A locked phone's unit is not installed, and the phone was shown to be gone all the same: there was no unit to stop, and
    the looks that end every stop (an emulator kill, adb, the ports) found nothing running. The way out is install-units. A
    caller that only needs the phone off (an unsafe image) has it, and may treat this as done."""


class UnsafeImage(PhoneError):
    """A locked phone booted from an image that cannot give the locked tier its protections. By the time this is raised
    the phone is shut down (or could not be: the code is then the shutdown's, and the message says both), so its session
    is over: whoever catches it frees the lease and records data["reported"], what the phone said about itself."""


class RestartNeeded(PhoneError):
    """A locked phone's boot is not one that can be used, and starting the phone again is the way out: the phone was found
    unlocked, with a screen lock, in a boot that was never seen at its lock screen (nothing shows that its owner unlocked it:
    a screen lock set during a boot leaves the phone unlocked), or it was seen with no screen lock and has one now. The look
    that finds this refuses with state `restart-needed`. A lease that catches it restarts the phone, as a new session starts
    it; an action only refuses. Which of the two it was is its `cause` (a key of RESTART_CAUSES): the reply and the audit
    record carry it, and a lease's reply says it in words (`because`)."""

    def __init__(self, message: str, data: dict[str, Any], cause: str) -> None:
        super().__init__(Code.NEEDS_OPERATOR, message, {**data, "cause": cause})
        self.cause = cause

    @property
    def because(self) -> str:
        return RESTART_CAUSES[self.cause]


def _line_of(text: str) -> str:
    """What a one-line shell command printed, less its line end and nothing else: ONE terminal sequence, a newline or the CR
    LF a pty makes of one, never both of two and never a lone carriage return. The text comes from the phone, so a value is
    not made to match by what is trimmed from it: `str.strip` would take a line or paragraph separator, a no-break space, or
    a second newline off `file`, and removing a carriage return and then a newline in turn would take a lone carriage return
    (`file` and CR) or a doubled one off it as well; `file` is what is checked, and all of those are something else."""
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _shown(value: str) -> str:
    """What a phone reported, as text that is safe to print and to store. It comes from the phone, so it is untrusted:
    it is quoted with its control characters escaped (no terminal escape or line break survives) and cut short."""
    return repr(value if len(value) <= SHOWN_MAX else value[:SHOWN_MAX] + "...")


def write_private(path: Path, data: bytes) -> None:
    """An owner-only file holding all of data. Buffered: a short write (a full disk) raises, never truncates."""
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())  # an audit record never points at a screenshot that a crash emptied


def _coords(*values: Any) -> list[int]:
    out = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COORD:
            raise PhoneError(Code.USAGE, f"coordinates must be whole numbers from 0 to {MAX_COORD}")
        out.append(value)
    return out


def _stopper(cfg: Config, again: str = "install-units") -> services.StopPhone:
    """The `stop` that services is handed: a locked phone it stops is stopped as a lease stops one, and reported stopped only
    once it is shown gone (Phones.stop_for_install).

    It is here, and services is handed the function, because phone.py imports services and not the other way round, and the
    proof needs adb, the phone's action lock and the lease store. Phones is built when a phone has to be stopped, not before:
    an install that stops nothing does not touch the state directory, as it never did. A state directory that cannot be used
    (it holds every lock) is a stop that cannot begin, said as one: StopNotAttempted, which names the phone's action lock, as
    every stop that cannot begin does, and says why it cannot be taken. A stop that fails ends its advice with "run `again`
    again": install-units, or setup for the look after its own reload."""
    built: list[Phones] = []

    def stop(phone: PhoneConfig) -> None:
        if not built:
            try:
                built.append(Phones(cfg))
            except PhoneError as exc:
                lock = LeaseStore(cfg.state_dir).action_lock_file(phone.name)  # where it would be, the directory usable
                raise services.StopNotAttempted(
                    f"its action lock {lock} cannot be taken: {exc.message}",
                    {**exc.data, "phone": phone.name, "path": str(lock)},
                ) from None
        built[0].stop_for_install(phone, again)

    return stop


def install_units(cfg: Config) -> services.Installed:
    """services.install_units with the stop it needs (see _stopper). The commands (`install-units`, and `setup`, which installs
    the units) call this, never services.install_units, which has no default for it."""
    return services.install_units(cfg, _stopper(cfg))


def reload_and_look(cfg: Config, stopped: list[str], again: str) -> None:
    """services.reload_and_look with the stop it needs, for setup's own reload after the links it makes (see there): the look
    stops a locked phone as install-units does, and adds it to `stopped`. `again` is the command a failure says to run."""
    services.reload_and_look(cfg, stopped, _stopper(cfg, again), again)


class Phones:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.clock = clock
        try:
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(cfg.state_dir, 0o700)
        except OSError as exc:
            raise PhoneError(
                Code.USAGE,
                f"cannot use state directory {cfg.state_dir}: {exc.strerror or exc}; "
                "set ORION_PHONE_STATE to a directory you own",
            ) from None
        self.leases = LeaseStore(cfg.state_dir, clock)
        self.audit = Audit(cfg.state_dir, clock)

    def adb(self, phone: PhoneConfig) -> Adb:
        return Adb(self.cfg.adb, phone.serial)

    def health(self, name: str) -> Result:
        phone = self.cfg.phone(name)
        base = {"phone": name, "tier": phone.tier, "serial": phone.serial}
        try:
            return self._probe(name, self.adb(phone), base)
        except PhoneError as exc:
            exc.data = {**base, **exc.data}  # a failed adb call knows only its argv, never which phone it was for
            raise

    def _probe(self, name: str, adb: Adb, base: dict[str, str]) -> Result:
        if not adb.is_running():
            raise PhoneError(Code.PHONE_DOWN, f"phone {name} is not running", base)
        if _line_of(adb.shell("getprop sys.boot_completed")) != "1":
            raise PhoneError(Code.UNHEALTHY, f"phone {name} is running but has not finished booting", base)
        adb.shell("input keyevent KEYCODE_WAKEUP")
        try:
            blank = screen.raw_frame_is_blank(adb.exec_out("screencap"))
        except ValueError as exc:
            raise PhoneError(Code.UNHEALTHY, f"phone {name} returned an unreadable frame: {exc}", base) from None
        if blank:
            raise PhoneError(Code.UNHEALTHY, f"phone {name} shows a blank screen", base)
        return Result(Code.OK, f"phone {name} is healthy", {**base, "healthy": True})

    def status(self, name: str | None) -> Result:
        if name is not None:
            return self.health(name)
        phones = []
        for each in sorted(self.cfg.phones):
            try:
                phones.append({**self.health(each).data, "code": Code.OK.name})
            except PhoneError as exc:
                if exc.code is Code.USAGE:  # setup is broken (no adb): it is not about this one phone
                    raise PhoneError(exc.code, exc.message) from None
                phones.append(
                    {**exc.data, "phone": each, "code": exc.code.name, "healthy": False, "message": exc.message}
                )
        return Result(Code.OK, f"listed {len(phones)} phones", {"phones": phones})

    def lease(self, name: str, holder: str) -> Result:
        phone = self.cfg.phone(name)
        self._refuse_if_held_by_another(name, holder)  # before the action lock, which may be held for long: BUSY at once
        if phone.tier == "open":
            with self.leases.acting(name):  # a takeover never begins while the previous holder's action is on the phone
                self._refuse_if_held_by_another(name, holder)  # again: someone may have claimed it during the wait
                self._require_usable(phone, for_lease=False)  # after the wait: the phone as it is now
                lease, previous = self.leases.acquire(name, holder, self.cfg.ttl_seconds)  # which decides, last
        else:
            with self.leases.acting(name):  # a new session never stops or boots the phone under an action in flight
                lease, previous = self._start_locked_session(phone, holder)
        extra = {"took_over_from": previous.holder} if previous else {}
        self.audit.record(name, holder, "lease", extra=extra)
        return Result(
            Code.OK,
            f"{holder} holds phone {name} until {iso(lease.expires_at)}",
            {"phone": name, "holder": holder, "expires_at": iso(lease.expires_at), **extra},
        )

    def _refuse_if_held_by_another(self, name: str, holder: str) -> None:
        """BUSY when someone else holds an unexpired lease. A look only: the claim itself is decided by acquire, under the
        lease lock. Taken again inside the action lock, it keeps an attempt that lost the race to a claim made while it
        waited from running `adb devices` for a phone it cannot have."""
        held = self.leases.peek(name)
        if held and held.holder != holder:
            raise busy(held)

    def _start_locked_session(self, phone: PhoneConfig, holder: str) -> tuple[Lease, Lease | None]:
        """Reserve the locked phone for this holder, then require the operator's unlock.

        A session is bound to one boot of the phone's unit: the lease records `ready` and the unit's InvocationID, set
        only once the phone has been stopped for certain and started again. A new session (a free phone, a takeover, a
        lease taken again after it lapsed) has neither, and neither has one whose boot was cut short (the lease is
        written before the boot, so a crash, a kill, or a timeout leaves it behind) or whose boot is over (the unit
        stopped, or ran again, since). Such a session stops the phone for certain and starts it again before anything
        else, so an unlocked phone never reaches a session whose owner did not unlock it. Between the stop and the start
        the loaded unit is read: a unit without its session cap is never started. That is the only start there is (and
        the one more, below): an attempt that finds its session's boot still on record starts nothing, even if the phone
        is not running by the time it looks (its unit stopped, by the session cap or a crash, since the look at the unit).
        It answers `starting`, and the next attempt, whose boot is gone, takes the path above in full.

        The one more: a boot that cannot be used (RestartNeeded: found unlocked without ever having been seen at its lock
        screen, or seen with no screen lock and having one now) is started again by the lease that finds it, by that same
        path, stop, read and start, and the reply is `starting` too. Otherwise the owner, who was signed in when the PIN was
        set, would have handed the lease holder an unlocked phone.
        """
        lease, previous = self.leases.acquire(phone.name, holder, self.cfg.ttl_seconds)  # first: a loser gets BUSY untouched
        restart: RestartNeeded | None = None
        try:
            if not self._booted(phone, lease):
                self._cold_boot(phone, holder)
            try:
                self._require_usable(phone, for_lease=True, lease=lease)
            except RestartNeeded as unusable:
                restart = unusable
                self._cold_boot(phone, holder, restart)
        except PhoneError as exc:
            if exc.code is not Code.NEEDS_OPERATOR:
                self.leases.release(phone.name, holder)  # the session could not start; free it
                exc.data = {"phone": phone.name, **exc.data}  # a failed adb or systemctl call knows only its own arguments
                reported = {"reported": exc.data["reported"]} if isinstance(exc, UnsafeImage) else None
                # a failed cold boot is on record, with what the phone said about its image when that was the failure, and a
                # failed restart with why the phone was being restarted: the reason, the state that sent it to restart, and the
                # cause
                extra = {**(reported or {})}
                if restart:
                    extra |= {"restart": RESTART_REASON, "state": RESTART_REASON, "cause": restart.cause}
                self.audit.record(phone.name, holder, "lease", code=exc.code.name, extra=extra or None)
                raise
            extra = {"took_over_from": previous.holder} if previous else {}
            exc.data.update(holder=holder, expires_at=iso(lease.expires_at), **extra)
            why = {key: exc.data[key] for key in ("restart", "cause") if key in exc.data}  # a restart says what it was for
            self.audit.record(
                phone.name, holder, "lease", code=exc.code.name, extra={"state": exc.data.get("state"), **why, **extra}
            )
            raise
        return lease, previous

    def _cold_boot(self, phone: PhoneConfig, holder: str, restart: RestartNeeded | None = None) -> None:
        """Stop the phone for certain, read the loaded unit, start it, and bind the session to that boot. Always raises: the
        reply to a boot that has only just been started is `starting` (see _starting). A new session, a takeover and a
        restart (`restart` is why the last boot is not used) all go through here, so a restart is stopped, checked for its
        cap and started as any start is."""
        self._stop_and_verify(phone)
        self._require_capped_unit(phone)  # after the stop (a refused phone is never left running), before the start
        services.systemctl("start", phone.unit_name)
        invocation = services.invocation_id(phone.unit_name)
        if not invocation:  # started, yet not active: there is no boot to bind the session to
            raise PhoneError(
                Code.PHONE_DOWN,
                f"{phone.unit_name} is not active after it was started",
                {"phone": phone.name, "unit": phone.unit_name},
            )
        self.leases.mark_ready(phone.name, holder, invocation)  # last: until bound, a retry boots it again
        raise self._starting(phone, restart)

    def _require_capped_unit(self, phone: PhoneConfig) -> None:
        """Refuse to start a locked phone from a unit that is not confirmed to enforce its session cap, or that watches
        another state directory than this command uses (see services.unit_cap_problem). The session is not started and
        the caller frees its lease, as for any other start that failed."""
        problem = services.unit_cap_problem(phone, self.cfg.state_dir)
        if problem:
            raise PhoneError(
                Code.USAGE,
                f"phone {phone.name} was not started, because its unit {phone.unit_name} is not the one this config and "
                f"environment call for ({problem}); fix that, then lease it again (orion-phone install-units, run with "
                "the same environment as this command, rewrites the unit and reloads systemd)",
                {"phone": phone.name, "unit": phone.unit_name},
            )

    def _booted(self, phone: PhoneConfig, lease: Lease) -> bool:
        """Is the phone's unit running the very boot this session started? A lease never marked ready, one with no boot
        on record, and a unit that is not active (whatever id it still reports) all say no. The run is checked once, before
        the looks that follow: if the operator restarts a locked unit by hand between this check and the first adb read, the
        looks judge the new run against the old run's marks. No agent can do that (a locked unit has Restart=no, which every
        start and every install checks the loaded unit for, and no [Install] section: only the tool's own cold boot, under the
        action lock, or the operator starts it), and the check is not repeated."""
        return lease.ready and bool(lease.invocation) and services.invocation_id(phone.unit_name) == lease.invocation

    def _stop_and_verify(self, phone: PhoneConfig, retry: str = "lease", installing: bool | str = False) -> None:
        """Stop the phone for certain, and see that it is gone. `retry` is the verb the failure advice ends with ("then lease
        it again"). `installing` is the stop of an install (install-units, or setup's look after its own reload, which names
        the command as a string): its advice ends "then run install-units again" (or that command), and never tells it to run
        install-units first.

        `systemctl stop` always runs: whether the phone is up is never decided from one `adb devices` answer (an adb
        server that just restarted lists nothing). It cannot reach an emulator its unit did not start, so the phone is
        also told to exit, best effort, when its console port accepts a connection. The kill connects to that port itself, not
        through the adb server, and under WSL2's mirrored networking a connect to a closed loopback port is dropped, not
        refused: a locked phone is down between every two sessions, so a kill sent regardless would have each cold boot wait
        out adb's whole time limit (measured: `adb emu kill` for a phone that was down ran until a 40 second timeout ended
        it). With nothing listening on the console there is nothing for it to reach, and it is not sent; the console, and
        not the adb port, is the port to ask (services.console_answers), since a kill sent when only the adb port answers
        would hang the same way. Then `adb devices` must answer, and must stop listing the phone, and the phone's console
        and adb ports must stop accepting connections (a look that does not go through the adb server), all within a few
        seconds; if not, it fails closed with a PhoneError. A new session that fails frees its lease (it never started); a
        release that fails keeps the lease, so the phone is never left running and free.

        A unit that is not installed cannot be stopped (systemd answers no, and `not_installed` finds that to be why), but
        an emulator started by hand, outside any unit, can be running, possibly unlocked: so the kill (when its console
        answers) and the looks are made all the same. A phone still there is the same PHONE_DOWN, with the way out added; one
        shown gone is UnitNotInstalled, which tells the way out. The phone is never reported only as "not installed" while it
        may still be running.

        Residual: a foreign phone so new that it has opened no port yet, and that adb has not seen yet either, at the
        moment of the looks, would survive this. All of it holds for the default probe address. ORION_PHONE_PROBE_HOST (the
        environment is inside the trust boundary) can name a loopback address where nothing listens: the kill, which always
        goes to 127.0.0.1, is then never sent, whatever the phone's console does, and the port looks see nothing, so only
        `adb devices` is left (see SECURITY.md).
        """
        if installing:
            then = f"run {installing if isinstance(installing, str) else 'install-units'} again"
        else:
            then = f"{retry} it again"
        missing = False
        try:
            services.systemctl("stop", phone.unit_name)
        except services.SystemctlFailed:  # an answer of no (a stop that did not answer in time has nothing to look into)
            if not services.not_installed(phone.unit_name):  # systemd cannot stop a unit it does not have: that is the cause
                raise
            missing = True
        adb = self.adb(phone)
        if services.console_answers(phone):  # `emu kill` goes to the console itself: with nothing there, it reaches nothing
            try:
                adb.emu_kill()
            except PhoneError:
                pass  # best effort: there is nothing to kill when the stop worked, and the looks below decide
        deadline = time.monotonic() + services.limit("ORION_PHONE_GONE_WAIT", GONE_WAIT)
        while adb.is_listed():  # an adb that cannot answer raises here: no answer is not "gone"
            if time.monotonic() >= deadline:
                raise self._still_running(
                    phone, f"adb still lists {phone.serial}", then=then, missing=missing, installing=installing
                )
            time.sleep(GONE_POLL)
        port = services.answering_port(phone)  # adb can be blind to a phone that is still there; a port that answers is not
        while port is not None:
            if time.monotonic() >= deadline:
                raise self._still_running(
                    phone, f"port {port} still answers", port, then=then, missing=missing, installing=installing
                )
            time.sleep(GONE_POLL)
            port = services.answering_port(phone)
        if missing:  # shown gone, and there was no unit to stop: the way out is to install it
            raise self._not_installed(phone, then)

    @staticmethod
    def _still_running(
        phone: PhoneConfig,
        what: str,
        port: int | None = None,
        then: str = "lease it again",
        missing: bool = False,
        installing: bool = False,
    ) -> PhoneError:
        """PHONE_DOWN for a phone that is still there after its stop. With `missing` there was no unit to stop (it is not
        installed), so nothing was stopped, and the advice to install the unit comes with the one to stop the emulator, unless
        the caller is install-units (`installing`), which is the way to install it and is not told to run itself first."""
        data: dict[str, Any] = {"phone": phone.name, "serial": phone.serial, "unit": phone.unit_name}
        if port is not None:
            data["port"] = port
        if missing:
            install_first = (
                ""
                if installing
                else "run `orion-phone install-units` (or `orion-phone setup`) from a shell with the same environment as "
                "this command, "
            )
            return PhoneError(
                Code.PHONE_DOWN,
                f"phone {phone.name} is still running outside its unit: {what}, and its unit {phone.unit_name} is not "
                f"installed (systemd has no unit by that name), so there was nothing to stop; stop its emulator process "
                f"(adb -s {phone.serial} emu kill, or kill it), {install_first}then {then}",
                data,
            )
        return PhoneError(
            Code.PHONE_DOWN,
            f"phone {phone.name} is still running outside its unit: {what} although {phone.unit_name} was stopped; "
            f"stop its emulator process (adb -s {phone.serial} emu kill, or kill it), then {then}",
            data,
        )

    @staticmethod
    def _not_installed(phone: PhoneConfig, then: str) -> UnitNotInstalled:
        """USAGE for a phone whose unit systemd does not have (before a first install-units, or after its file was removed),
        once the phone is shown gone: the stop failed because there is no unit to stop, not because the phone is down, and
        the way out is to install it."""
        return UnitNotInstalled(
            Code.USAGE,
            f"phone {phone.name} cannot be stopped or started: its unit {phone.unit_name} is not installed (systemd has no "
            "unit by that name); run `orion-phone install-units` (or `orion-phone setup`) from a shell with the same "
            f"environment as this command, then {then}",
            {"phone": phone.name, "unit": phone.unit_name},
        )

    @staticmethod
    def _starting(phone: PhoneConfig, restart: RestartNeeded | None = None) -> PhoneError:
        """The reply to a boot that has only just been started. It must not ask for the unlock: the image cannot be read
        before the phone has booted, so on an unsafe image the owner would type the PIN before the next lease refuses it.
        Only the `locked` and `no-screen-lock` replies (see _require_usable), reached once the image has passed, ask the
        owner for anything: to unlock the phone, and to set a PIN on it. `restart` is the reply to a phone started again
        because its last boot could not be used (see RestartNeeded): it says why, as the end of a sentence about that boot.
        The state is `starting` all the same, and the data says that it is a restart, and its cause."""
        if restart:
            return PhoneError(
                Code.NEEDS_OPERATOR,
                f"phone {phone.name} is starting again: its last boot {restart.because}, so it is not used; lease it again "
                "shortly. "
                "Do not unlock it yet: once its image has been checked, a later lease says when its lock screen shows, and "
                "its owner unlocks it then",
                {"phone": phone.name, "state": "starting", "restart": RESTART_REASON, "cause": restart.cause},
            )
        return PhoneError(
            Code.NEEDS_OPERATOR,
            f"phone {phone.name} is starting; lease it again shortly. Do not unlock it yet: once its image has been checked, "
            "a later lease says what its owner is to do (set a screen lock, if it has none, or unlock it)",
            {"phone": phone.name, "state": "starting"},
        )

    def release(self, name: str, holder: str) -> Result:
        phone = self.cfg.phone(name)
        stopped = phone.tier == "locked"
        current = self.leases.current(name)  # a refusal is answered at once; the check inside the locks still decides
        if not current or current.holder != holder:
            raise not_held(name, holder, current)
        try:
            with self.leases.acting(name):  # wait out an action in flight: the phone is never shut down under one
                with self.leases.releasing(name, holder):  # drops the lease only if this block ends without an error
                    if stopped:
                        self._stop_and_verify(phone, retry="release")  # a phone still showing keeps the lease
                self.audit.record(name, holder, "release", extra={"stopped": stopped})
        except PhoneError as exc:
            exc.data = {"phone": name, **exc.data}  # a failed systemctl call knows only its unit
            raise
        return Result(Code.OK, f"{holder} released phone {name}", {"phone": name, "stopped": stopped})

    def stop_for_install(self, phone: PhoneConfig, again: str = "install-units") -> None:
        """Stop a running locked phone that install-units found without what its config calls for, and see that it is gone,
        as a lease or a release does: under the phone's action lock (it is not shut down under an action in flight), then
        the emulator told to exit (when its console answers), then adb and both ports looked at within the gone wait. The
        unit this stops may be one whose settings leave the emulator alive (an old unit with no cap, a kill mode that
        spares it, a stop that is not forced), so `systemctl stop` exiting 0 is not enough. Raises PhoneError when the
        phone is not shown gone. A unit that is not installed leaves nothing to stop: shown gone is all that was wanted.

        The lock is tried for, not waited on, every ACTION_LOCK_POLL seconds for at most ACTION_LOCK_WAIT, as the lease watch
        does it: an action that never ends does not hold the install for ever, and Ctrl-C is answered while it waits. The
        phone is not stopped without the lock. When the wait runs out, or the lock file cannot be opened (a directory in its
        place, or no directory to hold it: an OSError, not a PhoneError, which left alone would end the install as INTERNAL
        and lose the phones already stopped), the stop never began: StopNotAttempted, which says so, names the lock file, and
        claims nothing of the emulator. Only taking the lock is mapped; whatever goes wrong while stopping is not."""
        deadline = time.monotonic() + services.ACTION_LOCK_WAIT
        while True:
            with ExitStack() as held:
                try:
                    free = held.enter_context(self.leases.try_acting(phone.name, make_dir=True))
                except OSError as exc:
                    path = exc.filename or self.leases.dir
                    raise services.StopNotAttempted(
                        f"its lock file cannot be opened ({path}: {exc.strerror or exc}); make it a file you can open for "
                        "writing, or remove it (it is made again when missing)",
                        {"phone": phone.name, "path": str(path)},
                    ) from None
                if free:
                    try:
                        self._stop_and_verify(phone, installing=again)
                    except UnitNotInstalled:
                        pass  # shown gone, whatever the state of its unit: the install is about to write it
                    return
            if time.monotonic() >= deadline:
                lock = self.leases.action_lock_file(phone.name)
                raise services.StopNotAttempted(
                    f"its action lock is busy (an action has held {lock} for {services.ACTION_LOCK_WAIT:g} s)",
                    {"phone": phone.name, "path": str(lock)},
                )
            time.sleep(services.ACTION_LOCK_POLL)

    def act(self, name: str, holder: str, action: str, args: dict[str, Any]) -> Result:
        """Run one action on a phone the caller holds. Every failure after the lease check leaves an audit record.

        The lease comes first: a caller who does not hold the phone (for a locked phone, with a session whose cold
        boot finished and whose boot is the one running) gets NOT_HOLDER whatever its arguments, and nothing is sent to
        the phone. Only a holder's arguments are checked, inside the audited block, so a holder's malformed ones are
        USAGE, recorded like any other refusal. The phone's action lock is held from the lease check to the audit
        record, so one action's screenshot never shows another's input. A caller who plainly does not hold the phone
        is refused before that lock, as release() does, so it never waits behind the holder's action in flight.
        """
        phone = self.cfg.phone(name)
        current = self.leases.current(name)  # may only refuse: the renew inside the locks still decides
        if not current or current.holder != holder or current.expires_at <= self.clock():
            raise not_held(name, holder, current)
        if phone.tier == "locked" and not current.ready:  # refused at once like a non-holder, and recorded: it holds the lease
            self.audit.record(name, holder, action, code=Code.NOT_HOLDER.name, args=args)
            raise not_ready(name, holder)
        with self.leases.acting(name):  # even one holder's actions never overlap on the phone
            lease = self.leases.renew(name, holder, self.cfg.ttl_seconds)  # first: no lease, NOT_HOLDER, whatever the arguments
            try:
                if phone.tier == "locked" and not self._booted(phone, lease):  # not the boot this session started
                    raise not_ready(name, holder)
                command = self._validate(action, args)  # inside: a holder's rejected arguments are audited too
                data = self._perform(phone, lease, action, command)
            except Exception as exc:  # the phone may already be touched, so every failure leaves a record
                code = exc.code if isinstance(exc, PhoneError) else Code.INTERNAL
                if isinstance(exc, PhoneError):  # a failed adb call knows only its argv, never which phone (as in health)
                    exc.data = {"phone": name, **exc.data}
                extra: dict[str, Any] = {}
                if isinstance(exc, PhoneError):  # a wait for the operator says which, as a lease does, and a boot that cannot
                    extra = {key: exc.data[key] for key in ("state", "cause") if key in exc.data}  # be used says why
                if isinstance(exc, UnsafeImage):  # its phone is shut down: the session is over, so the lease goes with it
                    self.leases.release(name, holder)
                    extra["reported"] = exc.data["reported"]
                # if the record cannot be written, that error surfaces in place of this one
                self.audit.record(name, holder, action, code=code.name, args=args, extra=extra or None)
                raise
            extra = {"ui": data["path"]} if action == "ui" else None  # the saved dump: the record names what was read
            self.audit.record(name, holder, action, args=args, screenshot=Path(data["screenshot"]), extra=extra)
        return Result(Code.OK, f"{action} done on phone {name}", data)

    def _perform(self, phone: PhoneConfig, lease: Lease, action: str, command: str | None) -> dict[str, Any]:
        """Run one validated action, then capture the screen. act() audits every failure."""
        self._require_usable(phone, for_lease=False, lease=lease)
        adb = self.adb(phone)
        data: dict[str, Any] = {"phone": phone.name}
        if command is not None:
            adb.shell(command)
        if action == "ui":
            data.update(self._dump_ui(adb, phone.name))
        data["screenshot"] = str(self._capture(adb, phone.name, action))
        if action == "shot":
            data["path"] = data["screenshot"]
        return data

    def _capture(self, adb: Adb, name: str, action: str) -> Path:
        png = adb.exec_out("screencap -p")
        if not png.startswith(PNG_SIGNATURE):  # exec-out exits 0 and returns error text when screencap fails
            raise PhoneError(Code.UNHEALTHY, f"phone {name} returned no screenshot ({len(png)} bytes)", {"phone": name})
        shot = self.audit.new_path("shots", name, action, ".png")
        write_private(shot, png)
        return shot

    def _dump_ui(self, adb: Adb, name: str) -> dict[str, Any]:
        # uiautomator exits 0 even when it writes nothing, so remove the last dump first: a failed dump must read
        # as unreadable, never as the previous screen. Remove it after reading too: it holds other apps' text.
        # Right after a window changes (an app starting) Android's accessibility bridge can have no window to give it: it
        # then writes nothing and says why on stderr ("null root node", merged in here so it can be told), and a dump a
        # second later succeeds (measured on the pinned image, under heavy host load too). Whether a dump was written is
        # asked of the phone in the same command (a read of a missing file returns cat's complaint, exit 0, as exec-out
        # merges stderr), and the file is read only then. A dump that wrote nothing is tried once more, after
        # UI_DUMP_RETRY_WAIT; one that writes nothing again is UNHEALTHY in the tool's own words.
        said = ""
        for attempt in (1, 2):
            marker = f"{UI_DUMP_WRITTEN}-{secrets.token_hex(8)}"  # new for every call: nothing on the phone can print it ahead
            said = adb.shell(
                f"rm -f {UI_DUMP} && uiautomator dump {UI_DUMP} 2>&1 && test -s {UI_DUMP} && echo {marker} || true"
            )  # `|| true`: a dump that wrote nothing is told by the missing marker, not by an exit status adb takes for failure
            lines = [line.strip() for line in said.splitlines() if line.strip()]
            if lines and lines[-1] == marker:  # the last thing printed, and only by the echo that `test -s` let through
                break
            if attempt == 1:
                time.sleep(UI_DUMP_RETRY_WAIT)
        else:
            words = next((line.strip() for line in reversed(said.splitlines()) if line.strip()), "nothing")
            words = "".join(c if c.isprintable() else "?" for c in words)[:200]
            raise PhoneError(
                Code.UNHEALTHY,
                f"phone {name} gave no UI dump (uiautomator said: {words}); the screen was probably still changing, as when "
                "an app is starting: call ui again in a moment, or use shot",
                {"phone": name},
            )
        xml = self._read_ui_dump(adb, name)
        path = self.audit.new_path("ui", name, "ui", ".xml")
        write_private(path, xml)
        try:
            text = screen.compact_ui(xml)
        except (ValueError, ParseError) as exc:
            raise PhoneError(
                Code.UNHEALTHY, f"could not read the UI dump: {exc}", {"phone": name, "path": str(path)}
            ) from None
        return {"path": str(path), "text": text}

    def _read_ui_dump(self, adb: Adb, name: str) -> bytes:
        """Read the dump uiautomator wrote, and remove it from the phone. The read stops at UI_DUMP_MAX_BYTES."""
        try:  # the read itself stops at the limit: a dump that never ends is not read until adb's time runs out
            return adb.exec_out_limited(f"cat {UI_DUMP} && rm -f {UI_DUMP}", UI_DUMP_MAX_BYTES)
        except OutputOverLimit:
            try:  # adb was killed before the phone's own `rm -f` ran, and the dump holds other apps' text
                adb.shell(f"rm -f {UI_DUMP}")
            except PhoneError:
                pass  # the next ui removes it first anyway
            raise PhoneError(
                Code.UNHEALTHY,
                f"phone {name}'s UI dump is more than {UI_DUMP_MAX_BYTES} bytes, so it was not saved or read further; "
                "the screen may be hostile, use shot instead",
                {"phone": name, "limit": UI_DUMP_MAX_BYTES},
            ) from None

    def _require_usable(self, phone: PhoneConfig, for_lease: bool, lease: Lease | None = None) -> None:
        """Raise unless the phone is up, booted, on a safe image, protected by a screen lock, and unlocked, and unlocked by its
        owner: a locked phone's boot is used only after a look in that same boot saw its lock screen showing over a secure
        screen lock (recorded in the lease, see LeaseStore.mark_seen_locked), and a later look finds it unlocked. That
        sighting is the only evidence that the unlock is the owner's, so a phone found unlocked in a boot never seen locked is
        RestartNeeded, whatever else was seen of the boot: no look at all, a boot still under way, and a screen lock or a lock
        state that could not be read record nothing. (The boot that was seen with no screen lock is judged first, see
        _require_screen_lock.) `for_lease` is the lease call's look at a session whose boot is already on record, where a
        locked phone that is not running is still `starting`, not down. `lease` is the session's own, which a locked phone is
        judged by. Nothing here ever starts a unit: a locked phone is started only by _start_locked_session, after the old
        phone has been stopped for certain and the loaded unit has been read."""
        adb = self.adb(phone)
        base = {"phone": phone.name}
        if phone.tier == "open":
            if not adb.is_running():
                raise PhoneError(Code.PHONE_DOWN, f"phone {phone.name} is not running", base)
            return
        if not adb.is_running():
            if for_lease:  # not listed yet, or its unit stopped since the last look; either way the next lease begins afresh
                raise self._starting(phone)
            raise PhoneError(Code.PHONE_DOWN, f"phone {phone.name} is not running", base)
        if _line_of(adb.shell("getprop sys.boot_completed")) != "1":
            raise PhoneError(
                Code.NEEDS_OPERATOR, f"phone {phone.name} is still booting; retry shortly", {**base, "state": "booting"}
            )
        # Before the lock state: an unsafe image is refused, never reported as waiting for its owner to unlock it.
        self._require_safe_image(phone, adb)
        # Also before the lock state, and after the image: a phone with no screen lock has no keyguard to show, so its lock
        # state would read as unlocked, and the tier's whole protection would be missing with nothing to say so.
        if lease is None:
            raise ValueError("a locked phone is judged by the lease of its session")
        self._require_screen_lock(phone, adb, lease)
        if for_lease:  # only a lease: an action refused here sends the phone nothing
            self._wake_for_owner(adb)  # before the look, so a lock screen it dismissed could never count as seen
        showing = screen.keyguard_showing(adb.shell("dumpsys activity activities"))
        if showing is None:
            raise PhoneError(
                Code.NEEDS_OPERATOR,
                f"could not read the lock state of phone {phone.name}; treating it as locked",
                {**base, "state": "unknown"},
            )
        if showing:
            # The lock screen, showing over a screen lock that was just read to be secure: this boot has been seen locked,
            # which is the evidence that an unlock of it is its owner's. It is written before the reply, so that a look that
            # cannot record it does not ask for an unlock that would not count.
            self.leases.mark_seen_locked(phone.name, lease.holder, lease.invocation)
            raise PhoneError(
                Code.NEEDS_OPERATOR,
                f"phone {phone.name} is locked; its owner must unlock it on its screen",
                {**base, "state": "locked"},
            )
        if not lease.invocation or lease.seen_locked != lease.invocation:
            # Unlocked, with a screen lock, in a boot that was never seen at its lock screen: nothing shows that its owner
            # unlocked it. A screen lock set during a boot leaves the phone unlocked (its owner was signed in), so this boot
            # may never have had a lock screen for anyone to unlock, and whoever holds the lease would be handed it.
            raise RestartNeeded(
                f"phone {phone.name} was never seen at its lock screen in this boot, so it is not used; lease it again, "
                "which restarts the phone (its owner then unlocks it from its lock screen, once a lease says it is locked, "
                "and not before)",
                {**base, "state": RESTART_REASON},
                cause="never-seen-locked",
            )

    @staticmethod
    def _wake_for_owner(adb: Adb) -> None:
        """Wake the display of a locked phone whose lock state a lease is about to read. A cold boot leaves the display
        asleep, so the owner, asked to unlock the phone, would face a dark screen. A wake shows the lock screen and never
        dismisses one over a screen lock, but nothing relies on that: it is sent before the look that records the sighting,
        so a lock screen that a wake did dismiss would never be seen, and the boot would never count as seen locked. A wake
        that fails changes nothing the look decides: the owner can still wake the screen by hand."""
        try:
            adb.shell("input keyevent KEYCODE_WAKEUP")
        except PhoneError:
            pass

    def _require_screen_lock(self, phone: PhoneConfig, adb: Adb, lease: Lease) -> None:
        """Refuse a locked phone that has no secure screen lock: NEEDS_OPERATOR, like the lock screen, so the lease is kept
        and no action reaches the phone.

        A new phone boots with no screen lock at all (no lock screen, nothing to swipe), and such a phone reads as unlocked:
        there is no keyguard to show. So the locked tier is only a tier once the phone has a PIN, a password or a pattern,
        which `dumpsys lock_settings` reports as user 0's CredentialType (see screen.secure_screen_lock). With none the
        state is `no-screen-lock`, and the message asks the owner to set a PIN on the phone's own screen: the tool never
        sets, receives or types one. A report that cannot be read with certainty is `unknown`, and the phone is treated as
        having none. Like the image check, it runs on every lease retry and every action after the boot, so an agent that
        acts without leasing again is covered too. The open tier claims no protection and is never asked.

        What it remembers is that this boot was seen with none. The screen lock can be set during a boot (the owner is
        signed in when they set it, so nothing is left to unlock), and a boot that was seen with none must not become usable
        on that, not even when its lock screen is seen later: it ran without a screen lock, and whoever holds the lease would
        be handed an unlocked phone. So the boot is marked in the lease (LeaseStore.mark_unprotected) when a report says
        there is none, and a later look in that boot that finds a secure screen lock raises RestartNeeded, which a lease
        answers by starting the phone again and an action only reports. A report that cannot be read marks nothing: it is no
        evidence of an unprotected boot, and a mark made on it would restart every boot whose first look happens to fail. The
        mark belongs to the boot (a new boot has another InvocationID, and mark_ready clears it). What lets a boot be used
        is judged after this, by _require_usable: it must also have been seen at its lock screen."""
        secure = screen.secure_screen_lock(adb.shell("dumpsys lock_settings"))
        if secure is None:
            raise PhoneError(
                Code.NEEDS_OPERATOR,
                f"could not read the screen lock of phone {phone.name}; treating it as having none",
                {"phone": phone.name, "state": "unknown"},
            )
        if not secure:
            self.leases.mark_unprotected(phone.name, lease.holder, lease.invocation)  # this boot has been seen with none
            raise PhoneError(
                Code.NEEDS_OPERATOR,
                f"phone {phone.name} has no screen lock, so nothing protects it; its owner must set a PIN on the phone's own "
                "screen (Settings, then Security, then Screen lock) before anything else, then lease it again",
                {"phone": phone.name, "state": "no-screen-lock"},
            )
        if lease.unprotected and lease.unprotected == lease.invocation:  # a screen lock, in a boot that was seen with none
            raise RestartNeeded(
                f"phone {phone.name}: a screen lock was set after this boot was seen with none, so it is not used; lease it "
                "again, which restarts the phone (its owner then unlocks it from its lock screen, once a lease says it is "
                "locked)",
                {"phone": phone.name, "state": RESTART_REASON},
                cause="seen-with-none",
            )

    def _require_safe_image(self, phone: PhoneConfig, adb: Adb) -> None:
        """Refuse a locked phone whose booted image cannot give the locked tier its protections.

        File-based encryption (ro.crypto.type is `file`) keeps app data sealed until the first unlock after boot, and a
        build that is not debuggable (ro.debuggable is `0`) gives adb no root shell. The phone is asked once it has
        booted, before its lock state is read and before any action is sent to it. Anything else shuts the phone down and
        raises UnsafeImage: the phone is never reported as waiting for its owner's unlock, and no action reaches it.
        (The lease call that only starts the boot returns before the phone can be asked.) This reads what the phone
        reports about itself: it catches an image that is not what the locked tier needs, not one built to lie.

        It runs on every lease retry and every action after the boot, not once per boot. A read-only property cannot
        change during a boot, so asking again only repeats the answer, but nothing has to be remembered (no new field
        in the lease to go stale), and an agent that acts without leasing again after the boot is covered too. The cost is
        two `getprop` round trips each time.
        """
        crypto = _line_of(adb.shell("getprop ro.crypto.type"))
        debuggable = _line_of(adb.shell("getprop ro.debuggable"))
        if crypto == LOCKED_CRYPTO_TYPE and debuggable == LOCKED_DEBUGGABLE:
            return
        reported = {"ro.crypto.type": _shown(crypto), "ro.debuggable": _shown(debuggable)}
        facts = (
            f"ro.crypto.type reports {reported['ro.crypto.type']}, it needs {LOCKED_CRYPTO_TYPE!r}; "
            f"ro.debuggable reports {reported['ro.debuggable']}, it needs {LOCKED_DEBUGGABLE!r}"
        )
        data = {"phone": phone.name, "reported": reported}
        try:
            self._stop_and_verify(phone)
        except UnitNotInstalled:
            pass  # shown gone, which is all this needs: the phone is shut down whatever its unit (a lease says it is missing)
        except PhoneError as exc:  # it could not be shown gone: the refusal is still told, under the shutdown's own code
            raise UnsafeImage(
                exc.code,
                f"phone {phone.name}'s system image does not provide the locked tier's protections ({facts}), and it "
                f"could not be shut down: {exc.message}",
                {**exc.data, **data},
            ) from None
        raise UnsafeImage(
            Code.USAGE,
            f"phone {phone.name} was shut down and its session ended: its system image does not provide the locked "
            f"tier's protections ({facts}); a locked phone needs file-based encryption and no root shell, which a Google "
            "Play system image is meant to provide",
            data,
        )

    @staticmethod
    def _validate(action: str, args: dict[str, Any]) -> str | None:
        """Turn validated arguments into one device shell command (None for shot and ui)."""
        if action in ("shot", "ui"):
            return None
        if action == "tap":
            x, y = _coords(args["x"], args["y"])
            return f"input tap {x} {y}"
        if action == "swipe":
            x1, y1, x2, y2 = _coords(args["x1"], args["y1"], args["x2"], args["y2"])
            ms = args.get("ms", 300)
            if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= 10000:
                raise PhoneError(Code.USAGE, "swipe duration must be 1 to 10000 ms")
            return f"input swipe {x1} {y1} {x2} {y2} {ms}"
        if action == "key":
            name = str(args["name"])
            key = name.upper()  # ASCII only (checked below): upper() maps some other letters onto ASCII, U+0131 to I
            key = key if key.startswith("KEYCODE_") else f"KEYCODE_{key}"
            if not name.isascii() or not KEY_RE.fullmatch(key):
                raise PhoneError(Code.USAGE, "key names look like BACK, HOME, ENTER, or KEYCODE_WAKEUP")
            return f"input keyevent {key}"
        if action == "type":
            text = args["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= 1000 or any(not 32 <= ord(c) <= 126 for c in text):
                raise PhoneError(
                    Code.USAGE,
                    "type takes 1 to 1000 printable ASCII characters (Android's input text has no Unicode support)",
                )
            return "input text " + shlex.quote(text.replace(" ", "%s"))
        if action == "open":
            target = str(args["target"])
            if URL_RE.fullmatch(target):
                return "am start -a android.intent.action.VIEW -d " + shlex.quote(target)
            if len(target) <= MAX_PACKAGE and PACKAGE_RE.fullmatch(target):
                return f"monkey -p {target} -c android.intent.category.LAUNCHER 1"
            raise PhoneError(Code.USAGE, "open takes an http(s) URL or an Android package name like com.example.app")
        raise PhoneError(Code.USAGE, f"unknown action {action!r}")
