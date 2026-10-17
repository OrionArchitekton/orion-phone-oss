"""Load and validate the operator's config. Config and state never live inside a git tree."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
import tomllib
import unicodedata
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from orion_phone.codes import Code, PhoneError

# Always fullmatch these: `$` alone also accepts a trailing newline.
NAME_RE = re.compile(r"^[a-z0-9]{1,16}$")
GPU_RE = re.compile(r"^[a-z_]{1,32}$")
PARTITION_RE = re.compile(r"^[0-9]{1,4}[MG]$")
IMAGE_RE = re.compile(r"system-images(;[A-Za-z0-9._-]+){3}")  # semicolon form; setup converts it for sdkmanager
DEVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # no leading dash: avdmanager would read it as an option
TIERS = ("open", "locked")
DEFAULT_IMAGE = "system-images;android-36;google_apis_playstore;x86_64"
PLAY_TAG = "google_apis_playstore"  # the tag (third field) of a Google Play system image
INSIDE_GIT_TREE = "is inside a git working tree; keep it outside any repository"
# Every key the config may carry. A typo must fail loudly, never fall back to a default.
TOP_KEYS = frozenset({"android", "lease", "phones"})
ANDROID_KEYS = frozenset({"sdk_root", "adb", "system_image", "device", "gpu", "ram_mb", "data_partition"})
LEASE_KEYS = frozenset({"ttl_minutes"})
PHONE_KEYS = frozenset({"tier", "console_port", "session_cap_minutes"})


@dataclass(frozen=True)
class PhoneConfig:
    name: str
    tier: str
    console_port: int
    session_cap_minutes: int

    @property
    def serial(self) -> str:
        return f"emulator-{self.console_port}"

    @property
    def avd_name(self) -> str:
        return f"orion-phone-{self.name}"

    @property
    def unit_name(self) -> str:
        return f"orion-phone-{self.name}.service"


@dataclass(frozen=True)
class Config:
    path: Path
    state_dir: Path
    sdk_root: Path
    adb: Path
    system_image: str
    device: str
    gpu: str
    ram_mb: int
    data_partition: str
    ttl_seconds: float
    phones: dict[str, PhoneConfig]

    def phone(self, name: str) -> PhoneConfig:
        try:
            return self.phones[name]
        except KeyError:
            known = ", ".join(sorted(self.phones))
            raise PhoneError(Code.USAGE, f"unknown phone {name!r} (configured: {known})") from None


def config_path() -> Path:
    raw = os.environ.get("ORION_PHONE_CONFIG", "~/.config/orion-phone/config.toml")
    return _unit_safe(_absolute(raw, "ORION_PHONE_CONFIG"), "ORION_PHONE_CONFIG")


def write_example() -> Path:
    """Write the example config where the CLI reads its config (config_path), for an install that has no checkout to copy
    it from, and return that path. Never over anything already there: an edited config is its operator's, and which
    phones are locked lives in it. The file is readable by its owner only, and every directory made for it is its
    owner's only. A path inside a git working tree is refused before anything is created there, as every later command
    would refuse it.

    The example is written whole under a name of its own, flushed to disk, and then linked to the path, so the config
    appears complete or not at all: a write cut short (a full disk, an interruption) leaves nothing there to block a
    second init or to break every command. Like O_EXCL, the link never replaces anything at the path, a link there
    included, rather than write where it points."""
    path = config_path()
    refuse_git_tree("config", path)
    text = resources.files("orion_phone").joinpath("config.example.toml").read_text(encoding="utf-8")
    try:
        _make_private_dirs(path.parent)
        temp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:  # from here the file object closes the descriptor
                os.fchmod(fh.fileno(), 0o600)  # exactly: the umask takes bits from open's mode, the owner's too
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                raise PhoneError(
                    Code.USAGE,
                    f"a config already exists at {path}; edit it, or move it aside to start again from the example",
                ) from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
    except OSError as exc:
        raise PhoneError(
            Code.USAGE, f"cannot write the example config to {path} ({exc.strerror or exc}); no config was written"
        ) from None
    return path


def _make_private_dirs(directory: Path) -> None:
    """Make directory and each missing parent, every one its owner's only (0700, whatever the umask).
    mkdir(parents=True) gives the parents the umask's permissions instead, and a parent that others may write to lets
    them swap what lies below it. The directories are made under a umask of 077, so mkdir's own mode is exact and no
    chmod by path follows, which a link swapped in between could redirect. A umask is per process: init runs alone in
    its CLI process."""
    missing = []
    while not directory.is_dir() and directory.parent != directory:
        missing.append(directory)
        directory = directory.parent
    umask = os.umask(0o077)
    try:
        for made in reversed(missing):
            try:
                made.mkdir(mode=0o700)
            except FileExistsError:
                # Made meanwhile, by a second init or by whoever else may write to its parent. Only a directory as
                # this function would have made it is used: a link would take the config, and a check against git
                # trees made before it, somewhere else, and a directory another may write to lets them swap the
                # config inside it.
                if not _owner_only_dir(made):
                    raise OSError(errno.EEXIST, f"{made} appeared meanwhile and is not a directory for its owner only")
    finally:
        os.umask(umask)


def _owner_only_dir(path: Path) -> bool:
    """True when path is itself, not through a link, a directory of this user's that nobody else may use."""
    st = os.lstat(path)
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def state_path() -> Path:
    raw = os.environ.get("ORION_PHONE_STATE", "~/.local/state/orion-phone")
    return _unit_safe(_absolute(raw, "ORION_PHONE_STATE"), "ORION_PHONE_STATE")


