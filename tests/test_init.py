"""`orion-phone init`: the example config, written where the CLI reads its config, for an install with no checkout."""

import errno
import os
import stat
from importlib import resources
from pathlib import Path

import pytest

from orion_phone import config
from orion_phone.codes import Code, PhoneError

ROOT = Path(__file__).resolve().parent.parent


def test_init_writes_the_example_config_where_the_cli_reads_it(phone_env, tmp_path):
    target = tmp_path / "fresh" / "orion-phone" / "config.toml"
    phone_env.env["ORION_PHONE_CONFIG"] = str(target)
    code, out = phone_env.run("init")
    assert (code, out["code"]) == (0, "OK"), out
    assert target.read_text() == (ROOT / "config.example.toml").read_text()
    assert out["data"]["path"] == str(target) and "setup" in out["message"]
    # The config decides which phones are locked, so it is the operator's alone, like the state directory.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_init_never_overwrites_a_config(phone_env):
    target = Path(phone_env.env["ORION_PHONE_CONFIG"])  # the fixture wrote one
    before = target.read_bytes()
    code, out = phone_env.run("init")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert target.read_bytes() == before and str(target) in out["message"]


def test_init_never_writes_through_a_link_at_the_config_path(phone_env, tmp_path):
    elsewhere = tmp_path / "elsewhere.toml"
    target = tmp_path / "linked" / "config.toml"
    target.parent.mkdir()
    target.symlink_to(elsewhere)  # dangling: a write that followed it would create elsewhere.toml
    phone_env.env["ORION_PHONE_CONFIG"] = str(target)
    assert phone_env.run("init")[1]["code"] == "USAGE"
    assert not elsewhere.exists()




def test_init_makes_every_directory_it_creates_owner_only_whatever_the_umask(phone_env, tmp_path):
    target = tmp_path / "made" / "by" / "init" / "config.toml"
    phone_env.env["ORION_PHONE_CONFIG"] = str(target)
    old = os.umask(0o002)  # a parent its group could write to would let the group swap the config's directory
    try:
        code, out = phone_env.run("init")
    finally:
        os.umask(old)
    assert (code, out["code"]) == (0, "OK"), out
    for made in (tmp_path / "made", tmp_path / "made" / "by", target.parent):
        assert stat.S_IMODE(made.stat().st_mode) == 0o700, made



def test_init_sets_its_modes_exactly_whatever_the_umask(phone_env, tmp_path):
    target = tmp_path / "made" / "config.toml"
    phone_env.env["ORION_PHONE_CONFIG"] = str(target)
    old = os.umask(0o777)  # one that clears even the owner's bits would leave a config nobody can read
    try:
        code, out = phone_env.run("init")
    finally:
        os.umask(old)
    assert (code, out["code"]) == (0, "OK"), out
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_init_refuses_a_file_where_its_directory_goes(phone_env, tmp_path):
    (tmp_path / "taken").write_text("not a directory\n")
    target = tmp_path / "taken" / "orion-phone" / "config.toml"
    phone_env.env["ORION_PHONE_CONFIG"] = str(target)
    code, out = phone_env.run("init")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert str(target) in out["message"]
    assert (tmp_path / "taken").read_text() == "not a directory\n"


def test_init_leaves_an_existing_directory_as_its_owner_set_it(phone_env, tmp_path):
    there = tmp_path / "there"
    there.mkdir(mode=0o750)
    os.chmod(there, 0o750)  # the operator's own choice: init sets the modes only of the directories it makes
    phone_env.env["ORION_PHONE_CONFIG"] = str(there / "config.toml")
    assert phone_env.run("init")[1]["code"] == "OK"
    assert stat.S_IMODE(there.stat().st_mode) == 0o750
    assert stat.S_IMODE((there / "config.toml").stat().st_mode) == 0o600

def race_mkdir(monkeypatch, component: Path, plant) -> None:
    """Between init finding component missing and making it, plant puts something there, as another user could."""
    mkdir = Path.mkdir

    def racing(self, *args, **kwargs):
        if self == component and not os.path.lexists(component):
            plant(component)
        return mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing)


