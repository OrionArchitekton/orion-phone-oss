"""The repository must stay publishable: no phone artifacts, no personal addresses, no long dashes."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".xml", ".jsonl", ".mp4"}
ARTIFACT_DIRS = ("docs/assets/",)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "users.noreply.github.com")
LONG_DASHES = re.compile("[" + "".join(map(chr, (0x2013, 0x2014, 0x2015))) + "]")  # built from code points so this file never contains one


def repo_files() -> list[Path]:
    """Every file git would commit: tracked files plus untracked files that .gitignore does not cover."""
    try:
        # -z because plain output C-quotes unusual names (non-ASCII, quotes, tabs) and a quoted name is not a file
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        if os.environ.get("CI"):
            raise
        pytest.skip("not a git checkout")
    names = [os.fsdecode(raw) for raw in out.split(b"\0") if raw]
    files = sorted({ROOT / name for name in names if (ROOT / name).is_file()})
    assert ROOT / "pyproject.toml" in files, "file listing looks wrong; the hygiene guard would pass vacuously"
    return files


def is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\0" in handle.read(8192)


def allowed_email(address: str) -> bool:
    domain = address.rsplit("@", 1)[1].lower()
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def test_no_phone_artifacts_are_committed():
    bad = [
        str(p.relative_to(ROOT))
        for p in repo_files()
        if p.suffix.lower() in ARTIFACT_SUFFIXES and not str(p.relative_to(ROOT)).startswith(ARTIFACT_DIRS)
    ]
    assert not bad, f"screenshots, UI dumps, and audit logs must be gitignored or absent, never tracked: {bad}"


def test_no_real_email_addresses():
    hits = []
    for p in repo_files():
        if is_binary(p):
            continue
        for match in EMAIL.finditer(p.read_text(errors="ignore")):
            if not allowed_email(match.group()):
                hits.append(f"{p.relative_to(ROOT)}: {match.group()}")
    assert not hits, hits


def test_no_long_dashes():
    hits = [
        str(p.relative_to(ROOT))
        for p in repo_files()
        if not is_binary(p) and LONG_DASHES.search(p.read_text(errors="ignore"))
    ]
    assert not hits, hits