def git_tree_problem(path: Path) -> str | None:
    """Why path may not hold config or state, worded to follow the path in an error message; None when it may.

    A repository is a `gitdir:` file, or a .git directory with HEAD, at or above path. An empty .git, which git itself
    ignores, does not count. A .git that cannot be inspected also refuses the path (fail closed), but is reported as
    unchecked: nothing is known about what it holds.
    """
    for parent in [path, *path.parents]:
        dot = parent / ".git"
        try:
            if dot.is_file():
                with dot.open("rb") as fh:
                    if fh.read(8) == b"gitdir: ":
                        return INSIDE_GIT_TREE
            elif dot.is_dir():
                os.lstat(dot / "HEAD")  # lstat: an old-style HEAD symlink counts even when dangling
                return INSIDE_GIT_TREE
        except FileNotFoundError:
            continue
        except OSError as exc:
            where = f" at {exc.filename}" if exc.filename else ""
            return (
                f"could not be checked for a git working tree ({exc.strerror or exc}{where}); "
                "fix that or keep it outside any repository"
            )
    return None



def refuse_git_tree(label: str, candidate: Path) -> None:
    """USAGE when candidate, its links resolved, lies inside a git working tree: config and state never go in one."""
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:  # a symlink loop raises RuntimeError before Python 3.13
        raise _usage(f"cannot resolve {label} path {candidate}: {exc}") from None
    problem = git_tree_problem(resolved)
    if problem:
        raise _usage(f"{label} path {candidate} {problem}")

def inside_git_tree(path: Path) -> bool:
    """True when git would find a repository at or above path. Fails closed: an uninspectable .git counts."""
    return git_tree_problem(path) is not None


def _usage(message: str) -> PhoneError:
    return PhoneError(Code.USAGE, message)


def _absolute(raw: str, label: str) -> Path:
    # A relative path would give agents in different working directories separate lease stores.
    try:
        path = Path(raw).expanduser()
    except RuntimeError:  # ~user names nobody, or there is no home directory
        raise _usage(f"{label} cannot expand ~ in {raw!r}: no such user or home directory") from None
    if not path.is_absolute():
        raise _usage(f"{label} must be an absolute path or start with ~ (got {raw!r})")
    return path


UNIT_UNSAFE = "a control character, a line or paragraph separator, or a double quote"  # what messages say it holds


def unit_unsafe(text: str) -> bool:
    """True when text cannot sit inside a quoted systemd unit value: a double quote ends the value and a control
    character ends the line, and what follows would be read as another assignment or directive. A line or paragraph
    separator (U+2028, U+2029) is refused too, and a next line character (U+0085) is a control character: a unit file
    breaks lines on a newline only, but what a unit carries comes back in `systemctl show` output, where systemd prints
    them as they are and a reader that splits on them would see a line of its own."""
    return any(c == '"' or unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in text)


def _unit_safe(path: Path, label: str) -> Path:
    # These paths are written into systemd unit lines, which cannot carry a newline or a double quote.
    if unit_unsafe(str(path)):
        raise _usage(f"{label} {str(path)!r} contains {UNIT_UNSAFE}")
    return path


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _usage(f"{where} must be a TOML table")
    return value


