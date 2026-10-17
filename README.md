# orion-phone

<!-- mcp-name: io.github.OrionArchitekton/orion-phone -->

**Give your agent its own phone.**

orion-phone runs Android phones (emulators) that AI agents drive through an MCP server or a CLI:
Claude Code, Codex, or any MCP client. An agent leases a phone, reads its screen, taps, types and
opens apps, and every action leaves an audit record with a screenshot. A phone in the locked tier
does nothing until you unlock it yourself.

## Why

Agents that share one desktop browser collide when two of them want it at once, and they cannot
use apps that exist only on phones. Handing an agent your own phone is worse: everything on it is
then within reach. orion-phone gives agents phones of their own, signed in to the accounts you
choose, with three rules built in:

- **One agent per phone.** An agent leases a phone before it acts. A second agent gets `BUSY`
  with the holder's name and expiry instead of stepping on the first.
- **A locked tier that waits for unlock.** A phone in the `locked` tier never starts on its own and
  cold-boots for every session. It permits holder actions only after an unlock, and it uses a
  boot only after it has seen the lock screen showing over a screen lock and then found the phone
  unlocked. It shuts down when the session is released, when its lease lapses, and at a hard
  session cap. orion-phone does not handle the credential or attest who supplied it. Use it on
  a trusted host: local adb and host access remain outside this cooperative tool boundary.
  Independent verification of credential-encrypted storage remains open as Q2 in the spec.
- **A record of everything.** Each holder action writes an audit line, and each successful one a
  screenshot, to a private state directory outside any repository.

## Requirements

- Linux with systemd user services (version 250 or later) and KVM. Windows works through WSL2
  with nested virtualization.
- Python 3.11 or later
- A Java runtime, version 17 or later, for Google's Android command-line tools
- Google's Android command-line tools, version 23 or later ("Command line tools only" at
  https://developer.android.com/studio), unzipped so that
  `~/Android/Sdk/cmdline-tools/latest/bin/sdkmanager` exists (`~/Android/Sdk` is the default
  `sdk_root` in the config)
- A Google Play system image for any locked phone (`system_image` in the config; the default is
  one, and `setup` downloads it). orion-phone refuses a config that has a locked phone on any other
  tag, and refuses to use a locked phone whose booted image does not report file-based encryption
  (`ro.crypto.type` is `file`) and a build that is not debuggable (`ro.debuggable` is `0`)

## Install and set up

```bash
uv tool install orion-phone           # or: pipx install orion-phone; from a checkout: uv tool install .
orion-phone init                      # writes ~/.config/orion-phone/config.toml: phone a open, phone b locked
# Before setup, read and accept the Android SDK License yourself: https://developer.android.com/studio/terms
# (current SDK tools show the terms once and never ask again; orion-phone cannot check acceptance)
orion-phone setup                     # SDK packages, phones, systemd user units; starts open phones
orion-phone status a                  # exits 0 once the first boot finishes
```

Edit the config between `init` and `setup` to add, remove or rename phones. Run `setup` from a
desktop shell so the phone windows appear, and sign in to accounts by hand on each phone; to see a
phone over Remote Desktop or WSL2, mirror it with scrcpy (see "Viewing a phone" in
[docs/runbooks/phone-services.md](docs/runbooks/phone-services.md)). A locked phone has no window
until it is leased: follow "First boot of a locked phone" in the same runbook, which sets its
screen-lock PIN before anything else.

