"""Normalize a locally built source archive without changing its file payloads."""
import gzip
import io
from pathlib import Path, PurePosixPath
import sys
import tarfile


def normalize(path: Path, epoch: int) -> None:
    output = io.BytesIO()
    with tarfile.open(path, "r:gz") as source:
        members = source.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError("duplicate source archive name")
        with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=epoch) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as target:
                for member in sorted(members, key=lambda item: item.name):
                    name = PurePosixPath(member.name)
                    if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                        raise ValueError("source archive must contain only safe files and directories")
                    payload = source.extractfile(member) if member.isfile() else None
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.mtime = epoch
                    member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
                    member.pax_headers = {}
                    target.addfile(member, payload)
    path.write_bytes(output.getvalue())


if __name__ == "__main__":
    normalize(Path(sys.argv[1]), int(sys.argv[2]))