def _text(table: dict[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
        raise _usage(f"{where}.{key} must be a non-empty single-line string")
    return value


def _only(table: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            raise _usage(f"unknown key {key!r} in {where} (allowed: {', '.join(sorted(allowed))})")


def load() -> Config:
    path = config_path()
    try:
        found = path.is_file()
    except OSError as exc:  # e.g. a parent directory this account cannot search
        raise _usage(f"cannot read config at {path}: {exc.strerror or exc}") from None
    if not found:
        raise _usage(f"no config at {path}; run `orion-phone init` to write the example there, then edit it")
    state = state_path()
    for label, candidate in (("config", path), ("state", state)):
        refuse_git_tree(label, candidate)
    try:
        raw = tomllib.loads(path.read_bytes().decode("utf-8"))
    except OSError as exc:
        raise _usage(f"cannot read config at {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError as exc:
        raise _usage(f"config {path} is not valid TOML: it is not UTF-8 ({exc.reason} at byte {exc.start})") from None
    except tomllib.TOMLDecodeError as exc:
        raise _usage(f"config {path} is not valid TOML: {exc}") from None
    return parse(raw, path, state)


def parse(raw: dict[str, Any], path: Path, state: Path) -> Config:
    _only(raw, TOP_KEYS, "the top level")
    android = _table(raw.get("android", {}), "[android]")
    _only(android, ANDROID_KEYS, "[android]")
    lease = _table(raw.get("lease", {}), "[lease]")
    _only(lease, LEASE_KEYS, "[lease]")
    sdk_root = _absolute(_text(android, "sdk_root", "~/Android/Sdk", "android"), "android.sdk_root")
    adb = _absolute(_text(android, "adb", str(sdk_root / "platform-tools" / "adb"), "android"), "android.adb")
    image = _text(android, "system_image", DEFAULT_IMAGE, "android")
    if not IMAGE_RE.fullmatch(image):
        raise _usage("android.system_image must look like system-images;android-36;google_apis_playstore;x86_64")
    device = _text(android, "device", "pixel_7", "android")
    if not DEVICE_RE.fullmatch(device):
        raise _usage(
            "android.device must be 1 to 64 letters, digits, dots, dashes, or underscores, "
            "starting with a letter or digit"
        )
    ttl_minutes = lease.get("ttl_minutes", 30)
    if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, (int, float)) or not 0.01 <= ttl_minutes <= 1440:
        raise _usage("lease.ttl_minutes must be a number from 0.01 to 1440")
    gpu = android.get("gpu", "auto")
    if not isinstance(gpu, str) or not GPU_RE.fullmatch(gpu):
        raise _usage("android.gpu must be an emulator GPU mode such as auto, host, or swiftshader_indirect")
    ram_mb = android.get("ram_mb", 4096)
    if isinstance(ram_mb, bool) or not isinstance(ram_mb, int) or not 1024 <= ram_mb <= 65536:
        raise _usage("android.ram_mb must be a whole number from 1024 to 65536")
    data_partition = android.get("data_partition", "16G")
    if not isinstance(data_partition, str) or not PARTITION_RE.fullmatch(data_partition):
        raise _usage("android.data_partition must look like 16G or 8192M")

    phones: dict[str, PhoneConfig] = {}
    ports: set[int] = set()
    for name, table in _table(raw.get("phones", {}), "[phones]").items():
        if not NAME_RE.fullmatch(name):
            raise _usage(f"phone name {name!r} must be 1 to 16 lowercase letters or digits")
        table = _table(table, f"[phones.{name}]")
        _only(table, PHONE_KEYS, f"[phones.{name}]")
        tier = table.get("tier")
        if tier not in TIERS:
            raise _usage(f"phones.{name}.tier must be 'open' or 'locked'")
        port = table.get("console_port")
        if isinstance(port, bool) or not isinstance(port, int) or port % 2 or not 5554 <= port <= 5682:
            raise _usage(f"phones.{name}.console_port must be an even number from 5554 to 5682")
        if port in ports:
            raise _usage(f"phones.{name}.console_port {port} is used by another phone")
        ports.add(port)
        cap = table.get("session_cap_minutes", 120)
        if isinstance(cap, bool) or not isinstance(cap, int) or not 5 <= cap <= 1440:
            raise _usage(f"phones.{name}.session_cap_minutes must be a whole number from 5 to 1440")
        phones[name] = PhoneConfig(name, tier, port, cap)
    if not phones:
        raise _usage("config defines no phones")
    tag = image.split(";")[2]  # IMAGE_RE gave it exactly four fields: system-images;API level;tag;ABI
    if any(phone.tier == "locked" for phone in phones.values()) and not tag.startswith(PLAY_TAG):
        raise _usage(
            f"android.system_image has the tag {tag!r}, but a locked phone needs a Google Play system image (a tag that "
            f"starts with {PLAY_TAG}): the locked tier relies on file-based encryption and on an image with no root "
            f"shell, which Google Play images are meant to provide and others may not; use {DEFAULT_IMAGE} or another "
            "Google Play image"
        )
    return Config(
        path=path,
        state_dir=state,
        sdk_root=sdk_root,
        adb=adb,
        system_image=image,
        device=device,
        gpu=gpu,
        ram_mb=ram_mb,
        data_partition=data_partition,
        ttl_seconds=float(ttl_minutes) * 60,
        phones=phones,
    )