def test_init_never_writes_through_a_link_swapped_in_while_it_makes_a_directory(monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = tmp_path / "fresh" / "orion-phone" / "config.toml"
    monkeypatch.setenv("ORION_PHONE_CONFIG", str(target))
    race_mkdir(monkeypatch, target.parent, lambda at: at.symlink_to(elsewhere))
    with pytest.raises(PhoneError) as caught:
        config.write_example()
    assert caught.value.code == Code.USAGE and str(target) in caught.value.message
    assert list(elsewhere.iterdir()) == []  # nothing written where the link points


def test_init_refuses_a_directory_others_may_write_made_while_it_makes_one(monkeypatch, tmp_path):
    target = tmp_path / "fresh" / "orion-phone" / "config.toml"
    monkeypatch.setenv("ORION_PHONE_CONFIG", str(target))

    def shared(at: Path) -> None:
        os.mkdir(at)
        os.chmod(at, 0o777)  # whoever may write to it may swap the config inside it

    race_mkdir(monkeypatch, target.parent, shared)
    with pytest.raises(PhoneError) as caught:
        config.write_example()
    assert caught.value.code == Code.USAGE and str(target) in caught.value.message
    assert list(target.parent.iterdir()) == []


def test_init_accepts_an_owner_only_directory_made_while_it_makes_one(monkeypatch, tmp_path):
    target = tmp_path / "fresh" / "orion-phone" / "config.toml"
    monkeypatch.setenv("ORION_PHONE_CONFIG", str(target))
    race_mkdir(monkeypatch, target.parent, lambda at: os.mkdir(at, 0o700))  # a second init, say
    assert config.write_example() == target
    assert target.read_text() == (ROOT / "config.example.toml").read_text()


def test_an_init_that_cannot_finish_writing_leaves_nothing_and_can_be_run_again(monkeypatch, tmp_path):
    target = tmp_path / "fresh" / "config.toml"
    monkeypatch.setenv("ORION_PHONE_CONFIG", str(target))
    fsync = os.fsync

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(PhoneError) as caught:
        config.write_example()
    assert caught.value.code == Code.USAGE and str(target) in caught.value.message
    # A half-written config would make every command fail, and init refuses a path that holds anything.
    assert list(target.parent.iterdir()) == []
    monkeypatch.setattr(os, "fsync", fsync)
    assert config.write_example() == target
    assert target.read_text() == (ROOT / "config.example.toml").read_text()


def test_an_init_whose_chmod_fails_leaves_nothing_open_or_on_disk(monkeypatch, tmp_path):
    target = tmp_path / "fresh" / "config.toml"
    monkeypatch.setenv("ORION_PHONE_CONFIG", str(target))

    def refused(fd, mode):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(os, "fchmod", refused)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(PhoneError) as caught:
        config.write_example()
    assert caught.value.code == Code.USAGE
    assert len(os.listdir("/proc/self/fd")) == before  # the temporary file's descriptor was closed
    assert list(target.parent.iterdir()) == []

def make_repo(root: Path) -> Path:
    """root becomes a git working tree: a .git directory with a HEAD, as git itself would find it."""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def test_init_refuses_a_config_path_inside_a_git_tree_before_creating_anything(phone_env, tmp_path):
    target = make_repo(tmp_path / "some-repo") / "orion-phone" / "config.toml"
    phone_env.env["ORION_PHONE_CONFIG"] = str(target)
    code, out = phone_env.run("init")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "is inside a git working tree" in out["message"], out
    # Refused before the directory or the file exists: a config in a checkout is one commit away from published.
    assert not target.parent.exists()


def test_init_refuses_a_config_path_whose_directory_links_into_a_git_tree(phone_env, tmp_path):
    repo = make_repo(tmp_path / "some-repo")
    link = tmp_path / "config-dir"
    link.symlink_to(repo)
    phone_env.env["ORION_PHONE_CONFIG"] = str(link / "config.toml")
    code, out = phone_env.run("init")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "is inside a git working tree" in out["message"], out
    assert not (repo / "config.toml").exists()

def test_the_packaged_example_is_the_repository_example():
    packaged = resources.files("orion_phone").joinpath("config.example.toml").read_text()
    assert packaged == (ROOT / "config.example.toml").read_text()
