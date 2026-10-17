"""How the text a tool prints is cut into lines: at a newline and nowhere else, and a carriage return is part of a line end only
immediately before a newline.

A carriage return at the very end of the text, with no newline after it, is not before a newline: the last fragment is not a line
that a newline ended, so it keeps whatever it ends in. Taking that carriage return off made `RuntimeMaxUSec=2h` of
`RuntimeMaxUSec=2h` followed by a lone carriage return, and `device` of `device` followed by one, so an output that stopped short
of its newline was trusted as a whole line."""

from __future__ import annotations

import pytest

from orion_phone.lines import split_lines

CR, LF = "\r", "\n"

CASES = {
    "no text is one empty line": ("", [""]),
    "text with no newline is one line": ("a", ["a"]),
    "a newline ends a line and starts an empty one": ("a\n", ["a", ""]),
    "two lines": ("a\nb\n", ["a", "b", ""]),
    "a CR LF line end loses its carriage return": ("a\r\nb\r\n", ["a", "b", ""]),
    "a CR LF only ends the line it is after": ("a\r\nb\n", ["a", "b", ""]),
    "only one carriage return is part of a line end": ("a\r\r\nb\n", ["a\r", "b", ""]),
    "a lone carriage return in a line stays": ("a\rb\n", ["a\rb", ""]),
    "a carriage return after the newline stays": ("a\n\rb\n", ["a", "\rb", ""]),
    "a carriage return and a newline and a carriage return": ("a\r\n\r", ["a", "\r"]),
    "a final carriage return with no newline after it is not a line end": ("a\nb\r", ["a", "b\r"]),
    "a lone carriage return is one line": ("\r", ["\r"]),
    "text that is only a carriage return and a newline": ("\r\n", ["", ""]),
    "every carriage return at the very end stays": ("a\nb\r\r", ["a", "b\r\r"]),
    "a final line with a CR LF end is a whole line": ("a\nb\r\n", ["a", "b", ""]),
}


@pytest.mark.parametrize("text, lines", CASES.values(), ids=CASES)
def test_text_is_cut_at_a_newline_and_a_carriage_return_goes_only_with_the_newline_after_it(text, lines):
    assert split_lines(text) == lines


SEPARATORS = {
    "a vertical tab": chr(0x0B), "a form feed": chr(0x0C), "a file separator": chr(0x1C), "a next line character": chr(0x85),
    "a line separator": chr(0x2028), "a paragraph separator": chr(0x2029),
}


@pytest.mark.parametrize("separator", SEPARATORS.values(), ids=SEPARATORS)
def test_a_separator_that_str_splitlines_cuts_at_is_part_of_its_line(separator):
    assert split_lines(f"a{separator}b\nc") == [f"a{separator}b", "c"]
    assert split_lines(f"a{separator}b{CR}{LF}") == [f"a{separator}b", ""]
