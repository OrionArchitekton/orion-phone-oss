# Security model

orion-phone coordinates AI agents that drive real, signed-in Android phones. It is a
coordination layer, not a sandbox. This page states what it defends and what it does not.

## Defended

- **Collisions between agents.** Leases give one agent a phone at a time.
- **A confused or manipulated agent acting on a locked phone without its operator.** Locked
  phones never start on their own, always cold-boot without snapshots, refuse every action
  while the lock screen shows (so the tool cannot be used to guess a PIN), refuse a phone that
  has no screen lock (a new phone boots with none, and would read as unlocked), never use a boot
  that was not seen at its lock screen, showing over a screen lock, before it was found unlocked,
  nor one seen with no screen lock (a lease restarts the phone instead), and shut down on
  release, when their lease lapses, and at a hard session cap counted from boot. A lease that
  lapses without a release shuts the phone down within about the watch interval (15 seconds by
  default) plus a 30 second grace for an emulator that does not leave when asked, and plus the
  time an action in flight on the phone takes to end, which the shutdown waits for (for at most
  3 minutes: an action that never ends does not keep a lapsed phone running): the phone's own
  unit watches its lease, and it also stops a boot when a newer session has taken the lease (a
  takeover writes its lease before it stops the old boot, so a takeover command that dies in
  between does not leave the old boot running until the new lease expires). Once the watch has
  seen a lease bound to its boot, any later lease bound to no boot ends it, whatever the clock
  says; before that first binding it compares the wall clock, so only a clock stepped back by
  more than the age of the boot, in the short window between the boot's start and its binding,
  can hide a takeover. Until then the phone may sit running, possibly unlocked, and the next
  lease would shut it down and cold-boot it first anyway, so an unlocked phone never passes from
  one agent's session to another's. The session cap is the backstop if the watch is gone, so a
  new session is refused (USAGE) unless the unit the service manager has loaded would enforce it:
  the configured cap with no extra run time, and a stop that reaches every process of the unit
  (`KillMode=control-group`), is given at least 60 seconds, and kills what is left when that time
  runs out (`SendSIGKILL=yes`), since a unit that systemd reports as stopped can still leave its
  emulator running under another kill mode or without that last kill.
- **A locked phone's memory leaving it through a crash.** A locked phone's unit sets `LimitCORE=1`,
  so a crash of its emulator leaves no dump of the phone's memory, which holds what the lock
  protects once its owner has unlocked it. A limit of 0 would not do it: the kernel still hands a
  dump to a crash handler it pipes dumps to, such as systemd-coredump or WSL2's crash capture,
  since core limits are otherwise irrelevant to pipes, and drops it only at 1, the value the kernel
  (`fs/coredump.c`) takes as the signal to abort a piped dump. The limits are checked like the
  session cap: a lease refuses a unit whose loaded core limits are not 1, and installing the units
  stops a locked phone that runs without them, since a process keeps the limits it started with. (A
  unit changed and reloaded by hand is the exception: see below.) Every open phone's unit sets the
  same limit, unchecked (see below). Every phone's emulator also runs with its crash reporting
  disabled.
- **Personal data reaching this repository.** Tests use synthetic data only; CI scans every
  change for secrets and rejects committed screenshots, UI dumps, and audit logs.

## Not defended

