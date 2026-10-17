"""Exercise approval at the command-line boundary with synthetic artifacts."""
import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / ".github" / "verify_release.py"


def candidate(directory):
    directory.mkdir()
    names = ["orion_phone-0.2.0-py3-none-any.whl", "orion_phone-0.2.0.tar.gz"]
    records = []
    for name in names:
        payload = ("synthetic " + name).encode()
        (directory / name).write_bytes(payload)
        records.append(f"{hashlib.sha256(payload).hexdigest()}  {name}\n")
    manifest = "".join(records).encode()
    (directory / "SHA256SUMS").write_bytes(manifest)
    return hashlib.sha256(manifest).hexdigest(), names


def verify(directory, digest):
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(directory), digest, "0.2.0"],
        capture_output=True, text=True,
    )


def test_exact_approved_files_pass(tmp_path):
    digest, _ = candidate(tmp_path / "dist")
    result = verify(tmp_path / "dist", digest)
    assert result.returncode == 0, result.stderr


def test_changed_payload_is_refused(tmp_path):
    digest, names = candidate(tmp_path / "dist")
    (tmp_path / "dist" / names[0]).write_bytes(b"changed after review")
    result = verify(tmp_path / "dist", digest)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr


@pytest.mark.parametrize("change", ["extra", "missing", "symlink", "manifest", "unsafe", "duplicate"])
def test_ambiguous_or_changed_release_is_refused(tmp_path, change):
    directory = tmp_path / "dist"
    digest, names = candidate(directory)
    if change == "extra":
        (directory / "unexpected.whl").write_bytes(b"not reviewed")
    elif change == "missing":
        (directory / names[0]).unlink()
    elif change == "symlink":
        payload = (directory / names[0]).read_bytes()
        outside = tmp_path / "external"
        outside.write_bytes(payload)
        (directory / names[0]).unlink()
        (directory / names[0]).symlink_to(outside)
    elif change == "manifest":
        (directory / "SHA256SUMS").write_text("changed after approval\n")
    else:
        manifest = (directory / "SHA256SUMS").read_text()
        if change == "unsafe":
            manifest = manifest.replace(names[0], "../" + names[0])
        else:
            manifest += manifest.splitlines(keepends=True)[0]
        (directory / "SHA256SUMS").write_text(manifest)
        digest = hashlib.sha256(manifest.encode()).hexdigest()
    result = verify(directory, digest)
    assert result.returncode != 0
    assert "release refused" in result.stderr


def test_source_archive_timestamps_do_not_change_release_bytes(tmp_path):
    outputs = []
    for index, stamp in enumerate([100, 200]):
        archive = tmp_path / f"candidate-{index}.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            info = tarfile.TarInfo("orion_phone-0.2.0/README.md")
            info.size = 7
            info.mtime = stamp
            info.uid = index + 1000
            info.uname = f"builder-{index}"
            out.addfile(info, io.BytesIO(b"example"))
        result = subprocess.run(
            [sys.executable, str(ROOT / '.github/normalize_sdist.py'), str(archive), "1792170000"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(archive.read_bytes())
    assert outputs[0] == outputs[1]
