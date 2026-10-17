"""How the text a tool prints is cut into lines."""

from __future__ import annotations


def split_lines(text: str) -> list[str]:
    """The lines of `text`, cut at a newline and at nothing else.

    `str.splitlines` also cuts at a lone carriage return, a form feed, U+0085, U+2028 and U+2029. None of them is a line end
    to systemd or adb, which print a value as it is, so a value can hold one, and cutting there would let it forge a line of
    its own (a cap of two hours for a unit that has none, a row that names a phone that is not running).

    A carriage return is part of a line end only immediately before a newline: one is taken off each line that a newline ended
    (a CR LF line end), and nothing else is trimmed, so a second one stays in the line. The last fragment, what follows the last
    newline, was not ended by one, and a carriage return at its end is not before a newline: it stays. The fragment is empty
    when the text ends in a newline, as the output of a tool does; when it is not, the output stopped short, and what it ends in
    is part of what it printed. Text with no newline is that one fragment, and no text at all is one empty line."""
    *ended, last = text.split("\n")
    return [line.removesuffix("\r") for line in ended] + [last]