- **A deliberately malicious process on your machine, or on another operating system that
  shares its loopback.** Running as your OS user, it can call the Android debug bridge
  directly, bypass leases, use the emulator console, snapshot a locked phone while it is
  unlocked, or read input events while you type the PIN. Another OS user is not kept out
  either: the debug bridge's server, which orion-phone runs on loopback port 5037, takes commands
  without authentication from any process that can connect to it, under any OS user, so such a
  process can do to a phone what orion-phone does, with no lease and no audit record. Where
  loopback is shared with another operating system (WSL2's mirrored networking), that system's
  processes can reach the server, and the emulators' own ports, too.
- **Anyone who controls the CLI's environment.** The environment is part of the trust
  boundary: whoever sets it controls the tool. `ORION_PHONE_CONFIG`, `ORION_PHONE_STATE`, and
  `ORION_PHONE_UNIT_DIR` choose which config, state directory, and unit directory it trusts,
  and a config that marks a phone open lifts every locked-tier refusal. `ANDROID_AVD_HOME` (or,
  when that is unset, `ANDROID_USER_HOME`, whose `avd` directory the Android tools then use)
  chooses the directory that holds the phones' data (disk images and snapshots), and so where a
  locked phone's launch deletes its snapshots; setup, `install-units`, and every launch refuse a
  home whose variable is not an absolute path, and any home, the default `~/.android/avd`
  included, that lies inside a git repository, which catches a slip, not a hostile environment.
  `ORION_PHONE_PROBE_HOST` moves its look for a phone still running outside its unit to another
  loopback address, which can blind that look: the address also decides whether the emulator is
  told to exit, since `adb emu kill` always connects to 127.0.0.1 and is sent only when the
  console answers at the probe address, so an address where nothing listens blinds the port
  looks and means the kill is never sent, and only `adb devices` is left to see the phone (the
  default address is unaffected). It moves setup's look at the adb server's port too, although
  the server always listens at localhost: set to an address where nothing listens, setup waits
  out its readiness bound and fails even when the server is healthy.
  `ORION_PHONE_SYSTEMCTL_TIMEOUT` and `ORION_PHONE_GONE_WAIT` set
  how long it waits before it gives up. `ORION_PHONE_WATCH_INTERVAL`,
  which a locked phone's unit reads from its service manager's environment, sets how often the
  unit looks at its lease, so raising it lengthens the time a phone keeps running after its
  lease lapses (a value that is not a finite number above zero is ignored, and one above 300
  seconds is held to 300: the watch is never turned off, and a lapse is seen within 300 seconds
  at the most). `ORION_PHONE_SERVER_WAIT` and `ORION_PHONE_SERVER_SETTLE` set how long setup
  waits for the adb server it started to be up (10 seconds) and how long the server must have
  stayed up, with its port answering, before that counts (2 seconds); a value that is not a
  finite number above zero is ignored, and a wait above 120 seconds, or a settle above 30, is
  held to that. A settle shorter than the time an adb takes to abort when another server holds
  its port (about 0.7 seconds) can have setup report as up a server that is about to fail, and a
  settle that is not below the wait means that setup cannot succeed. The five time limits are
  tuning variables, not test-only switches: a slow machine may need longer ones. A locked
  phone's unit carries the state directory it was installed with and watches the leases there,
  so a command that uses another `ORION_PHONE_STATE` is refused (USAGE) before the phone starts:
  install the units from the environment your agents use. `ADB_SERVER_SOCKET`,
  `ANDROID_ADB_SERVER_ADDRESS` and `ANDROID_ADB_SERVER_PORT`, which an adb client follows, do
  not choose the debug bridge's server for it: every adb command it runs is given the address of
  the server it runs, loopback port 5037, so none of them can send one to another server (an
  `adb` you run by hand does follow them). Start agents with an environment you set yourself,
  and never let content an agent reads set it.
- **Anyone who writes your unit files or their drop-ins.** The units and any drop-in that changes
  them (`systemctl --user cat orion-phone-<name>.service` lists both) are your own configuration,
  inside the same trust boundary as the environment. Before a locked phone starts, and for each
  running locked phone when units are installed, orion-phone checks the loaded unit properties its
  guarantees rest on (the session cap, randomized extra time, pending reload, `KillMode`, stop
  timeout, `SendSIGKILL`, `Restart` and the core limits, plus, when a lease starts a phone, that
  the unit is loaded and which state directory it watches) and refuses to start, or stops, a phone
  whose unit does not match; every other unit setting is trusted as you wrote it. A phone it stops
  when units are installed is stopped as a release stops it and is reported stopped only once adb
  and its console ports show it gone, because a unit that does not match may be one whose stop
  leaves the emulator running: if it cannot be shown gone, the install fails and says not to trust
  the phone until it is. It tries every running locked phone before it fails, and the failure names
  each phone that could not be shown gone. It waits for an action in flight on a phone for at most
  3 minutes, and does not stop the phone without the lock: a phone whose action lock stays busy
  that long, or cannot be opened, is named as not stopped, in words that claim nothing of its
  emulator, and is not counted among the phones stopped. The unit that runs the debug bridge's
  server, `orion-phone-adb-server.service`, is trusted the same way: orion-phone never reads it
  back, and what it runs is the `android.adb` of your config.
- **The operator restarting a locked phone's unit by hand while a command looks at it.** The
  unit's run is checked once, before the phone is read, and not again: a restart made by hand
  between that check and the first read has the reads judge the new run by the old run's marks.
  No agent can do it: a locked unit has no `[Install]` section, and its `Restart` must be `no`
  (the checks above refuse to start, or stop, a phone whose loaded unit says otherwise, since the
  manager would then start it again by itself), so only the tool's own cold boot (under the
  phone's action lock) or the operator starts one.
- **A locked unit changed and reloaded by hand while its phone runs.** A process keeps the core
  limits it started with. Installing the units judges a running locked phone first by the unit the
  manager has loaded, which is the one the phone started from, unless someone reloaded a changed
  unit by hand in between: then a phone started under other core limits keeps them until its
  session ends (the session cap bounds it), though its unit now says 1. After changing a locked
  phone's unit by hand, release the phone, so that its next session starts from the changed unit.
- **Two sessions that share a holder name.** A holder name is a session: processes that share
  a name share its lease and, on a locked phone, its unlock. The MCP server picks a random name
  per process. Give each CLI session its own name, and never set `ORION_PHONE_HOLDER` in a
  harness's global environment, or every session becomes the same holder.
- **Anything an agent does on an open phone while it holds the lease.** An agent that reads
  untrusted content and holds the lease can post or send as you. orion-phone records it; it
  does not prevent it. Put accounts that need a hard gate on a locked phone.
- **What a holder can read once a locked phone is unlocked.** The lock decides when an agent can
  act, not what it can read afterwards. A UI dump reads the text of every screen, including one
  that an app marks as secure and that a screenshot or a mirror shows black: an authenticator's
  codes, for one. Keep off a locked phone what you would not let the holder of an unlocked session
  read.
- **The host's clipboard.** By default the emulator shares your clipboard with every phone. That
  is a setting of the emulator for your OS user, not of one phone, so orion-phone cannot turn it
  off for a locked phone alone, and anything you copy on the host can be read by an agent holding
  any phone. To turn it off, set `clipboardSharing=false` in
  `~/.config/Android Open Source Project/Emulator.conf` while no phone runs.
- **An open phone's memory in a crash dump, once its limit is changed.** An open phone's unit sets
  `LimitCORE=1` too, but nothing checks it. A unit changed by hand, one written before 0.2.0 and
  never installed again, or an open phone running since before its unit was last installed (a
  process keeps the limits it started with) can leave a dump of the phone's memory, several
  gigabytes holding whatever its accounts had: systemd-coredump keeps such a dump, and WSL2's crash
  capture writes it to the Windows temp folder.

## Where the locked tier's protection comes from

The locked tier relies on Android and a trusted host. Android distinguishes
credential-encrypted storage from device-encrypted storage; device-encrypted data can be
available during Direct Boot. Independent verification that credential-encrypted storage stays
sealed before first unlock on the pinned image remains open as Q2 in the specification. The
lock-screen test proves the tool's observed refusals and transitions, not app-data sealing.
See Android's file-based encryption documentation:
https://source.android.com/docs/security/features/encryption/file-based

The tool requires the image and lock reports described below. A locked-to-unlocked transition
does not attest who or what supplied the credential. Leases and lock checks govern cooperative
callers of this tool; they do not constrain a process with local adb or host control. Google Play
system images refuse a root debug shell, but the host remains outside this boundary.

orion-phone checks the image instead of assuming it. A config with a locked phone must name a
Google Play system image (a tag that starts with `google_apis_playstore`). And each time a
locked phone is used after its boot (a lease retry or an action), orion-phone first reads
`ro.crypto.type` and `ro.debuggable` from the running phone, before it reads the lock state and
before any action reaches the phone. Anything but `file` and `0` shuts the phone down, frees its
lease, records the refusal in the audit trail, and returns USAGE naming both values (if the
phone cannot be shown to be off, the error is PHONE_DOWN, which names both values too). The
lease call that starts the boot returns before the phone can be asked, so the first read is the
next call. The phone reports these two values about itself: the check catches a wrong image,
not one built to misreport, and it does not look at whether `adb root` is refused. Look at the
image yourself once before you rely on it. With the locked phone running (its adb serial is
`emulator-` plus its `console_port`):

- `adb -s emulator-<console port> shell getprop ro.crypto.type` prints `file`.
- `adb -s emulator-<console port> root` is refused.

A locked phone is protected by its screen lock only once its operator has set one, and a new phone
boots with none at all: no lock screen, nothing to swipe, and so nothing for the lock state to say
but unlocked. So orion-phone also requires the screen lock itself. Each time a locked phone is
used after its boot, after the image check and before the lock state is read, it reads user 0's
`CredentialType` from `dumpsys lock_settings`. (User 0 is the phone's only user on the pinned
image, so reading only user 0 matters only on a headless-system-user image, or after a user switch
inside an unlocked session.) Only `PIN`, `PASSWORD` and `PATTERN` go on. `NONE` (a lock that needs
only a swipe included) is refused as NEEDS_OPERATOR with state `no-screen-lock`, which asks the
operator to set a PIN on the phone's own screen, and any other name, or a report that cannot be
read with certainty, is refused as state `unknown` and counts as no screen lock. A report cannot
be read with certainty when it has not exactly one `User State:` header at the start of a line,
or not exactly one `User 0` line in that section, or not exactly one `CredentialType:` line in
that user's block; when that line is not exactly `CredentialType: NAME` (one space, and a name of
capital letters and underscores, with nothing but blanks after it); or when it was cut at its
timeout. The lease is kept and no action reaches the phone either way. A boot is used only after
orion-phone has seen its lock screen showing over a screen lock (recorded for that boot) and a
later look finds the phone unlocked, still with one: that sighting is evidence of an unlock
transition, not proof of the operator's identity. A PIN set during a boot leaves the phone unlocked, because its operator is
signed in when they set it, with no lock screen for anyone to unlock, and whoever holds the lease
would be handed an unlocked phone. So a phone found unlocked in a boot never seen locked is not
used, and neither is a boot that was seen with none, even once a PIN is set and its lock screen
has shown. An action in such a boot is refused as `restart-needed`, and a lease that finds it
stops the phone and cold-boots it, so that its operator unlocks it from its lock screen once a
lease says `locked`. (Only a lock screen over a screen lock is a sighting, and only a report that
says there is none marks a boot as seen with none: no look at all, a boot still under way, a
report that cannot be read, a lock screen over no screen lock, and a phone found unlocked record
nothing. Unlocking before a look, a lease's or an action's, has said `locked` costs one restart.)
Like the image check, this reads what the phone reports about itself: it catches a phone with no
screen lock, and a boot that was not seen locked, not one built to say that it has a screen lock
or to show a lock screen.

