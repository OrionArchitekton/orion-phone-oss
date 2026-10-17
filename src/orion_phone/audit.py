"""Append-only audit records and artifact paths, kept in the operator's state directory."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from orion_phone.lease import iso


class Audit:
    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = state_dir
        self.clock = clock

    def _dir(self, *parts: str) -> Path:
        path = self.state_dir
        for part in parts:  # every level owner-only: mkdir(parents=True) gives missing parents the default mode
            path = path / part
            path.mkdir(mode=0o700, exist_ok=True)
        return path

    def new_path(self, kind: str, phone: str, action: str, suffix: str) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        return self._dir(kind, phone) / f"{stamp}-{uuid.uuid4().hex[:8]}-{action}{suffix}"

    def record(
        self,
        phone: str,
        holder: str,
        action: str,
        *,
        code: str = "OK",
        args: dict[str, Any] | None = None,
        screenshot: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        args = dict(args or {})
        if code != "OK" and "text" in args:  # an action that was refused or failed leaves no typed text in the file
            args["text_length"] = len(str(args.pop("text")))
        now = self.clock()
        entry = {
            "ts": iso(now),
            "phone": phone,
            "holder": holder,
            "action": action,
            "code": code,
            "args": args,
            "screenshot": str(screenshot) if screenshot else None,
            **(extra or {}),
        }
        path = self._dir("audit", phone) / (time.strftime("%Y-%m-%d", time.gmtime(now)) + ".jsonl")
        line = (json.dumps(entry) + "\n").encode()
        with open(os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600), "ab") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size and os.pread(fh.fileno(), 1, size - 1) != b"\n":  # an earlier append failed half way:
                line = b"\n" + line  # its torn half stays on its own line and is never glued to this record
            fh.write(line)  # buffered: the whole line or an error, never a short write
            fh.flush()
            os.fsync(fh.fileno())  # as durable as the lease renewal that came before it
