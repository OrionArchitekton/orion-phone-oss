"""The only module that runs adb. Every call names one phone by serial, and the adb server it goes to by its address."""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path

from orion_phone.codes import Code, PhoneError
from orion_phone.lines import split_lines
from orion_phone.services import ADB_SERVER_ADDRESS, ADB_SERVER_UNIT

CHUNK = 65536  # bytes read from adb at a time
STDERR_KEPT = 8192  # of an adb that will not stop writing to stderr, only the last of this much is kept


class OutputOverLimit(Exception):
    """adb's output went past the limit given to exec_out_limited. adb was stopped: the rest was never read."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"adb output is more than {limit} bytes")
        self.limit = limit


class Adb:
    def __init__(self, adb: Path, serial: str, timeout: float = 30.0) -> None:
        self.adb = adb
        self.serial = serial
        self.timeout = timeout

    def _command(self, args: list[str]) -> list[str]:
        """The command line of one adb call: `-L ADB_SERVER_ADDRESS` first, then the call's own words. The global option names
        the server the client talks to, and a client that is given it reads none of ADB_SERVER_SOCKET,
        ANDROID_ADB_SERVER_ADDRESS and ANDROID_ADB_SERVER_PORT (observed with platform-tools adb 1.0.41). Left out, the client
        follows whichever of them its environment holds, to another server or to an address where none is running: under WSL2's
        mirrored networking a connect to such an address is dropped, so the call waits out its time limit although the server
        orion-phone runs is healthy, and on ordinary Linux the refused connect has the client start a server of its own, which
        nothing manages. Every call is built here, `emu kill` too, which connects to the emulator's console and not to the
        server: the option costs it nothing, and no call is left to the environment. The address is the one the server's unit
        gives the server (services.ADB_SERVER_ADDRESS, written once). It is not part of the error a failed call carries: that
        shows the call's own words."""
        return [str(self.adb), "-L", ADB_SERVER_ADDRESS, *args]

    def _timed_out(self, args: list[str]) -> PhoneError:
        """The error for a call that outran its time limit. A server that is not running is the usual reason where adb cannot
        start one of its own (WSL2's mirrored networking drops a connect to a port nobody listens on, so the refusal that
        tells adb to start one never comes): the message names the unit that runs the server and how to start it."""
        return PhoneError(
            Code.PHONE_DOWN,
            f"adb timed out after {self.timeout:.0f}s; if the adb server is not running, start it with "
            f"`systemctl --user start {ADB_SERVER_UNIT}`",
            {"args": args},
        )

    def _run(self, args: list[str]) -> bytes:
        try:
            if not self.adb.exists():
                raise PhoneError(Code.USAGE, f"adb not found at {self.adb}; set android.sdk_root or android.adb")
            # stdin: `adb shell` forwards its stdin to the phone, so it must never inherit a caller's input
            proc = subprocess.run(
                self._command(args), capture_output=True, timeout=self.timeout, stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired:
            raise self._timed_out(args) from None
        except OSError as exc:  # not executable, a directory, a broken interpreter line, an unsearchable parent
            raise PhoneError(Code.USAGE, f"cannot run adb at {self.adb}: {exc.strerror or exc}; check android.adb") from None
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            raise PhoneError(Code.PHONE_DOWN, f"adb failed: {err}", {"args": args})
        return proc.stdout

    def _states(self) -> list[str]:
        """The state of every row `adb devices` prints for this serial. A row is `serial<TAB>state` and ends at a newline,
        and the text is cut on those and on nothing else: a serial belongs to a device and is not ours to trust, and one
        with a line or paragraph separator in it would, to `str.splitlines` or `str.split`, look like a row of its own that
        names this phone. A carriage return before the newline (a CR LF line end) is part of the line end, but one only:
        a second belongs to the state, which is then not one this knows (the phone is listed, and not running). So does
        one at the very end of the listing, with no newline after it (see split_lines)."""
        out = self._run(["devices"]).decode("utf-8", "replace")
        states = []
        for line in split_lines(out):
            fields = line.split("\t")
            if len(fields) >= 2 and fields[0] == self.serial:
                states.append(fields[1])
        return states

    def is_running(self) -> bool:
        states = self._states()
        if len(states) > 1:  # which row is the phone? one of them may be forged: no answer is not "running"
            raise PhoneError(
                Code.PHONE_DOWN,
                f"adb devices lists {self.serial} more than once, so it cannot be told which row is the phone",
                {"serial": self.serial},
            )
        return states == ["device"]

    def is_listed(self) -> bool:
        """True when `adb devices` lists this serial in any state (offline and unauthorized count): it is still there. A
        serial listed twice is listed whichever row is real."""
        return bool(self._states())

    def emu_kill(self) -> None:
        """Ask the emulator to exit through its console. Reaches a phone that systemd did not start. The command connects
        to the console port itself, not through the adb server, and a connect to a port nobody listens on is dropped, not
        refused, on a host with WSL2's mirrored networking: sent for a phone that is down, it waits out the whole time limit.
        So callers send it only to a console that answers (services.console_answers). Like every call it is given the
        server's address (see _command), which it does not use."""
        self._run(["-s", self.serial, "emu", "kill"])

    def shell(self, command: str) -> str:
        return self._run(["-s", self.serial, "shell", command]).decode("utf-8", "replace")

    def exec_out(self, command: str) -> bytes:
        return self._run(["-s", self.serial, "exec-out", command])

    def exec_out_limited(self, command: str, limit: int) -> bytes:
        """`exec_out` for output that a hostile phone could make endless: at most `limit` bytes are returned, and at
        most `limit + 1` are ever held. The moment the output passes the limit, adb is killed and reaped and
        OutputOverLimit is raised (the rest was never read, so its size is not known). Otherwise this keeps `_run`'s
        contract: the same time limit (PHONE_DOWN, adb killed), PHONE_DOWN with adb's own words on a nonzero exit, and
        USAGE when adb is missing or cannot run. adb's stderr is read as it comes, so a chatty adb never stalls the read.
        """
        args = ["-s", self.serial, "exec-out", command]
        try:
            if not self.adb.exists():
                raise PhoneError(Code.USAGE, f"adb not found at {self.adb}; set android.sdk_root or android.adb")
            # stdin: `adb exec-out` forwards its stdin to the phone, so it must never inherit a caller's input
            proc = subprocess.Popen(
                self._command(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:  # not executable, a directory, a broken interpreter line, an unsearchable parent
            raise PhoneError(Code.USAGE, f"cannot run adb at {self.adb}: {exc.strerror or exc}; check android.adb") from None
        out, err = bytearray(), bytearray()
        over = False
        try:
            deadline = time.monotonic() + self.timeout
            with selectors.DefaultSelector() as pipes:
                pipes.register(proc.stdout, selectors.EVENT_READ, out)
                pipes.register(proc.stderr, selectors.EVENT_READ, err)
                while pipes.get_map() and not over:
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        raise subprocess.TimeoutExpired(args, self.timeout)
                    for key, _ in pipes.select(wait):
                        data = os.read(key.fd, CHUNK)
                        if not data:  # that pipe is done
                            pipes.unregister(key.fileobj)
                        elif key.data is err:
                            err += data
                            del err[:-STDERR_KEPT]
                        else:
                            out += data[: limit + 1 - len(out)]  # never past limit + 1: one byte over is all it takes
                            if len(out) > limit:
                                over = True
                                break
            if not over:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired:
            raise self._timed_out(args) from None
        finally:
            if proc.poll() is None:  # over the limit, out of time, or interrupted: adb must not outlive this call
                proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        if over:
            raise OutputOverLimit(limit)
        if proc.returncode != 0:
            message = err.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            raise PhoneError(Code.PHONE_DOWN, f"adb failed: {message}", {"args": args})
        return bytes(out)