orion-phone shuts the phone down on release, when its lease lapses, and at the session cap,
cold-boots every new session, never snapshots it, refuses to act on the lock screen, and never
holds the PIN. It reads the lock state only from the system's own `KeyguardController:` block of
the phone's activity dump, never from the rest of it, because that dump also prints text an app
controls (an intent's data, a task label), and a raw newline in such text starts a line of the dump
that can look like the system's. It takes the last `KeyguardController:` header, reads the lines
under it that are indented deeper (an empty line closes the block), and counts only whole
lock-state lines there: exactly `mKeyguardShowing=` and the per-display `KeyguardShowing=`, the two
keys the block prints, each with a value (never the middle of a line; any `true` wins). A whole
line with one of those two keys anywhere else in the dump is tampering. The dump prints the lock
state under other names too (Android 16's display policy prints `isKeyguardShowing=` earlier in the
same dump, before the block): those lines are not read at all, so they neither count nor make the
dump unknown. With no header, no counted line in the block, tampering, or a dump that dumpsys cut
at its timeout (it says so in the dump), the state is unknown, which counts as locked. So an app
can make a phone read as locked or unknown but not as unlocked, as long as the system's own block
is in the dump: a whole forged block, header included, would be read as written if the real one
were missing. The block's shape was confirmed on the pinned image (Android 16), with dumps taken
while the phone was unlocked and while it was locked: the header is indented two columns, its
fields four and the per-display line three, an empty line closes it, and it comes after the
activity records and after the display's own dump. A shape the reader misses (another image or
Android version) leaves every locked phone at `unknown`, which fails closed and never reads as
unlocked.

