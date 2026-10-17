"""Config errors are USAGE with a message that names the problem; config and state stay out of git."""

from __future__ import annotations

import errno
import os
import tomllib
from pathlib import Path

import pytest

from orion_phone import config
from orion_phone.codes import Code, PhoneError

PHONE_A = '[phones.a]\ntier = "open"\nconsole_port = 5554\n'


def write_config(env, text: str) -> None:
    Path(env.env["ORION_PHONE_CONFIG"]).write_text(text)


def assert_usage(env, *args: str, needle: str) -> None:
    code, out = env.run(*args)
    assert (code, out["code"]) == (2, "USAGE"), out
    assert needle in out["message"], out


@pytest.mark.parametrize(
    "text, needle",
    [
        ('[phones.a]\ntier = "public"\nconsole_port = 5554\n', "tier"),
        ('[phones.a]\ntier = "open"\nconsole_port = 5555\n', "console_port"),
        (PHONE_A + '[phones.b]\ntier = "open"\nconsole_port = 5554\n', "used by another phone"),
        ('[phones.A]\ntier = "open"\nconsole_port = 5554\n', "phone name"),
        ("[lease]\nttl_minutes = 0\n" + PHONE_A, "ttl_minutes"),
        ('[phones.b]\ntier = "locked"\nconsole_port = 5556\nsession_cap_minutes = 1\n', "session_cap_minutes"),
        ("", "no phones"),
        ("[phones.a\n", "not valid TOML"),
        ('[phones."a\\n"]\ntier = "open"\nconsole_port = 5554\n', "phone name"),
        ('[android]\ngpu = "auto\\n"\n' + PHONE_A, "android.gpu"),
        ('[android]\ndata_partition = "16G\\n"\n' + PHONE_A, "android.data_partition"),
    ],
)
def test_invalid_config_is_usage(phone_env, text, needle):
    write_config(phone_env, text)
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (2, "USAGE")
    assert needle in out["message"]


def test_missing_config_points_at_init(phone_env):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).unlink()
    code, out = phone_env.run("status")
    assert code == 2 and "orion-phone init" in out["message"]  # an install from the package has no example to copy


# Wrong types never crash: a section that is not a table, or a text field that is not text, is USAGE naming the key.
@pytest.mark.parametrize(
    "text, needle",
    [
        pytest.param('android = "x"\n' + PHONE_A, "[android]", id="android-not-a-table"),
        pytest.param('lease = "x"\n' + PHONE_A, "[lease]", id="lease-not-a-table"),
        pytest.param("phones = 5\n", "[phones]", id="phones-not-a-table"),
        pytest.param("[phones]\na = 5\n", "[phones.a]", id="phone-not-a-table"),
        pytest.param("[android]\nsdk_root = 5\n" + PHONE_A, "android.sdk_root", id="sdk_root-not-text"),
        pytest.param("[android]\nadb = 5\n" + PHONE_A, "android.adb", id="adb-not-text"),
        pytest.param("[android]\nsystem_image = 5\n" + PHONE_A, "android.system_image", id="system_image-not-text"),
        pytest.param('[android]\ndevice = ["pixel_7"]\n' + PHONE_A, "android.device", id="device-not-text"),
        pytest.param("[android]\ngpu = 5\n" + PHONE_A, "android.gpu", id="gpu-not-text"),
        pytest.param("[android]\ndata_partition = 16\n" + PHONE_A, "android.data_partition", id="partition-not-text"),
        pytest.param("[phones.a]\ntier = 5\nconsole_port = 5554\n", "phones.a.tier", id="tier-not-text"),
    ],
)
def test_wrongly_typed_config_is_usage(phone_env, text, needle):
    write_config(phone_env, text)
    assert_usage(phone_env, "status", "a", needle=needle)


def test_non_utf8_config_is_usage(phone_env):
    Path(phone_env.env["ORION_PHONE_CONFIG"]).write_bytes(b"\xff" + PHONE_A.encode())
    code, out = phone_env.run("status", "a")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert "UTF-8" in out["message"] and "not valid TOML" in out["message"], out


