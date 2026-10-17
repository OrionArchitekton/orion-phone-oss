"""orion-phone command line. Exit codes and --json output are the contract agents rely on."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback

from orion_phone import __version__
from orion_phone.codes import Code, PhoneError, Result

ACTIONS = ("shot", "ui", "tap", "type", "swipe", "key", "open")
AGENT_COMMANDS = ("status", "lease", "release", *ACTIONS)
LISTED_COMMANDS = (*AGENT_COMMANDS, "init", "install-units", "setup")  # what --help names: `_run` is the units' entry point
_NOT_ACTION_ARGS = {"json", "command", "phone", "holder"}
HOLDER_RE = re.compile(r"[A-Za-z0-9._:@-]{1,64}")  # the holder lands in lease files and audit lines


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse calls this for every usage problem
        raise PhoneError(Code.USAGE, message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orion-phone", description="Android emulator phones for AI agents.")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument("--version", action="version", version=f"orion-phone {__version__}")
    # The metavar replaces argparse's own list of every registered command, which would name `_run`.
    sub = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser, metavar="{" + ",".join(LISTED_COMMANDS) + "}"
    )

    status = sub.add_parser("status", help="prove one phone healthy, or list every phone")
    status.add_argument("phone", nargs="?")

    def leased(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("phone")
        p.add_argument(
            "--holder",
            default=os.environ.get("ORION_PHONE_HOLDER"),
            help="agent session name (or set ORION_PHONE_HOLDER)",
        )
        return p

    leased("lease", "take the phone for this agent session")
    leased("release", "give the phone back")
    leased("shot", "save a screenshot")
    leased("ui", "list the screen's UI elements with tap centers")
    tap = leased("tap", "tap a point")
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    typ = leased("type", "type printable ASCII text (put -- before text that starts with -)")
    typ.add_argument("text")
    swipe = leased("swipe", "swipe from one point to another")
    for axis in ("x1", "y1", "x2", "y2"):
        swipe.add_argument(axis, type=int)
    swipe.add_argument("--ms", type=int, default=300)
    key = leased("key", "press a key such as BACK, HOME, ENTER, WAKEUP")
    key.add_argument("name")
    opn = leased("open", "open an http(s) URL or an app by package name")
    opn.add_argument("target")

    sub.add_parser("init", help="write the example config where orion-phone reads its config")
    sub.add_parser("install-units", help="write systemd user units for the configured phones and the adb server")
    sub.add_parser("setup", help="install SDK packages, create phones, install units")
    # No help=: any help, even argparse.SUPPRESS, gets a row in --help (SUPPRESS prints as the text "==SUPPRESS==").
    run_parser = sub.add_parser("_run")
    run_parser.add_argument("phone")
    run_parser.add_argument("--print", action="store_true", dest="print_only")
    return parser


def run(argv: list[str]) -> Result:
    args = build_parser().parse_args(argv)
    if hasattr(args, "holder"):  # only the leased commands define --holder
        if not args.holder:
            raise PhoneError(Code.USAGE, "--holder (or ORION_PHONE_HOLDER) must name the agent session")
        if not HOLDER_RE.fullmatch(args.holder):
            raise PhoneError(Code.USAGE, "holder must be 1 to 64 characters from letters, digits, and . _ : @ -")
    from orion_phone import config

    if args.command == "init":  # before load: there is no config yet to load
        path = config.write_example()
        return Result(Code.OK, f"wrote the example config to {path}; edit it, then run orion-phone setup", {"path": str(path)})
    cfg = config.load()
    if args.command == "install-units":
        from orion_phone import services
        from orion_phone.phone import install_units

        installed = install_units(cfg)
        return Result(
            Code.OK,
            f"wrote {len(installed.units)} units" + services.stopped_note(installed.stopped),
            {"units": [str(u) for u in installed.units], "stopped": installed.stopped},
        )
    if args.command == "_run":
        from orion_phone import services

        return services.run_phone(cfg, args.phone, print_only=args.print_only)
    if args.command == "setup":
        from orion_phone import setup

        return setup.run(cfg)

    from orion_phone.phone import Phones

    phones = Phones(cfg)
    if args.command == "status":
        return phones.status(args.phone)
    if args.command == "lease":
        return phones.lease(args.phone, args.holder)
    if args.command == "release":
        return phones.release(args.phone, args.holder)
    action_args = {k: v for k, v in vars(args).items() if k not in _NOT_ACTION_ARGS}
    return phones.act(args.phone, args.holder, args.command, action_args)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        result = run(argv)
    except PhoneError as exc:
        result = Result(exc.code, exc.message, exc.data)
    except Exception as exc:  # a bug: still answer with one JSON object; the traceback goes to stderr
        traceback.print_exc()
        result = Result(Code.INTERNAL, f"internal error: {type(exc).__name__}: {exc}")
    if as_json:
        print(json.dumps(result.to_json()))
    else:
        _print_text(result)
    return int(result.code)


def _print_text(result: Result) -> None:
    stream = sys.stdout if result.code is Code.OK else sys.stderr
    print(f"{result.code.name}: {result.message}", file=stream)
    for key, value in result.data.items():
        if key != "text":
            print(f"  {key}: {value}", file=stream)
    if "text" in result.data:
        print(result.data["text"], file=stream)


def entry() -> None:
    raise SystemExit(main())