## Screen content is untrusted

Everything read from a phone (UI dumps, screenshots) can contain text written by anyone on
the internet. Agents should treat it as data, never as instructions.

orion-phone treats what systemd, adb, and the phone print the same way. It reads that text on
newlines and tabs only, because a value can hold a line or paragraph separator, or a next line
character, which systemd prints as it is and which common line splitting would take for the end of
a line: with it, a value in a unit's environment could forge a line of `systemctl show` output
(a cap of two hours for a unit that has none), and the serial of another adb device could forge a
row that names a phone that is not running. A property or an adb row that appears more than once
is refused, and a value a phone reports is not trimmed into a match (`file` followed by a
separator is not `file`). The same goes for what systemd and adb print: a line end is a newline
with at most one carriage return before it, and a second carriage return or a blank at either
end of a value or name is not trimmed away (`2h` followed by two carriage returns is not `2h`),
so the property or row is not recognized, which is refused or counts as not running. A carriage
return belongs to a line end only right before a newline: one at the very end of the output, with
no newline after it, stays in the last property or row, which is then not recognized either. That
output is captured as bytes and decoded without newline translation, so a carriage return reaches
the reader as one, and what a one-line command on the phone prints (a property value) loses one
newline or one CR LF and nothing else: `file` followed by a lone carriage return is not `file`.
Values written into a unit are checked the same way: a control character, a separator, or a
double quote is refused.

## What the audit trail holds

Every action by the lease holder writes an audit line to the state directory (owner-only
files), and every successful one a screenshot; a caller without the lease is refused before
anything is sent or recorded. The `ui` action also saves the screen's UI dump there.
Text an agent types is stored verbatim on success, so never have an agent type a password or
other credential: sign in to accounts by hand. A refused or failed action keeps only the length
of any typed text (so a PIN tried at a lock screen never reaches disk), even if the text did
reach the phone before the failure. A refusal that waits for the operator also records its state
(`locked`, `no-screen-lock`, `restart-needed`, and the others), and one that restarts, or refuses
for, a boot that cannot be used says why: `seen-with-none` or `never-seen-locked`.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a
vulnerability").