def test_unreadable_config_is_usage(phone_env):
    cfg = Path(phone_env.env["ORION_PHONE_CONFIG"])
    cfg.chmod(0)
    try:
        if os.access(cfg, os.R_OK):
            pytest.skip("this account can read a mode 000 file (root, or a filesystem without modes)")
        assert_usage(phone_env, "status", "a", needle=f"cannot read config at {cfg}")
    finally:
        cfg.chmod(0o600)


def test_config_in_an_unsearchable_directory_is_usage(phone_env, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    cfg = locked / "config.toml"
    cfg.write_text(Path(phone_env.env["ORION_PHONE_CONFIG"]).read_text())
    phone_env.env["ORION_PHONE_CONFIG"] = str(cfg)
    locked.chmod(0)
    try:
        if os.access(cfg, os.F_OK):
            pytest.skip("this account can search a mode 000 directory (root, or a filesystem without modes)")
        assert_usage(phone_env, "status", "a", needle=f"cannot read config at {cfg}")
    finally:
        locked.chmod(0o700)  # let pytest clean up the temp tree


# A typo must fail loudly: `session_cap_minute = 15` would otherwise leave a locked phone on the 120 minute default.
@pytest.mark.parametrize(
    "text, needle",
    [
        pytest.param(
            '[phones.b]\ntier = "locked"\nconsole_port = 5556\nsession_cap_minute = 15\n',
            "unknown key 'session_cap_minute'",
            id="typo-in-phone",
        ),
        pytest.param(PHONE_A + "[tuning]\nspeed = 1\n", "unknown key 'tuning'", id="unknown-section"),
        pytest.param('[android]\nsdk = "/opt/sdk"\n' + PHONE_A, "unknown key 'sdk'", id="typo-in-android"),
        pytest.param("[lease]\nttl = 5\n" + PHONE_A, "unknown key 'ttl'", id="typo-in-lease"),
    ],
)
def test_unknown_keys_are_usage(phone_env, text, needle):
    write_config(phone_env, text)
    assert_usage(phone_env, "status", "a", needle=needle)


@pytest.mark.parametrize(
    "text, needle",
    [
        pytest.param(
            '[android]\nsystem_image = "system-images/android-36/google_apis_playstore/x86_64"\n' + PHONE_A,
            "android.system_image",
            id="image-slash-form",
        ),
        pytest.param(
            '[android]\nsystem_image = "-system-images;android-36;google_apis_playstore;x86_64"\n' + PHONE_A,
            "android.system_image",
            id="image-starts-with-dash",
        ),
        pytest.param(
            '[android]\nsystem_image = "system-images;android-36;google_apis_playstore"\n' + PHONE_A,
            "android.system_image",
            id="image-too-few-parts",
        ),
        pytest.param(
            '[android]\nsystem_image = "system-images;android-36;google_apis_playstore;x86_64;extra"\n' + PHONE_A,
            "android.system_image",
            id="image-too-many-parts",
        ),
        pytest.param('[android]\nsystem_image = ""\n' + PHONE_A, "android.system_image", id="image-empty"),
        pytest.param('[android]\ndevice = "pixel 7"\n' + PHONE_A, "android.device", id="device-with-space"),
        pytest.param('[android]\ndevice = "-x"\n' + PHONE_A, "android.device", id="device-starts-with-dash"),
        pytest.param('[android]\ndevice = "_x"\n' + PHONE_A, "android.device", id="device-starts-with-underscore"),
        pytest.param('[android]\ndevice = ".x"\n' + PHONE_A, "android.device", id="device-starts-with-dot"),
        pytest.param('[android]\ndevice = "' + "d" * 65 + '"\n' + PHONE_A, "android.device", id="device-too-long"),
    ],
)
def test_image_and_device_must_have_the_expected_shape(phone_env, text, needle):
    write_config(phone_env, text)
    assert_usage(phone_env, "status", "a", needle=needle)


# Agents may run from any working directory, so a relative path would give each of them its own lease store.
@pytest.mark.parametrize(
    "text, needle",
    [
        pytest.param(
            '[android]\nsdk_root = "Android/Sdk"\n' + PHONE_A,
            "android.sdk_root must be an absolute path",
            id="relative-sdk_root",
        ),
        pytest.param(
            '[android]\nsdk_root = ""\n' + PHONE_A, "android.sdk_root must be a non-empty", id="empty-sdk_root"
        ),
        pytest.param(
            '[android]\nadb = "platform-tools/adb"\n' + PHONE_A,
            "android.adb must be an absolute path",
            id="relative-adb",
        ),
        pytest.param(
            '[android]\nsdk_root = "~no-such-user-zzz/sdk"\n' + PHONE_A,
            "android.sdk_root",
            id="unknown-user-sdk_root",
        ),
    ],
)
def test_tool_paths_must_be_absolute(phone_env, text, needle):
    write_config(phone_env, text)
    assert_usage(phone_env, "status", "a", needle=needle)


@pytest.mark.parametrize(
    "text, needle",
    [
        pytest.param('[android]\nsdk_root = "/opt/sdk\\nx"\n' + PHONE_A, "android.sdk_root", id="newline-in-sdk_root"),
        pytest.param('[android]\nadb = "/opt/sdk/adb\\u0000x"\n' + PHONE_A, "android.adb", id="nul-in-adb"),
    ],
)
def test_tool_paths_must_be_one_line(phone_env, text, needle):
    write_config(phone_env, text)
    assert_usage(phone_env, "status", "a", needle=needle)


@pytest.mark.parametrize("var", ["ORION_PHONE_STATE", "ORION_PHONE_CONFIG"])
@pytest.mark.parametrize(
    "value", ["", "relative/path", "~no-such-user-zzz/x"], ids=["empty", "relative", "unknown-user"]
)
def test_config_and_state_paths_must_be_absolute(phone_env, var, value):
    phone_env.env[var] = value
    assert_usage(phone_env, "status", needle=var)


# These two paths are written into systemd unit lines, which cannot carry a newline or a double quote.
@pytest.mark.parametrize("var", ["ORION_PHONE_STATE", "ORION_PHONE_CONFIG"])
@pytest.mark.parametrize(
    "suffix",
    [
        "\nExecStart=true", '"quoted"', "\ttab", "\x7fdel",
        chr(0x2028) + "RuntimeMaxUSec=infinity", chr(0x2029) + "RuntimeMaxUSec=infinity", chr(0x85) + "RuntimeMaxUSec=infinity",
    ],
    ids=["newline", "double-quote", "tab", "del", "line-separator", "paragraph-separator", "next-line"],
)
def test_paths_a_unit_file_cannot_carry_are_usage(phone_env, var, suffix):
    path = phone_env.root / f"x{suffix}"
    if var == "ORION_PHONE_CONFIG":
        path.write_text(Path(phone_env.env[var]).read_text())  # the file exists, so only the path rule can refuse it
    phone_env.env[var] = str(path)
    assert_usage(phone_env, "status", needle="control character")


# A unit file breaks lines on a newline only, but what a unit carries comes back in `systemctl show` output, where a line or
# paragraph separator or a next line character can look like the end of a line to a reader that splits on them too.


@pytest.mark.parametrize(
    "text",
    ['a"b', "a" + chr(10) + "b", "a" + chr(13) + "b", "a" + chr(9) + "b", "a" + chr(127) + "b", "a" + chr(0x2028) + "b",
     "a" + chr(0x2029) + "b", "a" + chr(0x85) + "b"],
    ids=["quote", "newline", "carriage-return", "tab", "del", "line-separator", "paragraph-separator", "next-line"],
)
def test_unit_unsafe_refuses_what_a_unit_cannot_carry_or_show_output_could_split(text):
    assert config.unit_unsafe(text)


@pytest.mark.parametrize(
    "text", ["/home/user/.local/state/orion-phone", "a b", "50%", "caf" + chr(0xE9), "a" + chr(0xA0) + "b", "a'b", "a$b", "a\\b"],
    ids=["a path", "a space", "a percent", "an accent", "a no-break space", "a quote", "a dollar", "a backslash"],
)
def test_unit_unsafe_lets_everything_else_through(text):
    assert not config.unit_unsafe(text)  # the unit escapes % and backslash itself, and a quote of this kind ends nothing


def dot_git(root: Path, form: str) -> None:
    """Create root/.git in one of the shapes the tests care about."""
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    if form in ("dir-with-head", "dir-with-dangling-head", "empty-dir"):
        git.mkdir()
    if form == "dir-with-head":
        (git / "HEAD").write_text("ref: refs/heads/main\n")
    elif form == "dir-with-dangling-head":
        (git / "HEAD").symlink_to("refs/heads/missing")  # an old-style symlinked HEAD whose target is absent
    elif form == "gitfile":  # a linked worktree or submodule: .git is a pointer file
        git.write_text("gitdir: /elsewhere/.git/worktrees/some-repo\n")
    elif form == "empty-file":
        git.write_text("")
    elif form == "foreign-file":
        git.write_text("this is not a pointer\n")
    elif form == "almost-gitfile":
        git.write_text("gitdir:/elsewhere\n")  # no space after the colon


# What git itself accepts as a repository. Other tools on a shared box create an empty .git briefly; git ignores it.
@pytest.mark.parametrize("where", ["at", "above"])
@pytest.mark.parametrize(
    "form, expected",
    [
        ("dir-with-head", True),
        ("dir-with-dangling-head", True),
        ("gitfile", True),
        ("empty-dir", False),
        ("empty-file", False),
        ("foreign-file", False),
        ("almost-gitfile", False),
    ],
)
def test_inside_git_tree_follows_what_git_accepts(tmp_path, form, expected, where):
    dot_git(tmp_path / "root", form)
    probe = tmp_path / "root" if where == "at" else tmp_path / "root" / "deeper" / "state"
    assert config.inside_git_tree(probe) is expected


def test_a_dot_git_that_cannot_be_inspected_counts_as_a_repository(tmp_path, monkeypatch):
    dot_git(tmp_path, "empty-dir")  # no HEAD, so only the failure below can make this a repository
    real_lstat = os.lstat

    def denied(path, *args, **kwargs):
        if Path(path).name == "HEAD":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(config.os, "lstat", denied)
    assert config.inside_git_tree(tmp_path / "state") is True


def test_a_dot_git_that_cannot_be_inspected_is_reported_as_unchecked(phone_env, tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    dot_git(sandbox, "empty-dir")  # no HEAD: this is not a repository, so the failed check is the only problem
    moved = sandbox / "config.toml"
    moved.write_text(Path(phone_env.env["ORION_PHONE_CONFIG"]).read_text())
    monkeypatch.setenv("ORION_PHONE_CONFIG", str(moved))
    monkeypatch.setenv("ORION_PHONE_STATE", str(sandbox / "state"))
    real_lstat = os.lstat

    def denied(path, *args, **kwargs):
        if Path(path).name == "HEAD":
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(config.os, "lstat", denied)
    with pytest.raises(PhoneError) as exc:
        config.load()
    assert exc.value.code is Code.USAGE
    assert "could not be checked" in exc.value.message and "Permission denied" in exc.value.message
    assert "is inside a git working tree" not in exc.value.message  # refusing is right; claiming a repository is not


@pytest.mark.parametrize(
    "form", [pytest.param("dir-with-head", id="repo"), pytest.param("gitfile", id="worktree")]
)
@pytest.mark.parametrize("which", ["config", "state"])
def test_config_or_state_inside_a_git_tree_is_refused(phone_env, tmp_path, which, form):
    repo = tmp_path / "some-repo"
    dot_git(repo, form)
    if which == "config":
        moved = repo / "config.toml"
        moved.write_text(Path(phone_env.env["ORION_PHONE_CONFIG"]).read_text())
        phone_env.env["ORION_PHONE_CONFIG"] = str(moved)
    else:
        phone_env.env["ORION_PHONE_STATE"] = str(repo / "state")
    code, out = phone_env.run("status")
    assert (code, out["code"]) == (2, "USAGE"), out
    assert f"{which} path" in out["message"] and "is inside a git working tree" in out["message"], out


def test_state_symlinked_into_a_repo_is_refused(phone_env, tmp_path):
    repo = tmp_path / "some-repo"
    dot_git(repo, "dir-with-head")
    (repo / "state").mkdir()
    link = tmp_path / "state-link"
    link.symlink_to(repo / "state")
    phone_env.env["ORION_PHONE_STATE"] = str(link)
    assert_usage(phone_env, "status", needle="git working tree")


def test_state_behind_a_symlink_loop_is_usage(phone_env, tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    try:
        loop.resolve()
    except (OSError, RuntimeError):
        pass
    else:
        pytest.skip("this Python resolves symlink loops without raising")
    phone_env.env["ORION_PHONE_STATE"] = str(loop)
    assert_usage(phone_env, "status", needle="cannot resolve state path")


def test_empty_dot_git_dirs_do_not_block_loading(phone_env, tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    dot_git(sandbox, "empty-dir")
    moved = sandbox / "config.toml"
    moved.write_text(Path(phone_env.env["ORION_PHONE_CONFIG"]).read_text())
    monkeypatch.setenv("ORION_PHONE_CONFIG", str(moved))
    monkeypatch.setenv("ORION_PHONE_STATE", str(sandbox / "state"))
    assert config.load().state_dir == sandbox / "state"


def parse_text(text: str) -> config.Config:
    return config.parse(tomllib.loads(text), Path("/etc/orion-phone/config.toml"), Path("/var/lib/orion-phone"))


def phone(port: int | float = 5554, name: str = "a", extra: str = "") -> str:
    return f'[phones.{name}]\ntier = "open"\nconsole_port = {port}\n{extra}'


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(phone(port=5554), id="port-lowest"),
        pytest.param(phone(port=5682), id="port-highest"),
        pytest.param(phone(name="a" * 16), id="name-16-chars"),
        pytest.param("[android]\nram_mb = 1024\n" + phone(), id="ram-lowest"),
        pytest.param("[android]\nram_mb = 65536\n" + phone(), id="ram-highest"),
        pytest.param("[lease]\nttl_minutes = 0.01\n" + phone(), id="ttl-lowest"),
        pytest.param("[lease]\nttl_minutes = 1440\n" + phone(), id="ttl-highest"),
        pytest.param(phone(extra="session_cap_minutes = 5\n"), id="cap-lowest"),
        pytest.param(phone(extra="session_cap_minutes = 1440\n"), id="cap-highest"),
        pytest.param('[android]\ndevice = "' + "d" * 64 + '"\n' + phone(), id="device-64-chars"),
        pytest.param('[android]\ndevice = "7"\n' + phone(), id="device-1-char-digit"),
        pytest.param('[android]\ndevice = "Pixel_6-Pro.v2"\n' + phone(), id="device-punctuation"),
    ],
)
def test_values_on_a_boundary_are_accepted(text):
    assert parse_text(text).phones


@pytest.mark.parametrize(
    "text, needle",
    [
        pytest.param(phone(port=5552), "console_port", id="port-below-range"),
        pytest.param(phone(port=5684), "console_port", id="port-above-range"),
        pytest.param(phone(port=5554.0), "console_port", id="port-float"),
        pytest.param("[android]\nram_mb = 1023\n" + phone(), "ram_mb", id="ram-below-range"),
        pytest.param("[android]\nram_mb = 65537\n" + phone(), "ram_mb", id="ram-above-range"),
        pytest.param("[lease]\nttl_minutes = 0.0099\n" + phone(), "ttl_minutes", id="ttl-below-range"),
        pytest.param("[lease]\nttl_minutes = 1441\n" + phone(), "ttl_minutes", id="ttl-above-range"),
        pytest.param("[lease]\nttl_minutes = true\n" + phone(), "ttl_minutes", id="ttl-bool"),
        pytest.param(phone(extra="session_cap_minutes = 4\n"), "session_cap_minutes", id="cap-below-range"),
        pytest.param(phone(extra="session_cap_minutes = 1441\n"), "session_cap_minutes", id="cap-above-range"),
        pytest.param(phone(name="a" * 17), "phone name", id="name-17-chars"),
    ],
)
def test_values_past_a_boundary_are_usage(text, needle):
    with pytest.raises(PhoneError) as exc:
        parse_text(text)
    assert exc.value.code is Code.USAGE and needle in exc.value.message


def test_example_config_is_valid(tmp_path):
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    cfg = config.parse(tomllib.loads(example.read_text()), example, tmp_path / "state")
    assert {name: p.tier for name, p in cfg.phones.items()} == {"a": "open", "b": "locked"}
    assert cfg.system_image == "system-images;android-36;google_apis_playstore;x86_64"
    assert cfg.ttl_seconds == 30 * 60


# What a quoted systemd unit value cannot carry: the paths and DISPLAY that are written into units are checked by this.
@pytest.mark.parametrize(
    "text, unsafe",
    [
        (":0", False),
        ("a b%c", False),
        ("", False),
        ('a"b', True),
        ("a" + chr(10) + "b", True),
        ("a" + chr(13), True),
        ("a" + chr(9), True),
        ("a" + chr(127), True),
    ],
    ids=["display", "space and percent", "empty", "double quote", "newline", "carriage return", "tab", "delete"],
)
def test_unit_unsafe_flags_what_a_quoted_unit_value_cannot_carry(text, unsafe):
    assert config.unit_unsafe(text) is unsafe


# The locked tier relies on file-based encryption and on a debug shell that cannot become root, which Google Play system
# images provide. So a config with a locked phone must name one: the image's tag, its third field, starts with
# google_apis_playstore. A config whose phones are all open claims no such protection and may use any image.

LOCKED_B = '[phones.b]\ntier = "locked"\nconsole_port = 5556\n'


def with_image(image: str, *phones: str, env=None) -> str:
    """A config on this image with these phones. Given the test environment (env), its fake SDK and adb are named too,
    so that nothing but the image can make the config unusable."""
    tools = f'sdk_root = "{env.root / "sdk"}"\nadb = "{env.root / "bin" / "adb"}"\n' if env else ""
    return f'[android]\n{tools}system_image = "{image}"\n' + "".join(phones)


NOT_PLAY_IMAGES = {
    "google apis without the Play Store": "system-images;android-36;google_apis;x86_64",
    "the default tag": "system-images;android-36;default;x86_64",
    "a tag that only contains it": "system-images;android-36;x_google_apis_playstore;x86_64",
    "the name in the API level field": "system-images;google_apis_playstore;android-36;x86_64",
    "the name in the ABI field": "system-images;android-36;google_apis;google_apis_playstore",
}


@pytest.mark.parametrize("phones", [(PHONE_A, LOCKED_B), (LOCKED_B,)], ids=["open and locked", "locked only"])
@pytest.mark.parametrize("image", NOT_PLAY_IMAGES.values(), ids=NOT_PLAY_IMAGES)
def test_a_locked_phone_needs_a_google_play_image(phone_env, image, phones):
    write_config(phone_env, with_image(image, *phones, env=phone_env))
    code, out = phone_env.run("status")
    assert (code, out["code"]) == (2, "USAGE"), out
    message = out["message"]
    assert "android.system_image" in message and "google_apis_playstore" in message, message
    assert "locked" in message and "file-based encryption" in message and "root shell" in message, message


def test_the_message_names_the_tag_it_found(phone_env):
    write_config(phone_env, with_image("system-images;android-36;google_apis;x86_64", PHONE_A, LOCKED_B, env=phone_env))
    assert_usage(phone_env, "status", needle="'google_apis'")


@pytest.mark.parametrize(
    "image",
    [
        "system-images;android-36;google_apis_playstore;x86_64",
        "system-images;android-36;google_apis_playstore_ps16k;x86_64",
        "system-images;android-34;google_apis_playstore;arm64-v8a",
    ],
    ids=["the default", "a 16 KB page variant", "another API level and ABI"],
)
def test_a_google_play_image_is_fine_for_a_locked_phone(image):
    assert parse_text(with_image(image, PHONE_A, LOCKED_B)).phones["b"].tier == "locked"


@pytest.mark.parametrize("image", NOT_PLAY_IMAGES.values(), ids=NOT_PLAY_IMAGES)
def test_a_config_with_only_open_phones_may_use_any_image(image):
    assert parse_text(with_image(image, PHONE_A)).system_image == image