Phone data (each phone's disk images and snapshots) lives in the AVD home, found the way the
Android tools find it: `ANDROID_AVD_HOME`, else the `avd` directory inside `ANDROID_USER_HOME`,
else `~/.android/avd`. Choose it before `setup`, as an absolute path outside any git repository:
orion-phone refuses a home inside a repository. Besides one unit per phone, `setup` writes
`orion-phone-adb-server.service`, which runs the debug bridge's server on loopback port 5037; the
runbook explains why it runs its own and how to upgrade from a version without it.

## Connect your agent

The MCP server is the `orion-phone-mcp` command, over stdio. Each server process is one lease
holder, and its tools mirror the CLI.

```bash
claude mcp add --scope user orion-phone -- orion-phone-mcp   # Claude Code
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.orion-phone]
command = "orion-phone-mcp"
```

```json
{"mcpServers": {"orion-phone": {"command": "orion-phone-mcp"}}}
```

Do not launch the server as `python -m orion_phone.mcp_server` from inside a project directory,
because a package named `orion_phone` there would shadow this one; the `orion-phone-mcp` command
is unaffected.

## Use it from a shell

```bash
H=--holder=my-agent-session            # one name per agent session; never shared
orion-phone lease a $H
orion-phone ui a $H                   # elements with tap centers
orion-phone tap a 540 260 $H
orion-phone type a $H -- "hello world"
orion-phone key a BACK $H
orion-phone open a https://example.com $H
orion-phone shot a $H
orion-phone release a $H
```

Add `--json` before the command for one machine-readable object per call. A holder name
identifies one agent session: never put `ORION_PHONE_HOLDER` in a harness's global
environment, or every session becomes the same holder.

| Exit | Code | Meaning |
|---|---|---|
| 0 | OK | Done. |
| 1 | INTERNAL | An unexpected error inside orion-phone (traceback on stderr). |
| 2 | USAGE | Bad arguments or config. |
| 3 | BUSY | Another agent holds the phone. |
| 4 | NOT_HOLDER | You do not hold the lease (never held, released, or expired); on a locked phone, also when its session ended: lease again. |
| 5 | NEEDS_OPERATOR | A locked phone is starting, still booting, has no screen lock, has a boot that cannot be used (found unlocked without ever being seen at its lock screen, or seen with no screen lock and given one since), is showing its lock screen, or has a lock state or screen lock that cannot be read. The result's `state` says which: `locked` asks for the unlock and `no-screen-lock` for a PIN to be set, both on the phone's own screen (`starting` and `booting` mean lease again shortly, and do not unlock yet, `restart-needed` means lease again, which restarts the phone, and `unknown`, a lock state or screen lock that cannot be read, also means lease again). |
| 6 | PHONE_DOWN | The phone is not running. |
| 7 | UNHEALTHY | Running, but failed the health proof (not booted, or a blank screen). |

## Installing from a checkout

For a local development or release-check environment, bootstrap the packaging tools
separately from runtime dependencies:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes --force-reinstall -r bootstrap-requirements.txt
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

This changes only the selected virtual environment. Release preparation and exact-artifact
approval are described in `docs/runbooks/release-artifacts.md`.

## What a task costs

Six runs on 2026-10-07, three per task, each a fresh headless Claude Code session (`claude -p`)
on Claude Opus 5.5, driving one open-tier phone through the MCP tools. Every run is listed.

| Task | Succeeded | Cost per run | Phone tool calls per run |
|---|---|---|---|
| Read the Android version and the storage used, in Settings | 3 of 3 | $0.38, $0.36, $0.20 | 19, 22, 10 |
| Create a labeled 6:45 AM alarm in Clock, see it listed, delete it, see it gone | 3 of 3 | $0.46, $0.33, $0.33 | 37, 22, 21 |

- Cost is the whole session as Claude Code reports it, its own system prompt and tool definitions
  included. The runs went back to back, so its prompt cache was warm; with a cold cache, a run
  costs about $0.18 more.
- Settings' About page shows a running uptime counter, so `ui` failed there in two runs (it waits
  for the screen to settle), about 25 seconds per failed call, and the agent read it with `shot`.
- Two of the three Settings runs read the storage figure from the summary on the main Settings
  list instead of opening the Storage page.
- The costliest alarm run spent 15 of its 37 calls on typing the time, which did not take, before
  it set the time on the clock dial.

## Security

Leases coordinate agents; they are not a sandbox. Everything a phone shows is untrusted input to
an agent, and an agent that holds an open phone can do there whatever its accounts allow. Read
[SECURITY.md](SECURITY.md) before signing a phone in to accounts that matter.

## Roadmap (help wanted)

orion-phone runs Android emulators on Linux and WSL2 today. Planned, and open to contributors:

- **Real Android devices** over adb (USB, Wi-Fi, or a Tailscale network). The open tier already
  works through plain adb; it needs a way to name a device by its serial.
- **iPhone**, which needs a Mac and Apple's developer tools.
- **macOS and native Windows hosts**, in place of systemd.
- **An SMS bridge**: real text messages delivered to a phone's emulated SIM.

## Known limits

- `type` accepts printable ASCII only, and a literal `%s` becomes a space (Android's
  `input text` behavior).
- `ui` lists at most 300 elements and stops adding them at 32,000 characters; a final
  `# truncated ...` line says when it cut the list short.
- A UI dump over 4 MiB is refused (UNHEALTHY), and neither saved nor read past the limit.
- `ui` can fail on a screen that changes every second, such as one showing a running clock or
  an uptime counter: the dump waits for the screen to settle. `shot` works there.
- Under WSL2, the emulator's clipboard sharing can crash WSLg's compositor, and every phone window
  with it ([microsoft/wslg#1407](https://github.com/microsoft/wslg/issues/1407)). Turn it off with
  no phone running: set `clipboardSharing=false` in
  `~/.config/Android Open Source Project/Emulator.conf`.
- A locked phone needs a display so its operator can unlock it. It needs an X11 display (`DISPLAY`;
  Wayland desktops provide one through XWayland), and orion-phone refuses to start a locked phone
  without one.
- Phones run only while your user's systemd services run.
- Leases use the wall clock. A forward jump (a host suspend, for instance) ends leases early: safe,
  because a lease taken over from a holder still mid-action waits for that action to finish, and
  a locked phone whose lease ends early shuts itself down. A backward jump can briefly revive an
  expired one.

## License

MIT
