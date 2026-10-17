"""Exercise approval at the command-line boundary with synthetic artifacts."""
import hashlib
import io
import subprocess
import sys
import tarfile
import zipfile
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


def test_wheel_permissions_do_not_change_release_bytes(tmp_path):
    outputs = []
    payloads = {
        "orion_phone/cli.py": b"print('synthetic')\n",
        "orion_phone/config.example.toml": b"[phones]\n",
        "orion_phone-0.2.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "orion_phone-0.2.0.dist-info/METADATA": b"Version: 0.2.0\n",
        "orion_phone-0.2.0.dist-info/RECORD": b"synthetic-record\n",
    }
    for index, mode in enumerate([0o100644, 0o100664]):
        archive = tmp_path / f"candidate-{index}.whl"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for name, payload in payloads.items():
                info = zipfile.ZipInfo(name, (2026, 10, 16, 17, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = mode << 16
                out.writestr(info, payload)
        result = subprocess.run(
            [sys.executable, str(ROOT / ".github/normalize_wheel.py"), str(archive)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        with zipfile.ZipFile(archive) as wheel:
            assert {name: wheel.read(name) for name in wheel.namelist()} == payloads
            assert wheel.getinfo("orion_phone/cli.py").external_attr >> 16 == 0o100664
            assert wheel.getinfo("orion_phone/config.example.toml").external_attr >> 16 == 0o100644
        outputs.append(archive.read_bytes())
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("name,mode", [("../outside.py", 0o100644), ("link.py", 0o120777)])
def test_wheel_normalization_refuses_unsafe_members_without_rewriting(tmp_path, name, mode):
    archive = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(archive, "w") as out:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        out.writestr(info, b"synthetic")
    before = archive.read_bytes()
    result = subprocess.run(
        [sys.executable, str(ROOT / ".github/normalize_wheel.py"), str(archive)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "safe regular files" in result.stderr
    assert archive.read_bytes() == before
