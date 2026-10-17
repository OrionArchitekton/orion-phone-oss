"""Refuse any release set that differs from its reviewed manifest and version."""
import hashlib
from pathlib import Path
import re
import sys


def verify(directory: Path, digest: str, version: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("expected a lowercase SHA-256 manifest digest")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("expected a three-part release version")
    expected = {
        f"orion_phone-{version}-py3-none-any.whl",
        f"orion_phone-{version}.tar.gz",
    }
    if {p.name for p in directory.iterdir()} != expected | {"SHA256SUMS"}:
        raise ValueError("unexpected release file inventory")
    if any(p.is_symlink() or not p.is_file() for p in directory.iterdir()):
        raise ValueError("release entries must be regular files, never symlinks")
    manifest = (directory / "SHA256SUMS").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != digest:
        raise ValueError("manifest checksum mismatch")
    records = {}
    for line in manifest.decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if match is None:
            raise ValueError("invalid manifest record")
        checksum, name = match.groups()
        if name not in expected or name in records:
            raise ValueError("unexpected or duplicate manifest name")
        records[name] = checksum
    if records.keys() != expected:
        raise ValueError("manifest must name exactly the wheel and source distribution")
    for name, checksum in records.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != checksum:
            raise ValueError(f"artifact checksum mismatch: {name}")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4:
            raise ValueError("usage: verify_release.py DIRECTORY MANIFEST_SHA256 VERSION")
        verify(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
    except (ValueError, OSError) as error:
        print(f"release refused: {error}", file=sys.stderr)
        sys.exit(1)
    print("Reviewed manifest and both release artifacts verified")
