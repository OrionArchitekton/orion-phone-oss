"""Keep the reviewed wheel's permission profile independent of the builder umask."""
import io
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile


def normalize(path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(path) as source:
        members = source.infolist()
        if len({member.filename for member in members}) != len(members):
            raise ValueError("duplicate wheel member")
        with zipfile.ZipFile(output, "w") as target:
            target.comment = source.comment
            for member in members:
                name = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if (name.is_absolute() or ".." in name.parts or member.is_dir()
                        or "\\" in member.filename or ":" in member.filename
                        or name.as_posix() != member.filename or member.external_attr & 0x10
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError("wheel must contain only safe regular files")
                # Preserve v0.2.0's approved profile. Python sources and the
                # wheel writer's own records used 0664; copied data used 0644.
                writer_record = name.parent.name.endswith(".dist-info") and name.name in {"WHEEL", "RECORD"}
                permissions = 0o664 if name.suffix == ".py" or writer_record else 0o644
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | permissions) << 16
                target.writestr(member, source.read(member))
    path.write_bytes(output.getvalue())


if __name__ == "__main__":
    normalize(Path(sys.argv[1]))
