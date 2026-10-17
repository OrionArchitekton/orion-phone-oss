"""Result codes shared by the CLI, the MCP server, and the tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Code(IntEnum):
    OK = 0
    INTERNAL = 1  # a bug in orion-phone; never a success
    USAGE = 2
    BUSY = 3
    NOT_HOLDER = 4
    NEEDS_OPERATOR = 5
    PHONE_DOWN = 6
    UNHEALTHY = 7


class PhoneError(Exception):
    """A refusal or failure that maps to exactly one result code."""

    def __init__(self, code: Code, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass
class Result:
    code: Code
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.code is Code.OK,
            "code": self.code.name,
            "exit": int(self.code),
            "message": self.message,
            "data": self.data,
        }
