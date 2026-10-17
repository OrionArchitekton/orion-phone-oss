---
verified: 2026-10-07
review_after: 2026-12-31
topics: [orion-phone, systemd, android-emulator]
references:
  - src/orion_phone/adb.py
  - src/orion_phone/phone.py
  - src/orion_phone/services.py
  - src/orion_phone/setup.py
  - config.example.toml
---

# Runbook: phone services

## What runs

One systemd user unit per configured phone, `orion-phone-<name>.service`, and one that runs the
debug bridge's server, `orion-phone-adb-server.service`, all written by `orion-phone install-units`
(also run by `orion-phone setup`). The server's unit runs `adb -L tcp:localhost:5037 nodaemon
server` in the foreground (adb is the `android.adb` of the config), so it listens on loopback port
5037 only, whatever `ADB_SERVER_SOCKET` or `ANDROID_ADB_SERVER_PORT` the user manager's environment
holds. Every adb command orion-phone runs is given the same address (`-L tcp:localhost:5037`, the
command that tells an emulator to exit included), so none of `ADB_SERVER_SOCKET`,
`ANDROID_ADB_SERVER_ADDRESS` and `ANDROID_ADB_SERVER_PORT` in the environment of the CLI or of an
agent can send one elsewhere. An `adb` you run by hand, or another adb tool, is left to its own
environment: it reaches the server only on that default address. The server runs from login,
whatever the tiers of the phones: it has an `[Install]` section (`WantedBy=default.target`),
`install-units` enables it without starting it, and `setup` starts it, before the phones, and waits
until it is up (below). It has to
be running before the first lease, because a lease talks to adb before it starts a locked phone:
nothing a phone's unit does could start it in time. (Every phone's unit also wants it and starts
after it, `Wants=` and `After=`: an open phone's unit starting, at login or by hand, starts the
server if it is not running. A locked phone's lease does not get the server that way, because its
first adb call comes before its unit starts. The server keeps running when the phones stop.) So a
stopped server is started when the user's service manager next starts (normally after a reboot, or a
restart of the WSL instance: a login starts nothing when the manager already runs, as with lingering
on or another session open), by a manual `systemctl --user start`, or by an open phone's unit
starting; after an upgrade, or after `systemctl --user stop orion-phone-adb-server.service`, start
it by hand (see Rollout). `systemctl start` returning 0 does not mean the server is up: the unit
counts as started once adb is spawned, not once adb has bound its port, so with another adb server
holding the port it returns 0 at once and the unit's adb aborts about a second later. `setup`
therefore waits, after it starts the server and before it starts an open phone, for up to 10
seconds, until the unit is active, its port accepts a connection (looked at a few times a second),
and both have held for 2 seconds. A slow machine can lengthen the two with
`ORION_PHONE_SERVER_WAIT` and `ORION_PHONE_SERVER_SETTLE` (seconds; no more than 120 and 30). Keep
the settle below the wait, and above the second or less that an adb takes to abort, or setup can
fail for good, or pass a server that is about to fail. A unit that has failed ends the wait at
once, whatever answers on the port, since another server may be what answers. `install-units`
starts nothing and has no such wait, and neither has a start at login (the manager starting the
unit, or an open phone's unit starting it): a failure then shows as `adb timed out after 30s` from
`orion-phone status` where no other server answers, and the Common failures entries on that and on
a failed server unit say what to do. The unit starts the server again
two seconds after it ends:
`adb kill-server` ends it with status 0, and it is back about two seconds later, and `systemctl
--user stop` is what stops it. That holds when nothing else takes the port. On a host where a
refused connect makes any adb client start a server of its own (ordinary Linux), something can take
the port first: an adb call in those two seconds, or another tool's server (an IDE's, or a newer
platform-tools after an update). The unit's next start then aborts once (`Address already in use`)
and the unit stays failed, while that other server serves the phones: an abort is not restarted,
because adb would abort every 2.7 seconds for ever and write a core dump each time (see Common
failures). orion-phone runs the server itself because adb starts its own only when its first
connect to that port is refused, and under WSL2's mirrored networking a connect to a port with
nothing listening is dropped and hangs instead: adb cannot start a server of its own there, and
with none running every adb call that goes through it waits out its 30 seconds. One adb command
does not go through the server: the one that tells an emulator to exit (`adb -s
emulator-<console port> emu kill`) connects to the phone's console port itself. Where nothing
listens on a port the same networking makes that connect hang too, so a stop sends it only when
the phone's console port accepts a connection: a locked phone is down between sessions, and the
cold boot of one that is down does not wait for it. A phone's unit carries the `DISPLAY` of
the shell that installed it (only when it is set) and always names the AVD home that shell
resolved (`ANDROID_AVD_HOME`, else `ANDROID_USER_HOME/avd`, else `~/.android/avd`), so install
units from the shell that ran `setup`.
Run `orion-phone install-units` again after changing a phone's tier
or anything else that goes into its unit: a phone switched to `locked` keeps its old open unit
(no session cap, and a login link) until then, and a lease on it is refused, not started without
its cap. If that phone is already running from the old unit, install-units stops it first, before
it writes any unit, and says which phones it stopped (a session on it ends, and its holder leases
again). It stops a phone as a release does: it waits for an action in flight on the phone (for at
most 3 minutes, as the lease watch does: it fails rather than stop the phone under the action),
tells the emulator to exit (when its console port accepts a connection), and looks at adb and the
phone's ports for up to `ORION_PHONE_GONE_WAIT` seconds, and it names a phone as stopped only once
it is shown gone, because an old unit may be one
whose stop leaves the emulator running. It reads the running locked phones a second time after its
reload, because the reload also loads a pending drop-in into a running unit (a `RuntimeMaxSec` or
`RuntimeRandomizedExtraSec` in a drop-in takes effect then) and stops any session that is no longer
exactly the configured cap.
A locked phone whose running unit has the configured cap before and after is left alone, and so
is one that is not running, or whose unit systemd does not have yet (a first install: the
install writes the units, reloads, and the manager has them).
In a run that completes, every login link is made before a reload, and a reload is always
followed by that look at the running locked phones: a link made without a reload (`systemctl
--user enable --no-reload`, which is how orion-phone makes them, so that no reload comes after a
look) leaves the whole manager saying `NeedDaemonReload=yes` for every unit until the next
reload, and a lease on a locked phone is refused for that. So `install-units` makes the adb
server's link, then reloads once, then looks. `setup` runs it, links the open phones, reloads
once more and looks again, and only then starts the adb server and the open phones (a start
changes no link).
Open phones start at login and restart on failure; the emulator is the unit's own process.
Locked phones start only when an agent leases them and never restart. A
locked unit runs its emulator as a child and watches the phone's lease: every 15 seconds it reads
the lease, and when none stands (none held, expired, bound to another boot, taken by a newer session
that has not yet stopped this boot, or unreadable) it stops
the emulator (SIGTERM, then SIGKILL after 30 seconds) and ends cleanly. So a locked phone stops on
release, within about 45 seconds after its lease lapses (later by the time an action in flight on the
phone takes to end, at most 3 minutes: the watch does not shut a phone down under an action), or
after `session_cap_minutes`. The
interval is `ORION_PHONE_WATCH_INTERVAL` (never more than 300), which the unit reads from the user
manager's environment (`systemctl --user set-environment ORION_PHONE_WATCH_INTERVAL=30`; it applies
to the next start). A locked unit also names `KillMode=control-group` and `TimeoutStopSec=90`, so a
stop takes the emulator's helper processes with it and has room for the grace; a lease is refused
while systemd has loaded another kill mode, a stop time under 60 seconds, or a `SendSIGKILL` other than `yes` (see Common
failures). The watch reads the
leases in the state directory the unit was installed with, so install the units from the environment
your agents use. Never start a locked unit by hand with `systemctl`: with no lease its watch stops
the phone at the first look. Use `orion-phone lease`.

Every phone's unit, open or locked, sets `LimitCORE=1`, so a crash of its emulator leaves no dump of
the phone's memory (see SECURITY.md); only a locked unit's is checked. Every phone cold-boots: no
quick-boot snapshot is saved or loaded, because loading one restores the phone's disks as they were
at its save (an open phone restarted after a crash used to come back without what was done since),
and a launch deletes any left over. An open phone takes about a minute to boot.

## Rollout

1. `orion-phone setup` from a desktop shell. It enables the adb server at login and the open
   phones too, reloads systemd, and then starts the adb server, before the phones, whatever their
   tiers, waits until the server is up (up to 10 seconds), and then starts the open phones. If the
   server is not up it fails and starts no phone: see Common failures.
2. `orion-phone status a`: exits 0 once the first boot finishes.
3. Keep open phones' screens on: `~/Android/Sdk/platform-tools/adb -s emulator-5554 shell svc power stayon true` (once per phone).
4. Sign in by hand on open phones. Before any sign-in on a locked phone, follow First boot of a
   locked phone below.

After any upgrade, restart each open phone once, while no agent is using it
(`systemctl --user restart orion-phone-<name>`): `setup` and `install-units` only start a phone
that is down, and a running emulator keeps the core limit and the command line it started with.
From a version before 0.2.0, that restart is what gives an open phone its cold boot and
`LimitCORE=1`.

After an upgrade from a version that had no adb server unit:

1. `orion-phone install-units`, from the environment your agents use. It writes
   `orion-phone-adb-server.service`, adds `Wants=` and `After=` lines for it to every phone's
   unit, and enables the server at login (the login link only, made before the reload that
   loads the units, and with no reload of its own: it starts nothing).
2. If an adb server is already running outside the unit (one an adb command started on its own, or
   one you started by hand), stop it first with `~/Android/Sdk/platform-tools/adb kill-server`:
   the unit cannot take port 5037 while another server holds it. Do it while no agent is using a
   phone: a call made while the new server finds the running phones again can fail.
3. `systemctl --user start orion-phone-adb-server.service`, whatever the phones' tiers. Until the
   user's service manager next starts, nothing else starts it except an open phone's unit
   starting: a locked phone's lease does not, because it talks to adb before it starts the
   phone, so the server must already be running when the first lease comes. A phone that is
   already running needs no restart for it. The same command starts it after any explicit
   `systemctl --user stop`.
4. `systemctl --user status orion-phone-adb-server.service` says `active (running)`.

## Viewing a phone

Each phone's emulator opens a window when its unit was installed from a shell with `DISPLAY`.
Never close one: closing the window shuts that phone down, and the shutdown can take every other
running phone down with it, because the emulators share one network service. Stop an open phone
with `systemctl --user stop orion-phone-<name>.service`, and a locked one by releasing its lease.

[scrcpy](https://github.com/Genymobile/scrcpy) mirrors a phone in a window of its own, and closing
that window stops nothing: `scrcpy -s emulator-5554`. It reaches the phone through the adb server
at the default address, loopback port 5037, where orion-phone runs it. Under WSL2 the emulator's
window can open where you cannot see it (over Remote Desktop it lands off the screen). With
mirrored networking, run the Windows build of scrcpy on the Windows side instead: it reaches the
same server through the shared loopback (scrcpy 4.1 for Windows has been used this way).

- A screen that an app marks as secure (PIN entry, the lock screen's PIN pad, an authenticator)
  shows black in scrcpy and in every screenshot. Type on it anyway: the phone receives the keys
  and taps.
- A locked phone's display is asleep after its cold boot. The lease that finds its lock screen
  wakes it. To wake a phone by hand, press its power key from scrcpy (Alt+P by default).

## First boot of a locked phone

Here `b` and `emulator-5556` are the locked phone of `config.example.toml`; use your own phone
name and its serial (`emulator-` plus its `console_port`).

A locked phone shuts itself down shortly after its lease lapses, so keep the lease while you work:
each `lease` call and each action extends it by `ttl_minutes` (default 30). If you need longer
between calls, raise `ttl_minutes` in the config for the setup. A phone that shut down loses
nothing but the session: its data stays, and the next lease cold-boots it.

1. `orion-phone lease b --holder operator` from a desktop shell, with the adb server already
   running (`orion-phone setup` started it and enabled it at login; `systemctl --user status
   orion-phone-adb-server.service` says so, and `systemctl --user start
   orion-phone-adb-server.service` starts it after an upgrade or an explicit stop: a lease talks to
   adb before it starts the phone, so the phone's own start cannot). The first call boots the phone
   and returns NEEDS_OPERATOR (state `starting`, and it says not to unlock the phone yet). Lease
   again shortly, until the reply is NEEDS_OPERATOR with state `no-screen-lock`. A new phone has
   no screen lock at all (its window shows no lock screen, and there is nothing to swipe), and
   orion-phone refuses a locked phone that has none, so nothing else can be done with it until
   step 2.
2. In the phone's window (or its scrcpy mirror: see Viewing a phone), set a screen-lock PIN (Settings, then Security, then Screen lock, then
   PIN; menu names vary by Android version). Do this before signing in to anything: until a PIN
   exists, anyone who can reach the phone can use it.
3. Check the image, with the phone running (the lease of step 4 restarts it):

   ```bash
   ~/Android/Sdk/platform-tools/adb -s emulator-5556 shell getprop ro.crypto.type   # prints file
   ~/Android/Sdk/platform-tools/adb -s emulator-5556 root                           # is refused
   ```

   If the first does not print `file`, or the second is not refused, keep accounts off this
   phone (see SECURITY.md). orion-phone already reads `ro.crypto.type` and `ro.debuggable` at
   every locked boot, so on an image that reports anything but `file` and `0` the `lease` call of
   step 1 that finds the phone booted stops with USAGE and shuts the phone down. This step stays
   as a double-check, and it is the only look at `adb root`.
4. `orion-phone lease b --holder operator` again. This boot was seen with no screen lock, and
   orion-phone never uses such a boot, even once a PIN is set during it: you were signed in when
   you set the PIN, so there was no lock screen for you to unlock, and whoever held the lease
   would be handed an unlocked phone. (It would not use a boot it never saw at its lock screen
   either: a boot is used only after a look, a lease's or an action's, has seen its lock screen
   showing over a screen lock, and the phone is then found unlocked.) So this lease stops the
   phone and cold-boots it, and answers NEEDS_OPERATOR with state `starting` (an action in its
   place would say `restart-needed`: lease again). No `release` is needed first, though one works
   too.
5. Lease it again, and again shortly, until the reply is NEEDS_OPERATOR with state `locked`
   (while it says `starting` or `booting`, do not unlock the phone yet: the unlock is asked for
   only once the phone's image has been checked, and a boot that is unlocked before a look has
   seen its lock screen is restarted instead of used; if it says `no-screen-lock`, the PIN was
   not set: go back to step 2). The `locked` reply is the sighting of the lock screen that the
   boot needs. Then swipe up: the phone must ask for the PIN, which proves it is in force.
   Enter it, lease again, then sign in by hand. Release when done.

## Monitoring

- `orion-phone status <phone>` exits 0 only with positive proof: booted and a non-blank frame.
- `systemctl --user status orion-phone-<name>`; logs with `journalctl --user -u orion-phone-<name> -n 100`
  (a locked phone's log says why its unit stopped the emulator).
- `systemctl --user status orion-phone-adb-server.service`: the debug bridge's server, which every
  adb call but the emulator's exit command needs (see Common failures for `adb timed out after
  30s`).
- Audit trail: `~/.local/state/orion-phone/audit/<phone>/<YYYY-MM-DD>.jsonl` (UTC dates).
  Screenshots are in `shots/<phone>/` and UI dumps in `ui/<phone>/` of the same state directory.

## Common failures

- Every phone window closes at once, and the open phones restart: the journal shows `XIO:  fatal
  IO error 2 (No such file or directory) on X server ":0"`, then `status=11/SEGV` (up to a minute
  later from an open unit written before 0.2.0, which waits for its crash dump). WSLg's
  compositor crashed and took the display with it. `/mnt/wslg/stderr.log` names the cause; the
  known one is its clipboard bridge (`rdpclip.c: ... Assertion ... failed`, microsoft/wslg#1407),
  which the emulator's clipboard sharing sets off. With no phone running, set
  `clipboardSharing=false` in `~/.config/Android Open Source Project/Emulator.conf`. A locked
  phone that was running is down (locked units never restart on their own): lease it again, and
  it cold-boots and asks for its unlock.
- PHONE_DOWN on an open phone: `systemctl --user restart orion-phone-<name>`, then read the
  journal. A KVM permission error means your user cannot read and write `/dev/kvm`.
- `adb timed out after 30s` (PHONE_DOWN, from any command that talks to a phone: `status`, a lease, a
  release or an action; the message names the unit and how to start it): the debug bridge's server
  is not running, whether or not the phone's own unit is. adb starts its own server only when its
  first connect to loopback port 5037 is refused, and under WSL2's mirrored networking a connect
  to a port with nothing listening is dropped and hangs instead, so adb cannot start one and every
  call that goes through the server waits out its 30 seconds (the command that tells an emulator to
  exit does not go through it, and a stop sends it only to a console that answers). orion-phone
  runs the server as its own unit for this reason. Look at `systemctl --user status
  orion-phone-adb-server.service`, and start it with `systemctl --user start
  orion-phone-adb-server.service`. `setup` and `install-units` enable it at login, so it was
  stopped by hand, or it failed after an abort (the expected cause is another adb server holding
  port 5037: see the next entry), or the units were installed before it existed (the unit file
  is missing, or has no `[Install]` section, `systemctl --user cat
  orion-phone-adb-server.service`): follow "After an upgrade" in Rollout. A lease talks to adb
  before it starts a phone, so the phone's own unit is no help here, and neither is leasing
  again. The environment is not the cause: orion-phone's own calls name the server's address and
  follow none of `ADB_SERVER_SOCKET`, `ANDROID_ADB_SERVER_ADDRESS` and `ANDROID_ADB_SERVER_PORT`.
  An `adb` you type in a shell that sets one of them does follow it, and can hang the same way:
  unset it, or give `-L tcp:localhost:5037`.
- `orion-phone-adb-server.service` is failed, and `journalctl --user -u orion-phone-adb-server -n
  20` says `Address already in use` (adb's own words: `could not install *smartsocket* listener`):
  another adb server holds port 5037 (one an adb command started on its own, another tool's such as
  an IDE's, or a newer platform-tools after an update), and it serves the phones for now. The unit
  made one attempt and stopped, because adb aborts every time while the port is held, and would
  otherwise do so every 2.7 seconds for ever, with a core dump each time. Run
  `~/Android/Sdk/platform-tools/adb kill-server`, then `systemctl --user start
  orion-phone-adb-server.service`. Do it while no agent is using a phone: a call made while the
  new server finds the running phones again can fail. `orion-phone setup` finds this right after
  it starts the unit and fails (the entry above); a start at login is not checked that way, so look
  at the unit's state after a reboot if you cannot tell whose server answers.
- No window on an open phone: the units were installed from a shell without `DISPLAY`. Rerun
  `orion-phone install-units` from a desktop shell and restart the unit.
- A window you cannot see (under WSL2 over Remote Desktop it opens off the screen), or every
  phone stopping after one window was closed: see Viewing a phone.
- A locked phone never shows a window: its lease keeps returning NEEDS_OPERATOR (starting), or
  PHONE_DOWN (not active after it was started), because its unit refuses to start without a
  display and exits at once. `journalctl --user -u orion-phone-b -n 20` says it needs an X11
  display: the units were installed without `DISPLAY`. Rerun `orion-phone install-units` from a
  desktop shell, then lease again.
- A phone's unit exits at once and `journalctl --user -u orion-phone-<name> -n 20` complains about
  its AVD home: the one its unit names is no longer fit to hold phone data. A repository now
  surrounds it, or it is not an existing directory any more (the message says which). Run
  `orion-phone setup` from a shell whose `ANDROID_AVD_HOME` (or `ANDROID_USER_HOME`) names a
  directory outside any repository, or unset both to use `~/.android/avd`: it makes the directory
  if it is missing and rewrites the units.
- A lease on a locked phone is refused with USAGE naming `ro.crypto.type` and `ro.debuggable`: the
  phone booted an image that reports something other than `file` and `0`. orion-phone shut the
  phone down and freed the lease. A phone keeps the image it was created from, and `setup` never
  changes one that exists: if the config named another image when the phone was made, delete the
  phone (`avdmanager delete avd -n orion-phone-<name>`, as in Rollback; this erases its data),
  set a Google Play image in the config, and run `orion-phone setup` again. If it already is a
  Google Play image, check it by hand as in First boot of a locked phone, step 3.
- A lease (or a release) on a locked phone is refused with USAGE saying its unit is not installed:
  systemd has no unit by that name, so there is nothing to stop or start. This is the first use of
  a phone before `orion-phone install-units` (or `setup`) has run, or a unit file that was
  removed. Run `orion-phone install-units` from the environment your agents use, then lease
  again. To see what systemd has: `systemctl --user show -p LoadState
  orion-phone-<name>.service` prints `LoadState=not-found` for a unit it has not got, and
  `loaded` for one it has. With no unit to stop, orion-phone still looks for an emulator on the
  phone's serial (adb, then the console and adb ports) before it refuses, and tells one whose
  console accepts a connection to exit; if one is still there the refusal is PHONE_DOWN, naming
  what answers, and the advice is to stop that emulator by hand (`adb -s emulator-<console port>
  emu kill`) and then install the units.
- A lease on a locked phone is refused with USAGE saying its unit is not the one this config and
  environment call for (the message says which): no session cap, a cap other than
  `session_cap_minutes`, a unit file that changed since systemd loaded it, extra run time past the
  cap, a stop that would not end the phone in time (the next but one entry), a `Restart` other
  than `no` (below), a property systemd did not print, or another state directory (the entry
  after). The phone
  was shut down, not started, and the lease freed. Fix what the message names, then lease again.
  For the first three, run `orion-phone install-units`, which rewrites the units and reloads
  systemd. Extra run time is `RuntimeRandomizedExtraSec`: systemd
  stops the unit at the cap plus a random span of up to that much, so a session could outlast the
  cap. The units orion-phone writes never set it, so a drop-in does (`systemctl --user cat
  orion-phone-<name>.service` lists the unit and its drop-ins): remove the setting, then run
  `orion-phone install-units` to reload systemd. (`NeedDaemonReload` also says `yes` for every
  unit after a login link was made or removed with `--no-reload` and nothing has reloaded since:
  `systemctl --user daemon-reload` clears it. orion-phone's own commands reload after their links,
  so a run that succeeds, once its reload has loaded what is on disk, does not leave it, and one
  that fails says to run it again.) A message that says systemd printed no
  `RuntimeRandomizedExtraUSec` means systemd older than 250, which has no such property: a locked
  phone needs 250 or later (`systemctl --version`). To see what systemd has loaded: `systemctl
  --user show -p LoadState -p RuntimeMaxUSec -p RuntimeRandomizedExtraUSec -p KillMode -p
  TimeoutStopUSec -p SendSIGKILL -p Restart -p NeedDaemonReload orion-phone-<name>.service`.
  `LoadState` must say `loaded`, the cap prints as `2h` for 120 minutes and as `infinity` when
  there is none, `RuntimeRandomizedExtraUSec` must say `0`, `KillMode` must say `control-group`,
  `TimeoutStopUSec` must say `1min` or more (the default `1min 30s` is fine), `SendSIGKILL` must
  say `yes`, `Restart` must say `no`, and `NeedDaemonReload` must say `no`.
- A lease on a locked phone is refused with USAGE naming `Restart`: systemd would start the phone
  again by itself when its unit ends. A locked phone is started only by a lease, as a cold boot, and
  is over when its session is, so a unit that systemd restarts would bring back a phone that no
  lease asked for. The units orion-phone writes say `Restart=no` (and have no `[Install]`
  section), so a drop-in sets the other (`systemctl --user cat orion-phone-<name>.service` lists
  the unit and its drop-ins): remove the setting, then run `orion-phone install-units`. The check
  covers a running phone too: `install-units` stops one whose loaded unit says otherwise.
- A lease on a locked phone is refused with USAGE naming `KillMode`, `TimeoutStopUSec` or
  `SendSIGKILL`: the unit's stop would not end the phone in time. The session cap, the lease watch and a release all
  end a phone by stopping its unit, so the stop must reach every process of the unit
  (`KillMode=control-group`: with `process` the emulator keeps running when the unit is stopped,
  and with `none` everything does, while systemd reports the unit as stopped) and be given the
  time the watch's own stop takes (a 30 second grace for SIGTERM and another after SIGKILL: at
  least 60 seconds, and not `infinity`), and when that time runs out systemd must kill what is left
  (`SendSIGKILL=yes`, the default: with `no`, a stop of a unit that ignores SIGTERM ends with the
  unit's processes still running). The units orion-phone writes say `KillMode=control-group` and
  `TimeoutStopSec=90` and leave `SendSIGKILL` at its default, so a drop-in sets the other
  (`systemctl --user cat orion-phone-<name>.service` lists the unit and its drop-ins): remove the
  setting, then run `orion-phone install-units`. A unit written before it named them has systemd's
  defaults, which pass unless the manager's `DefaultTimeoutStopSec` is under a minute:
  install-units writes it out.
- A lease on a locked phone is refused with USAGE naming `LimitCORE` or `LimitCORESoft`: the unit
  would let a crash of the emulator leave a dump of the phone's memory. The units orion-phone
  writes say `LimitCORE=1`, which sets both limits to 1, the only value that keeps a dump from a
  crash handler such as systemd-coredump or WSL2's crash capture (0 does not). A unit written
  before this has systemd's defaults (`infinity` and `0`): run `orion-phone install-units`, which
  writes the limit, stops a locked phone that is running without it, and reloads. Otherwise a
  drop-in sets another limit (`systemctl --user cat orion-phone-<name>.service` lists the unit and
  its drop-ins): remove it, then run `orion-phone install-units`.
- A lease on a locked phone is refused with USAGE saying its unit watches the state directory X but
  this command uses Y (or that the unit names no state directory, or its environment could not be
  read): the unit was installed from a shell with another `ORION_PHONE_STATE` than the one your
  agents use. Its watch would find no lease in X and shut the phone down at its first look, so the
  lease is refused up front instead. Run `orion-phone install-units` from the environment your
  agents use (the same `ORION_PHONE_STATE` and `ORION_PHONE_CONFIG`), then lease again.
- `orion-phone install-units` refuses with USAGE naming a unit it could not stop: a locked phone
  is running from a unit that is not confirmed to enforce the session cap the config calls for
  (the old open unit of a phone switched to `locked`, another cap, extra run time past the cap, a
  kill mode, stop time or `SendSIGKILL` that may leave the emulator running, a `Restart` other than
  `no` that would have systemd start the phone again, core limits other than 1 that would let a
  crash leave a dump of the phone's memory, or a reload that systemd has not made since the file
  changed), and it could not be stopped and shown gone. Either
  `systemctl --user stop` failed, or it exited 0 and the emulator is still there: the message says
  what still answers (adb still lists `emulator-<console port>`, or that port still answers). The
  stop is not taken for the phone being gone, because a unit like these may be one whose stop leaves
  the emulator alive: the install waits for an action in flight on the phone, tells the emulator to
  exit (when its console port accepts a connection), and looks at adb and the phone's ports for up
  to `ORION_PHONE_GONE_WAIT` seconds. Do not
  trust that phone until it is gone: it may be running, possibly unlocked, outside any cap. The
  install looks at every running locked phone before it fails, so the message names each phone it
  could not show gone and what still answers for it, and `stopped` lists the ones it did stop
  (`failed` lists every phone not stopped or not shown gone). A phone it could not even try to
  stop says "not stopped" instead, and nothing of its emulator (`not_stopped` lists these): its
  action lock stayed busy for 3 minutes (an action is still running on it: let the action end, or
  stop the phone by hand, then run install-units again), or its lock cannot be taken. The message
  names the lock file either way: a directory in its place, or no directory to hold it (make it a
  file you can open for writing, or remove it, since it is made again when missing), or a state
  directory that cannot be used (the message says why, and to set `ORION_PHONE_STATE` to a
  directory you own). If the
  message says the units were written and reloaded, a drop-in changed the running unit at the
  reload (`systemctl --user cat orion-phone-<name>.service` lists the unit and its drop-ins):
  remove the setting that changes the unit (the cap, the extra run time, the kill mode, the stop
  time, `SendSIGKILL` or the core limits). Otherwise nothing was written. Stop the phone by hand
  (`systemctl --user stop orion-phone-<name>.service`; if the emulator still runs, `adb -s
  emulator-<console port> emu kill`), check that adb no longer lists it, then run
  `orion-phone install-units` again. The error's `stopped` lists the phones the install did stop
  and show gone, here and when it fails for any other reason after stopping some (the data lists
  them; the sessions ended either way).
- `orion-phone setup` fails with PHONE_DOWN after the install, naming the `enable` or the `start` of a
  unit, the `daemon-reload`, or the `show` of a phone's unit that failed or timed out: the message
  says that the units are written, names the phones the install or setup stopped (the data's
  `stopped` lists them too), and says to run `orion-phone setup` again, which repeats cleanly: it
  writes the units, links, reloads, looks and starts again. The `enable` is the login link of an
  open phone, which comes before setup's own reload; the `show` is the look after that reload,
  which could not read a unit (so it is not known whether a running locked phone's unit enforces
  its session cap; `install-units` says the same after its own reload, and to run itself again); the
  `start` comes after it, so everything a lease needs is loaded by then.
- `orion-phone setup` fails with PHONE_DOWN saying that `orion-phone-adb-server.service` is failed
  after it was started, or that it did not stay up with port 5037 answering (or that it could not
  be looked at: the `show` that failed is then systemctl's own words): the adb server is not up, and
  setup started no open phone. The message says that the units are written, names the phones
  stopped, and says what to do first; the data has the `unit`, its `state`, whether the port
  answers (`port_answers`) and the phones stopped (`stopped`). The expected cause is another adb
  server holding port 5037: if
  `journalctl --user -u orion-phone-adb-server -n 20` says `Address already in use`, run
  `~/Android/Sdk/platform-tools/adb kill-server`, then `systemctl --user start
  orion-phone-adb-server.service`, then `orion-phone setup` again. The other server answers, so
  the port is no proof that the server is yours: setup looks at the unit too, and a failed unit
  ends its wait at once. Where the unit stays active and nothing answers within 10 seconds, read
  the journal for what adb says. Only setup checks this. A start at login (or an open phone's unit
  starting the server) is not checked that way: after a reboot, `orion-phone status` reports
  `adb timed out after 30s` where no server answers (the entry above), and
  `systemctl --user status orion-phone-adb-server.service` shows a failed unit where another
  server answers for now (the entry below).
- `orion-phone install-units` fails with USAGE saying `cannot sync` and naming the unit directory
  (`~/.config/systemd/user`, or where `ORION_PHONE_UNIT_DIR` points), with an I/O error from the
  disk or filesystem under it: the units are written, but they may not survive a power loss until
  the sync succeeds, and the install went no further (it made no login link and reloaded nothing,
  so systemd still runs what it had loaded). The message says to run `orion-phone install-units`
  again once the disk is fixed, and names the phones the install stopped before the sync (the
  data's `stopped` lists them too).
- `orion-phone install-units` fails with PHONE_DOWN naming `daemon-reload` (it timed out or failed)
  and says to run it again once systemd is responsive. The units are written, and systemd may still
  load them, so a drop-in that changes a running locked phone's cap could still take effect: the
  install looked at the running locked phones anyway and stopped any it could not confirm (the
  error's `stopped` lists the ones it shows gone; a phone it could not show gone is named in the
  message, with the advice not to trust it until it is). When `systemctl --user` answers again,
  run `orion-phone install-units` again; it reloads and looks once more.
- Black or crashing window: set `gpu = "swiftshader_indirect"` in the config and restart the unit.
- NEEDS_OPERATOR with state `no-screen-lock`: the locked phone has no screen lock, so nothing
  protects it, and orion-phone refuses it before it reads the lock state (a new phone boots like
  this: no lock screen, nothing to swipe). Its operator sets a PIN on the phone's own screen
  (Settings, then Security, then Screen lock), and the next lease restarts the phone (see
  `restart-needed` below); for a new phone follow First boot of a locked phone. A lock that needs
  only a swipe is no screen lock either. The check
  reads user 0's `CredentialType` in `adb -s emulator-<console port> shell dumpsys lock_settings`:
  only `PIN`, `PASSWORD` and `PATTERN` pass, and `NONE` is no screen lock. State `unknown` with a
  message about the screen lock means that report could not be read with certainty: no
  `User State:` section, or more than one; no `User 0` line in it, or more than one; not exactly
  one `CredentialType:` line in user 0's block; a credential line that is not exactly
  `CredentialType: NAME` (anything but blanks after the name, a missing or doubled space, an empty
  name, or a name that is not capital letters and underscores); a name other than `PIN`,
  `PASSWORD`, `PATTERN` and `NONE`; or a report cut at its timeout. Look at the report to see
  which. The phone is treated as having no screen lock until it reads.
- NEEDS_OPERATOR with state `restart-needed` (from an action), or a lease that answers `starting`
  with a message saying that the phone is starting again because its last boot is not used: that
  boot was found unlocked, with a screen lock, without ever having been seen at its lock screen
  (it was unlocked before any look, a lease's or an action's, said `locked`, or a PIN was set
  during it), or it was seen with no screen lock (`no-screen-lock`) and has one now, even if its
  lock screen has shown since. A boot is used only after a look, a lease's or an action's, has
  seen its lock screen showing over a screen lock and the phone is then found unlocked: a PIN set
  during a boot leaves the phone unlocked, because its operator was signed in when they set it,
  with no lock screen for anyone to unlock, and whoever held the lease would be handed an
  unlocked phone. An action only refuses. The next lease stops
  the phone for certain and cold-boots it (`starting`, then `booting`, then `locked`), and its
  operator unlocks it from its lock screen once a lease says `locked`: there is nothing to fix by
  hand, and unlocking earlier only costs another restart. A lease that still finds no screen lock
  says `no-screen-lock` and restarts nothing. The result's `cause` says which of the two it was
  (`never-seen-locked` or `seen-with-none`), and so does the audit record, of the action that
  was refused and of the lease that restarted the phone (or failed to).
- NEEDS_OPERATOR with state `locked` that never clears: the locked phone is at its lock screen;
  unlock it in its window. (That reply is what records that the boot was seen at its lock screen,
  so the unlock after it counts. State `starting` or `booting` is not a request to unlock
  anything: lease again shortly; if the phone's unit has stopped meanwhile, that next lease boots
  it afresh.)
  Leases also refuse while the lock state is unreadable (fail closed). The lock state is read only
  from the system's own `KeyguardController:` block of the phone's activity dump (`adb -s
  emulator-<console port> shell dumpsys activity activities`: that header line, then the lines
  under it indented deeper, closed by an empty line). The lines that count are the block's two
  keys: `mKeyguardShowing=true` or `=false`, and the per-display `KeyguardShowing=true` or `=false`
  one column less deep. The dump prints the lock state under other names too, such as the
  `isKeyguardShowing=` line of the display policy, well before the block: orion-phone does not read
  those, so do not take one of them for what it saw. No such block, no counted line in it, a
  `mKeyguardShowing=` or `KeyguardShowing=` line anywhere else in the dump (an app can print one
  with a raw newline in an intent's data or a label), or a dump cut at its timeout reads as
  `unknown`: look at the dump to see which. The block's shape was confirmed on the pinned Android 16
  image. A phone on another image or Android version that stays `unknown` may print its block in
  another shape, and the reader fails closed on it: every locked lease stays at NEEDS_OPERATOR with
  state `unknown`.
- A lease nobody releases: it expires after `ttl_minutes` (default 30); the next lease takes it
  over, once any action still running on the phone has finished. A lease call that does not return
  is waiting for such an action (even on an open phone) and returns when it ends. A lease held by
  someone else is BUSY at once.
- A locked phone stopped by its session cap while an agent keeps tapping: the agent's lease stays
  alive but can never act. Stop that agent; the lease then expires after `ttl_minutes`.
- A locked phone shut down while someone was using it: its lease lapsed (no `lease` call or action
  for `ttl_minutes`, or the clock jumped ahead, as after a host suspend). The unit's log says so
  (`journalctl --user -u orion-phone-b -n 20`: "the lease of ... has expired", or "was taken after
  this boot started" or "is bound to no boot, though a lease was bound to this one" when another
  agent leased the phone and its command died before it could stop the old boot: the watch stopped
  it for that agent). Lease it again: it cold-boots, and its operator unlocks it again. A locked
  phone still running long after its lease lapsed means no watch is running: the unit was started before this version, or
  `ORION_PHONE_WATCH_INTERVAL` is large. Stop it with `systemctl --user stop orion-phone-b`; the
  next lease cold-boots it anyway.
- A locked phone keeps running for minutes after its lease lapsed, and the unit's log says "waiting
  for an action in flight": an `orion-phone` command (a tap, a screenshot, a lease that is waiting on
  the same lock) is still running on that phone, and the watch does not shut a phone down under an
  action. It waits for at most 3 minutes, then the log says "stopping the emulator of phone b
  anyway" and the phone goes down. If a command hangs for good (an adb call that never returns),
  find it (`ps -ef | grep orion-phone`) and stop it; the phone then goes down within a fifth of a
  second.
- Stopping a locked phone takes up to 30 seconds longer than usual: its emulator did not leave when
  asked and was killed after the grace. Nothing to fix; `release` waits for it.
- `release b` returns PHONE_DOWN: the phone did not go away after its unit stopped (an emulator
  started by hand?). Close that emulator, then release again.

## Rollback

```bash
# run this block as written, without `set -e`: every line must run, even after an earlier one has failed
# every unit, the adb server's too, is stopped first, a line each, and then disabled, a line each: see below
systemctl --user stop orion-phone-a.service
systemctl --user stop orion-phone-b.service
systemctl --user stop orion-phone-adb-server.service
systemctl --user disable orion-phone-a.service
systemctl --user disable orion-phone-b.service
systemctl --user disable orion-phone-adb-server.service
# a disable that failed (its unit file is gone) left its login link: each is removed by its path
rm -f ~/.config/systemd/user/default.target.wants/orion-phone-a.service
rm -f ~/.config/systemd/user/default.target.wants/orion-phone-b.service
rm -f ~/.config/systemd/user/default.target.wants/orion-phone-adb-server.service
(
  # The AVD home the units name, read before the units go, from the first phone unit that names one (a unit writes % and \
  # doubled, which the second sed undoes). Units that all exist and name none are from before units named it: nothing is
  # exported then, and avdmanager follows your own environment (an exported ANDROID_AVD_HOME, or the avd directory inside an
  # exported ANDROID_USER_HOME, else the tools' default). With a unit file gone and none left naming it, the home is not
  # known: nothing is deleted on a guess.
  home= gone=
  for unit in ~/.config/systemd/user/orion-phone-a.service ~/.config/systemd/user/orion-phone-b.service; do
    [ -e "$unit" ] || { gone=1; continue; }
    home=$(sed -n 's/^Environment="ANDROID_AVD_HOME=\(.*\)"$/\1/p' "$unit" | sed 's/%%/%/g; s/\\\\/\\/g')
    [ -n "$home" ] && break
  done
  if [ -z "$home" ] && [ -n "$gone" ]; then
    echo "the AVD home is not known (a unit file is gone, and none left names it): no phone was deleted, so delete them from the home they were made in" >&2
  else
    [ -n "$home" ] && export ANDROID_AVD_HOME="$home"
    ~/Android/Sdk/cmdline-tools/latest/bin/avdmanager delete avd -n orion-phone-a   # the open phone and its data
    ~/Android/Sdk/cmdline-tools/latest/bin/avdmanager delete avd -n orion-phone-b   # the locked phone and its data
  fi
)
rm -f ~/.config/systemd/user/orion-phone-*.service && systemctl --user daemon-reload
rm -r ~/.local/state/orion-phone   # audit records, screenshots, UI dumps, leases
```

The units are removed only after the phones are deleted, because `avdmanager` finds the phones
only in the AVD home the units name, and the block reads it from a unit first. It reads it from
the first phone unit that names one, since a unit file may be gone (removed by hand, which this
allows), and a home a unit names wins over one your shell exports. Units that all exist and name
none are from before units named it: the block then exports nothing and leaves `avdmanager` to
your own environment, so it deletes from the home your shell exports (`ANDROID_AVD_HOME`, or the
`avd` directory inside `ANDROID_USER_HOME`) and, with neither, from the tools' default,
`~/.android/avd`. That is the home those phones were made in only if your shell is set as it was
when you ran `setup`: if it is not, set or unset the variable first, and check with `avdmanager
list avd`. If a unit file is gone and none left names the home, it is not known: the block says
so and deletes no phone, rather than look in a home it guesses (the tools' default, or one your
shell exports) and miss phones kept in another, and it goes on to remove the units and the state,
so delete those phones by hand from the home they were made in.

Every unit has a stop line and a disable line of its own, the adb server's too (it has a login
link like an open phone's, which its disable removes; a locked phone's unit has none). All the
stops come before the first disable, because a disable reloads the manager, and that should not
happen while units still run. A stop reaches a unit that is loaded and running even when its file
is gone, since systemd keeps a unit that runs until it is stopped, whatever happens to its file. A
disable that names a unit file that is not there fails and removes no link, so a `--now` on it
would never stop anything, and one with several units on its line would disable none of them: a
unit file that was removed by hand, or never installed, must not leave another unit running, or
its login link dangling once its file goes. A link that a failed disable left is removed by its
path (`rm -f`). Then `rm -f` takes whatever unit files are left, the server's with the rest, since
its name matches the pattern; it is `-f` so that it succeeds when every file is already gone, and
the reload after it runs either way. To remove only the adb server's unit and keep the phones:
`systemctl --user stop orion-phone-adb-server.service`, `systemctl --user disable
orion-phone-adb-server.service`,
`rm -f ~/.config/systemd/user/default.target.wants/orion-phone-adb-server.service`,
`rm ~/.config/systemd/user/orion-phone-adb-server.service`, then `systemctl --user daemon-reload`.
A phone's unit that still says `Wants=` for it starts without it (systemd ignores a wanted unit it
cannot start), and adb is then back to starting its own server, which hangs under WSL2's mirrored
networking: start one by hand first (`adb nodaemon server`). The config in
`~/.config/orion-phone/` stays until you remove it.
