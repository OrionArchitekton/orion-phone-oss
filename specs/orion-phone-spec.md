# orion-phone specification

Status: accepted 2026-09-30. This is the living validation reference for orion-phone:
update it in the same change whenever behavior changes.

## 1. Purpose

orion-phone gives AI agent harnesses a second surface for real, signed-in work: Android
apps and mobile web on emulated phones, alongside desktop browser automation. It covers two
gaps:

- the desktop browser is busy (another agent holds it) or broken;
- the job needs a mobile-only app, SMS, or an authenticator.

It is a coordination layer, not a sandbox. Phones hold real accounts. The tool's job is to
make sure exactly one agent drives a phone at a time, that sensitive phones need a human to
unlock them, and that every action leaves a record.

## 2. Terms

One canonical term per concept.

| Term | Meaning | Replaces |
|---|---|---|
| phone | One Android emulator instance with persistent data, named in config (for example `a`, `b`). | device, emulator, AVD |
| tier | A phone's trust level: `open` or `locked`. | mode, class |
| open phone | Agents may drive it whenever they hold its lease. | everyday phone |
| locked phone | Never starts on its own; each session cold-boots and is usable only after the operator unlocks it by hand. | sensitive phone, vault phone |
| lease | Exclusive, time-limited right for one agent session to drive one phone. | lock, claim, reservation |
| holder | The agent session named on a lease. | owner |
| operator | The human who owns the phones and their accounts. | user, admin |
| action | One driving command: screenshot, UI dump, tap, type, swipe, key, open. | command, step |
| audit record | One line per action by the lease holder, with a screenshot for each successful one, kept outside the repository. | log entry |

## 3. Scope

In v1:

- A command-line tool, the single authority for leases and actions.
- An MCP server exposing the same actions as tools. Every tool call goes through the
  command-line tool, so leases are enforced in exactly one place.
- Setup that creates phones from a Google Play system image downloaded on the operator's own
  machine.
- Service definitions so an open phone can run in the background, a locked phone has a
  hard maximum session length, and the debug bridge's server runs as a service of its own.

Not in v1: iOS; cloud or remote device farms; network-exposed phones (phones listen on
localhost only); a record/replay macro language; any attempt to infer what an action means
(for example "this tap publishes a post").

## 4. Scenarios

Each scenario is a vertical slice: it runs end to end through the real command-line boundary
and is demoable on its own. Listed in build order.

### S1. Set up a phone and prove it healthy

The operator runs setup once. It creates the configured phones, starts the open phone, and
shows its window so the operator can sign in to accounts by hand.

Acceptance:

- Every phone cold-boots: no snapshot is ever saved or loaded, and setup and every launch delete
  any that exist. Rationale: loading a quick-boot snapshot restores the phone's disks as they were
  at its save, so an open phone started again after a crash came back without the sign-ins and
  installs made since, and the emulator cuts a save at a stop off after 20 seconds; a snapshot
  left on disk would roll its phone back if a launch by hand loaded it. An open phone whose
  snapshots cannot be deleted starts all the same, since it never loads one (a locked phone's
  stops: S4); a link in their place is removed, not what it points to.
- Every phone's service sets core limits of 1, so a crash of its emulator leaves no dump of the
  phone's memory, which holds the sessions of its accounts; only a locked phone's limits are
  checked (S4).
- `status` reports a phone healthy only with positive proof: the phone reports boot completed
  AND a fresh screenshot is non-blank (not a single uniform color). Running-but-not-booted,
  or a blank frame, is UNHEALTHY.
- A phone that is not running is PHONE_DOWN. `status` never reports healthy on a missing or
  failed signal.
- The debug bridge's server runs as a service of its own, so that nothing depends on the bridge
  starting a server of its own. Every adb command orion-phone runs goes through it except one:
  the command that tells an emulator to exit connects to the emulator's console itself, and is
  sent only when that console accepts a connection (with nothing listening there it could reach
  nothing, and where the host drops such a connection, as below, it would wait out its time limit
  for a phone that is down; a locked phone is down between sessions, so a cold boot of a phone
  that is down does not wait for it). The look at whether a phone's ports answer is a plain
  connection to the emulator as well, not an adb command. The bridge starts its own server only
  when its first connection to the server's port is refused. Where the host drops a connection
  to a port nobody listens on instead of refusing it (WSL2's mirrored networking) that refusal
  never comes: with no server running every adb command that goes through it waits out its time
  limit, and `status` reports PHONE_DOWN while the phone runs; the message of that timeout names
  the service and how to start it. A lease on a locked phone talks to the bridge before it
  starts the phone, so the server has to be running before the first lease, and a phone's own
  start cannot see to that. So the service starts at login, whatever
  the tiers of the phones: setup enables it and starts it, before the phones, and installing the
  units enables it too (the login link only: installing starts nothing). Starting it is not
  proof that it is up: the service counts as started the moment the bridge is spawned, not once
  it has bound its port, so with another bridge server holding the port the start succeeds, the
  bridge aborts a moment later, and the service stays failed while the other server goes on
  answering. So setup, after it starts the service and before it starts any open phone, waits
  until the service is active and its port accepts a connection and both have held for 2 seconds
  (longer than that abort takes), for at most 10 seconds in all (the operator can change both
  times through the environment, up to 30 and 120 seconds, as for the other time limits); a port
  that answers while the service is failed is not ready, and a failed service ends the wait at
  once. If the service is not ready in time, has failed, or cannot be looked at, setup fails
  with the code of a phone that is down, says that the units are written, names the service and
  the phones it stopped, starts no open phone, and says that if the service's journal reports
  the address in use, another bridge server holds the port: the operator stops that server,
  starts the service, and runs setup again. Installing the units starts nothing and has no such
  wait, and neither has a start at login (the service manager starting the service, or an open
  phone's unit starting it): a failure there shows as `status` reporting the phone down with the
  bridge's time-out message where no other server answers, and the operator's guide to a failed
  service covers the rest. After an upgrade, or an explicit stop, the operator starts it by
  hand. The service runs the server in the foreground on loopback port 5037 only (the address is
  given to the server, not left to the environment, so it holds whatever the environment says,
  and the server never listens on every interface). Every adb command orion-phone runs is given
  that address too, the command that tells an emulator to exit included, so nothing the
  environment says can send one to another server, or to an address where none is running (where
  the host drops a connection to a port nobody listens on, as above, that would wait out the
  time limit although the server is healthy). Any other bridge client reaches the server only on
  that default address, which is where a client goes when its environment names none. The
  service starts the server again whenever it ends, except after an abort: an abort leaves the
  service failed until it is started again, and the expected cause is another server already
  holding the port, which would otherwise have it loop and write a core dump each time; a plain
  start brings it back once that server is gone. Every phone's unit, open or locked, also wants
  it and starts after it: an open phone's unit starting, at login or by hand, starts a server
  that is not running. A locked phone's lease does not get the server that way, since its first
  adb command comes before its unit starts. So a stopped server is started when the user's
  service manager next starts (normally after a reboot or a restart of the WSL instance), by
  hand, or by an open phone's unit; stopping a phone does not stop it. Its name can never be a
  phone's unit name (a phone's name has no hyphen, the service's has one). The service is
  written beside the phones' units, before them, from the configured debug bridge path. A debug
  bridge path that the service manager would misread in a command line (whitespace, a quote, a
  backslash, `$`, `%` or a control character), or that is not valid UTF-8, is refused with USAGE
  naming `android.adb`, before any unit is written, as an interpreter path is.
- The tool never asks for or handles account credentials: the operator signs in by hand. Text an
  agent types is stored in the audit trail, so agents must never type a credential.
- The tool refuses to place config, state, or the phones' data directory (the AVD home) inside a git
  working tree, and refuses an AVD home that a variable gives as anything but an absolute path.
- Text that comes from systemd, adb, the phone, or the files the Android tools write is read on
  newlines and tabs only: a line or paragraph separator, or a next line character, is part of a
  value and never a line break, so no value can forge a line that changes what the tool decides
  (the cap of a unit, which phone adb lists as running). A systemd property, or an adb row for
  one phone, that appears more than once is refused for the same reason. What a phone reports is
  the text up to its line end, and nothing else is trimmed from it, so a value is not made to
  match by what is cut off it. A line end is a newline with at most one carriage return before
  it: a second carriage return, or a blank at either end of a systemd value or name, or of the
  state in an adb row, is part of the text and is not trimmed away. A carriage return is part of
  a line end only immediately before a newline, so one at the very end of the output, with no
  newline after it, stays in the last property or row, which is then not recognized (an output
  that stops short of its newline is not read as a whole line). The output of systemctl and
  adb is taken as bytes and decoded as UTF-8 with replacement, with no newline translation, so a
  carriage return reaches the reader as one (a translating capture would turn two of them before
  a newline into an empty line, and a lone one into a newline). A unit property that is not
  exactly as systemd prints it is unreadable, which is refused, and a phone whose adb row has
  such a state is listed and not running. What a one-line command on the phone prints (a
  property value) loses one terminal line end, a newline or a carriage return and a newline, and
  nothing else: a lone carriage return, a carriage return after the newline, or a doubled one
  stays in the value, which is then not what the check wants, so the image is refused and a boot
  is not taken for finished.

### S2. Lease and drive an open phone

An agent leases the phone, takes actions, and releases it.

Acceptance:

- Every action whose command line parses checks the lease first. A caller without it gets
  NOT_HOLDER at once (it never waits behind another agent's action) and nothing happens on the
  phone, even if its arguments are invalid. A holder's invalid action is refused as USAGE and
  audited. (A command line that does not parse at all is USAGE before anything else runs.)
- Actions on one phone run one at a time, so each audit screenshot shows the phone right after
  that action and no other. A lease attempt waits for the action in flight too: a lease never
  begins, and a takeover never happens, while the previous holder's action is still on the phone.
  (Its lease can lapse mid-action, across a host suspend for instance.) So does the shutdown of
  a locked phone: a phone is never shut down under an action in flight, by a release, by a new
  session, or by the lease watch of its unit (scenario S4). An attempt that finds
  another agent's unexpired lease does not wait: it is BUSY at once.
- Every successful action renews the lease and writes one audit record: time, phone, holder,
  action, arguments, and a screenshot taken after the action.
- `release` ends the lease. Releasing a lease the caller does not hold is NOT_HOLDER. A holder may
  release its own lease even after it expired, as long as no other agent has taken it over.
- Screenshots and UI dumps are written to the state directory; the command prints their
  paths. The UI dump is also available as compact text an agent can read directly. A UI dump
  over 4 MiB is refused (UNHEALTHY), and neither saved nor read past the limit.

### S3. Two agents want the same phone

Acceptance:

- A lease attempt on a phone with an unexpired lease held by someone else returns BUSY with
  the holder and expiry at once, even while an action is in flight, and does nothing on the phone.
- When two or more lease attempts start at the same instant, exactly one succeeds. (Race
  test: 8 concurrent claimers, 200 rounds, zero double grants.)
- A lease not renewed within its time-to-live (default 30 minutes) is expired. The next lease
  attempt takes it over, once any action the previous holder still has on the phone has finished,
  so a takeover never overlaps that action, and the takeover is audited with the previous holder.
  An attempt that waited checks that the phone is running after the wait, not before it.
- A damaged lease file is reported as USAGE naming the file and how to clear it. It is never
  treated as free (that could hand a phone to a second agent) or as BUSY (agents would retry forever).

### S4. Locked phone session

Acceptance:

- A locked phone never starts on its own. It runs only while the lease of the session that
  started it stands: waiting at its lock screen for the operator, or during a session the operator
  has unlocked. When that lease lapses without a release the phone shuts itself down (the lease
  watch, below); the session cap and the next lease shut it down too.
- A locked phone's unit watches the phone's lease. The unit runs the emulator as its child and,
  every watch interval (15 seconds by default, and never more than 300), reads the phone's
  lease. It stops the emulator when no lease is held on the phone, when the lease has expired,
  when the lease is bound to another boot of the unit, when the lease is bound to no boot and
  either was taken after this boot started or comes after the watch has seen a lease bound to this
  boot, and when the lease file is damaged or cannot be read; it keeps the emulator while the lease
  stands and is bound to this boot, or, while no lease has yet been seen bound to this boot, is
  bound to no boot and was taken no later than this boot started (the session whose start this is:
  its lease is written before its unit is started). A lease bound to no boot that is not that
  session's is a newer session's: a takeover writes its lease before it stops the boot it
  replaces, and if that command dies or hangs in between, the boot it replaces, possibly
  unlocked, is stopped by its own watch at the next look and does not run on until the new lease
  expires. Once the watch has seen a lease bound to its boot (even one that has since expired) it
  remembers it, and from then on a lease bound to no boot is a newer session's whatever time it
  says, since the session that bound its lease is the one whose boot this is. The time comparison
  is wall clock and matters only before that first binding: a clock stepped back between a
  lease's write and the start of its own boot makes the watch stop that boot (it fails closed),
  and a step back by more than the age of the boot, in the window between its start and its
  binding (a boot whose binding never comes stays in it), before a takeover writes its lease,
  would let that lease read as older than the boot, which is the one case left that a clock
  decides. A stop is SIGTERM to the emulator and,
  after a grace period (30 seconds), SIGKILL, and the unit then ends as an ordinary stop
  (inactive, not failed), so the next lease cold-boots the phone. A phone whose lease lapses is
  off within about the watch interval plus the grace, plus however long an action in flight on
  it takes to end: before the watch stops the emulator for a lapsed lease it takes the phone's
  action lock, the one that makes a release and a new session wait, trying for it every fifth of
  a second without ever blocking on it, for at most 180 seconds. Once it holds the lock it reads
  the lease again, and stops the emulator, keeping the lock until the emulator is gone, unless
  the lease stands again, in which case it carries on and lets the lock go. An action that
  outlasts the 180 seconds does not keep a lapsed phone running: the stop then goes ahead (it
  still reads the lease again first), and so it does where the lock cannot be opened at all.
  A stop of the unit (release, a new session, the session cap) is answered at once even while the
  watch waits for the action lock, since a release holds that lock as it stops the unit; it is
  passed on to the emulator the same way, and the emulator is never left running behind
  the unit: the unit gives a stop 90 seconds, which a grace for SIGTERM and one after SIGKILL
  fit in, and its stop kills everything the unit started, helper processes included. An emulator
  that exits on its own ends the unit with its exit status (128 plus the signal number when a
  signal ended it). Reading the lease never waits for the lease lock. An open phone is not
  watched: its emulator is the unit's own process.
- The watch reads the leases in the state directory its unit was installed with, so a lease
  attempt whose command uses another state directory is refused with USAGE before the phone
  starts, and so is one whose unit names no state directory or whose environment cannot be read:
  every boot such a command started would be stopped at the watch's first look, no lease being
  held in the unit's directory. The paths are compared resolved. A locked phone's unit is started
  by a lease only: running its `_run` entry point by hand is refused with USAGE pointing to the
  lease, while an open phone's refusal still points to its unit.
- A locked phone never starts without a display, since its operator could not unlock it.
- A lease attempt on a locked phone that is off boots it (always a cold boot, never from a
  snapshot) and returns NEEDS_OPERATOR with state `starting`, which does not ask for the unlock:
  the phone's image cannot be read before it has booted, and an unsafe image must never have its
  PIN typed. Later attempts return NEEDS_OPERATOR with state `booting` until the boot has
  finished and, once the image has been checked, with state `no-screen-lock` while the phone has
  no secure screen lock (below), and then with state `locked`, which asks the operator to unlock
  it (a boot that was seen with no screen lock is first started again: below). The lease is held
  for the requesting agent from the first attempt, so no other agent can step in after the
  operator unlocks.
- A cold boot leaves the phone's display asleep, so a lease attempt that reads the lock state of a
  phone with a secure screen lock first wakes the display, and the operator asked to unlock it
  sees its lock screen. The wake comes before the look that records the sighting: a wake cannot
  dismiss a lock screen over a screen lock, but nothing relies on that, since a lock screen that a
  wake dismissed would never be seen and the boot would never count as seen locked. Of the lease
  attempts and the actions, only a lease attempt wakes the phone: an action refused on a locked
  phone sends the phone no input at all (`status`, which proves a running phone healthy, wakes its
  display too, before it takes a frame). A wake that fails changes nothing the attempt decides.
- Every new session on a locked phone starts from a cold boot. If the phone is running when a new
  session begins (after another holder's lease expired, or after it was started outside the tool),
  it is shut down and booted again first, so an unlocked phone never passes to a new session.
- A locked phone runs only on an image that provides the locked tier's protections: file-based
  encryption and no root shell. When any phone is locked, the configuration must name a Google
  Play system image (a tag that starts with `google_apis_playstore`). Each time a locked phone is
  used after its boot, once the boot has finished and before its lock state is read and before
  any action is sent to it, the phone must report `ro.crypto.type` as `file` and `ro.debuggable`
  as `0`. Anything else shuts the phone down, frees its lease, records the refusal in the audit
  trail, and returns USAGE naming both values (PHONE_DOWN, still naming both, when the shutdown
  cannot be confirmed). What the phone reports about itself is all this checks.
- A locked phone is used only while it has a secure screen lock. A new phone boots with no screen
  lock at all (no lock screen, nothing to swipe), so its lock state would read as unlocked and
  agents could use it: the tier's protection would be missing, silently. So each time a locked
  phone is used after its boot, once the image has been checked and before the lock state is
  read, the credential type that the phone's lock settings report for user 0 must be `PIN`,
  `PASSWORD` or `PATTERN`. `NONE` is no screen lock (a lock that needs only a swipe included): it
  is refused with NEEDS_OPERATOR and state `no-screen-lock`, which asks the operator to set a PIN
  on the phone's own screen (Settings, Security, Screen lock) before anything else, and it is
  refused even while a lock screen shows, since the lock state is not read until a screen lock
  exists. Any other name, or a report that cannot be read with certainty, is refused with
  NEEDS_OPERATOR and state `unknown`, and the phone is treated as having no screen lock. The
  report is read as the lock state is, on newlines only and by whole lines, and only where it
  holds the answer: in its section of per-user state, in user 0's block there, with exactly one
  such section, one user 0 and one credential line (zero or several are unknown), a name of
  capital letters and underscores, and a report cut at its timeout unknown. The line that the
  storage part of the report prints for user 0 is not user 0's block, and another user's
  credential is never read. (User 0 is the phone's only user on the pinned image, so reading only
  user 0 matters only on an image whose user 0 is a headless system user, or after a user switch
  inside an unlocked session.) As for the lock screen, the lease is kept, every action is refused
  with no action reaching the phone, and the refusal is audited with its state. An open phone
  makes no claim of protection and is never asked.
- A boot is used only on positive evidence of a secure lock screen followed by an unlock.
  This transition does not prove who supplied the credential. The operator
  is signed in when they set the first PIN, so a PIN set during a boot leaves the phone unlocked,
  with no lock screen for anyone to unlock, and an agent holding the lease would be handed an
  unlocked phone. So a boot is used only after a look in that same boot has found the lock screen
  showing over a secure screen lock (the credential type is `PIN`, `PASSWORD` or `PATTERN`: a
  lock screen that needs only a swipe is not one), and a later look finds the phone unlocked,
  still with one. That sighting is recorded for the boot by the reply that says the phone is
  locked, whether a lease or an action got it. Nothing else is a sighting: no look at all, a boot
  still under way, a screen lock or a lock state that cannot be read, a lock screen over no
  screen lock, and a phone found unlocked record nothing.
- A boot that was never seen locked is not used when it is found unlocked with a secure screen
  lock, whatever else was seen of it. Nor is a boot that has been seen with no secure screen lock
  (a look that found `NONE`; an unreadable report is not that) once it has one, whatever is set
  afterwards and even if its lock screen has shown since. An action in such a boot is refused
  with NEEDS_OPERATOR and state `restart-needed`, whose message says that the boot was never seen
  at its lock screen, or that a screen lock was set after the boot was seen with none; it starts
  nothing. A lease stops the phone for certain and cold-boots it, through the same stop, check
  and start as a new session or a takeover (so a restart is refused, with the phone left off,
  when the loaded unit fails that check), and answers `starting` with the same reason; the
  operator then sees `booting` and `locked`, and unlocks it from its lock screen. The only cost
  is one more restart when the operator unlocks before a look, a lease's or an action's, has
  said `locked`, which the replies say not to do. A lease attempt that still finds no screen
  lock answers `no-screen-lock` and restarts nothing. Both marks (seen locked, seen with no
  screen lock) belong to the boot: a new boot, a new session and a takeover start without them,
  and a lease record from before they existed is read as having neither. The phone's run is
  checked once, before the looks: if the operator restarts a locked phone by hand between that
  check and the first read, the looks judge the new run by the old run's marks (no agent can do
  that: only the tool's own cold boot, under the action lock, or the operator starts a locked
  phone). The restart is audited with its reason and its cause, a stable short value for each of
  the two (`seen-with-none` and `never-seen-locked`); so is an action refused for it, which
  records the same cause, and so is a restart that fails, which records besides the state that
  sent the phone to be restarted (`restart-needed`). The reply carries the cause too.
- The operator unlocks it by typing the PIN into the phone window, once a look, a lease's or an
  action's, has said it is locked. The next lease attempt succeeds.
- The tool never receives, stores, or types the PIN. While the lock screen shows on a locked
  phone, every action returns NEEDS_OPERATOR, so the tool cannot be used to guess the PIN. The
  audit record of a refused action keeps only the length of any typed text, never the text, and
  names the state when the refusal is a wait for the operator.
- The lock state is read only from the system's own block of the phone's activity dump: the one
  that starts at a line holding `KeyguardController:` (the last such line is the header taken) and
  runs over the lines indented deeper than it, up to the first that is not (an empty line ends it).
  Inside that block only whole lines count: a line that starts, after blanks, with one of the two
  lock-state keys the block prints, `mKeyguardShowing` (its own line) and `KeyguardShowing` (its
  per-display line), and then `=true` or `=false`; any `true` line wins. The dump prints the lock
  state under other names elsewhere (the display policy of the pinned Android 16 image prints
  `isKeyguardShowing=` in the same dump, before the block): such a line is not read at all, so it
  neither counts nor makes the dump unknown, wherever it stands. The dump also prints text an app
  controls, in the activity records before the block and in the records that follow it, and a raw
  newline in such text starts a line of the dump. So the same words in the middle of a line, or
  after a line or paragraph separator, never count, and a lock-state line (one of the two keys, as
  above) anywhere outside the block is tampering: the state is unknown. No header, or no
  lock-state line in the block, is an unknown state too, as
  is a dump that the phone's dump tool cut at its timeout (the dump says so, and the tool still
  reports success). Unknown is treated as locked, so a forged `false` cannot read as an unlocked
  phone while the system's own block is in the dump.
- Releasing a locked phone shuts it down and confirms it is gone before the lease is freed. If it
  cannot confirm that, the release fails and the holder keeps the lease, so a failed shutdown
  never leaves an unlocked phone free for another agent.
- A locked phone has a hard maximum running time per boot (default 2 hours, counted from
  boot, so time spent waiting at the lock screen counts), enforced by the service manager
  rather than by the agent. After it, the phone is off and the next boot is locked.
- A new session never boots a locked phone from a unit that lacks that cap. After the previous
  phone is shut down and before the new one starts, the tool reads the unit as the service
  manager has it loaded. The phone is not started, the lease is freed, the refusal is recorded in
  the audit trail, and the result is USAGE telling the operator to fix the unit (install-units,
  run with the same environment as the command, rewrites it and reloads the manager), when the
  unit has no cap, has a cap other than the configured one, has extra run time past the cap (a
  randomized extra time other than zero, which lets a session outlast its cap), has a stop that
  would not end the phone in time (a kill mode other than the one that signals every process the
  unit started, control-group, so that stopping the unit could leave the emulator running; a
  stop time under two graces of the lease watch, 60 seconds, or none at all; or a SendSIGKILL
  other than yes, so that a stop that runs out of its time leaves running what it has not ended),
  would be started again by the manager on its own (a Restart other than no: the manager would
  bring back a locked phone that no lease asked for, and the units are written with no, which a
  drop-in can change), would let a crash of the emulator leave a dump of the phone's memory (core
  limits other than 1 and 1: the units are written with LimitCORE=1, a crash handler the kernel
  pipes dumps to takes one under any other soft limit, 0 included, and a hard limit above 1 would
  let the process raise its soft limit; a unit that names none has infinity and 0), has a reload
  pending (its file changed since the manager loaded it),
  watches another state directory than the command uses (see the lease watch), or reports
  anything that cannot be read with certainty. A unit that names none of the kill mode, the stop
  time, SendSIGKILL and Restart has the defaults, control-group, 90 seconds, yes and no, which
  pass (a unit that names no core limit fails, as above); where the manager's default stop time
  is shorter, such a unit is refused until install-units writes the stop time out. A manager that does not
  report the extra run time at all (systemd before 250) is such a case: a missing answer is never
  taken as zero. This keeps a phone switched from open to locked off its old unit, which has no
  cap, until install-units has rewritten it. If the manager cannot be asked at all, the phone is
  not started either.
- A locked phone whose unit is not installed (the manager has no unit by that name, as before a
  first install-units, or after the unit file was removed) is refused with USAGE telling the
  operator to run install-units, never with PHONE_DOWN naming a failed systemctl. There is no unit
  to stop, so the stop that opens every new session fails; the tool then asks the manager why, and
  a unit that it certainly does not have, with nothing running under it, gives that refusal. A
  stop that fails for any other reason is still PHONE_DOWN, and so is a stop whose cause cannot be
  looked up; a stop that did not answer in time is not looked into at all (a unit that is not
  installed makes the stop fail at once). With no unit to stop, the rest of every shutdown is still
  done, because an emulator started by hand can be running with no unit behind it, possibly
  unlocked: the emulator is told to exit (when its console accepts a connection), and adb and
  the phone's ports are looked at until the phone is shown gone. The refusal as not installed is
  made only once it is; a phone that is still there is PHONE_DOWN, naming what still answers,
  with the advice to run install-units added, and its lease is freed like that of any session
  that could not start. The same refusals answer a release (the holder keeps the lease, as for
  any release that could not shut the phone down). A unit the manager does not have but that
  still runs (a process that outlived its unit file) is stopped like any other, and then refused
  as not installed.
- A locked phone is started only at the beginning of a session, after the old phone is shut down
  for certain and the unit has been checked as above. A lease attempt for a session whose boot is
  already under way never starts anything: if the phone is not running when it looks (it is not
  listed yet, or its unit stopped since, by the session cap or a crash), the answer is
  NEEDS_OPERATOR with state `starting`, and the next attempt, finding no running boot of its own,
  begins a new session.
- Installing the units never leaves a locked phone running without its session cap. Before it
  writes any unit, install-units stops each locked phone that may be running from a unit whose
  loaded definition does not carry exactly the configured cap (the old open unit of a phone
  switched to locked, another cap, extra run time past the cap, a stop that would not end the
  phone in time as above, a restart as above, core limits other than 1 as above, which a phone
  started from a unit written before them has, or one that cannot be read), and reports the phones
  it stopped. A process keeps the core limits it started with, and before the writes the loaded
  unit is the one the phone was started from. A
  phone is stopped as a release stops it, and counts as stopped only once it is shown gone: the
  stop waits for an action in flight on the phone, the emulator is told to exit (when its
  console accepts a connection), and adb and the phone's ports are looked at until nothing of
  the phone answers, within the wait a release has.
  A phone stopped here may be one whose settings leave the emulator running after the manager
  reports the unit stopped, so that report is not the proof, and each is shown gone the same
  way. A pending reload alone does not stop a session at that point.
  But the reload that install-units then makes loads whatever is pending, a drop-in's session cap
  or extra run time, kill mode, stop time or SendSIGKILL included, into a running unit as well, so
  after the reload it reads every locked phone that is still running again and stops, and
  reports, any
  whose loaded definition is not exactly the configured cap with no extra run time, a stop that
  ends the phone in time, no restart, core limits of 1, and no reload pending. A running session
  that carries the cap before and after, and open phones and units that are not running, are
  untouched. A locked phone whose unit the manager has not found, with nothing running under it
  (a first install), is skipped: there is nothing to stop, the manager answers for such a name
  without an error, and the install writes the unit and reloads. A unit that is not found and
  still runs is not skipped for that: it is read like any running unit. If a phone cannot be
  stopped, or cannot be shown gone, installation refuses with USAGE, rather than report
  success: before the writes it writes nothing, and after the reload the message says that the
  units are already written and loaded. Every locked phone is looked at in each look, whatever
  happens to the others: a phone that cannot be shown gone, or whose unit cannot be read, does
  not keep a later phone from being read and stopped, which matters most after the reload, since
  the reload may have uncapped that one too. The refusal comes once the last phone has been
  looked at. It names every phone that could not be shown gone, with its unit and what still
  answers for it, tells the operator not to trust each until it is gone, and lists the phones
  stopped; a unit that could not be read is named after them (when nothing but reads failed, the
  first read's own error stands, and after the reload it says too that the units are written,
  what to run again, and the phones stopped, as every failure after the writes does: the sync of
  the unit directory, the adb server's login link, the reload, and the look after it). A stop that
  cannot begin is not a stop that ran, and is said
  in other words: the stop is made under the phone's action lock, which install-units tries
  for, as the lease watch does and for as long as it does (3 minutes, so that an action that
  never ends does not hold the install, and so that an interrupt is answered), and the phone is
  never stopped without it. When the lock stays busy that long, or cannot be opened (a directory
  in its place, no directory to hold it, a state directory that cannot be used), the refusal
  says that the phone was not stopped and why, names the lock file, claims nothing of its
  emulator (it was not told to exit, and nothing is said of what answers), tells the operator not
  to trust the phone until it is stopped, and does not count it among the phones stopped. A
  reload that itself fails is not
  taken for one that did not happen (the manager may go on to finish it, or have finished it
  before the answer was lost): the same look is made anyway, stopping any running locked phone
  not confirmed to be exactly the configured cap with no extra run time and no reload pending,
  and the install fails with the reload's error, the phones it stopped, and the advice to run
  install-units again once the manager is responsive. If that look fails too, including a phone
  that could not be shown gone or was never stopped, the error stays the reload's, code and
  message, names each such phone with the same advice, carries in its data what it says in its
  text (the phones not stopped or not shown gone, those never stopped, the units that could not
  be read), and lists what the look managed together with the phones stopped before the reload.
  Every failure after a phone was stopped lists the phones already stopped and shown gone. A
  session ended this way is an ordinary shutdown: its holder leases again.
- A run of install-units or setup that succeeds makes no login link after its last reload of
  the manager. A link made without a reload leaves every unit marked as needing one until the
  next reload (it is the manager's unit-file state that is outdated, so every unit says it, not
  only the one linked), and a lease on a locked phone is refused for a unit that needs one. So
  the install makes the adb server's link before its one reload, and looks at the running
  locked phones after it, as after any reload. Setup, which links the open phones after the
  install's reload, reloads once more and looks again, and only then starts the adb server and,
  once it is up (see the service above), the open phones: the starts change no link, the second
  look is never skipped, and the server is started before any phone, whatever the tiers. A lease
  on a locked phone right after either command has succeeded is therefore not refused for a
  reload that is owed, so long as that reload loaded what is on disk (one that loaded nothing
  leaves it owed, and the lease is then refused, which is the safe answer). A failure of any
  step of setup after the install (a link, the reload, the look, a start, the wait for the
  service) says that the units are written, tells the operator to run setup again, and names the
  phones that were stopped, in its message and in its data, as the install's own failures after
  the writes do; the install's link, made before its reload, says besides that the units are not
  loaded yet.
- Snapshots are never saved or loaded for a locked phone (as for every phone: S1), and setup
  deletes any that exist. A launch or setup that cannot delete one stops with USAGE, naming it.
  Rationale: a snapshot taken while unlocked would restore an unlocked phone.

### S5. MCP parity

Acceptance:

- Every agent command (status, lease, release, and the seven actions) has an MCP tool with the
  same arguments. Results carry the same data, except that `shot` returns the screenshot image
  and `ui` returns the compact UI text.
- Each MCP server process is exactly one holder, named by the server itself (never taken from
  the environment), so two agent sessions never share a lease.
- MCP tools call the command-line tool. Lease errors surface as tool errors carrying the
  same result codes.
- No MCP tool reaches a phone except through the command-line tool.

## 5. Result codes

Every command supports JSON output that carries the code name, so agents never parse prose.

| Exit | Name | Meaning |
|---|---|---|
| 0 | OK | Done. |
| 2 | USAGE | Bad arguments or config. |
| 3 | BUSY | Another holder has an unexpired lease. |
| 4 | NOT_HOLDER | Caller does not hold the lease (never held, released, or expired); on a locked phone, also when its session ended: lease again. |
| 5 | NEEDS_OPERATOR | A locked phone is starting, still booting, has no screen lock, has a boot that cannot be used (found unlocked without ever being seen at its lock screen, or seen with no screen lock and given one since), is showing its lock screen, or has a lock state or screen lock that cannot be read (the result's `state`: `starting`, `booting`, `no-screen-lock`, `restart-needed`, `locked`, `unknown`). `locked` asks the operator to unlock it, and `no-screen-lock` to set a PIN; `restart-needed` says to lease again, which restarts the phone. |
| 6 | PHONE_DOWN | Phone is not running. |
| 7 | UNHEALTHY | Phone is running but failed the positive health proof. |
| 1 | INTERNAL | An unexpected error inside orion-phone; the traceback goes to stderr. Never a success. |

## 6. Security model

Stated plainly, and repeated in SECURITY.md.

Defended:

- Collisions between agents (leases).
- A confused or manipulated agent using this tool to act on a locked phone without the
  operator (a required screen lock, lock screen, NEEDS_OPERATOR refusal, a cold boot for every
  new session, shutdown on release, session cap).
- A locked phone's memory leaving it through a crash: its unit keeps the emulator from leaving a
  crash dump (core limits of 1, checked before every start and at install; every open phone's
  unit sets them too, unchecked), and every phone's emulator runs with crash reporting disabled,
  so a phone starting again after a crash never waits on the emulator's report dialog.
- Personal data reaching the public repository (section 8).

Not defended:

- A deliberately malicious process on the operator's machine, or on another operating system
  that shares its loopback. Running as the operator's OS user it can reach the Android debug
  bridge directly, bypass leases, use the emulator console, snapshot a locked phone while it is
  unlocked, or read input events while the operator types the PIN. The debug bridge's server,
  which orion-phone runs on loopback port 5037, takes commands without authentication from any
  process that can connect to it, under any OS user, and where loopback is shared with another
  operating system (WSL2's mirrored networking) that system's processes can reach it, and the
  emulators' own ports, too. Leases are coordination, not a security boundary.
- Anything an agent does on an open phone while it holds the lease. Open phones hold real
  accounts: an agent that reads untrusted content and holds the lease can post or send as the
  operator. The tool records it; it does not prevent it. Operators who need a hard gate on
  publishing should put publishing accounts on a locked phone.

The locked tier relies on Android and a trusted host. Android distinguishes credential-encrypted
storage from device-encrypted storage, which can be available during Direct Boot. Independent
verification of credential-encrypted storage before first unlock on the pinned image remains
open as Q2; the lock-screen acceptance test does not prove storage sealing. The secure lock
screen gates tool actions once a secure screen lock is set, and Google Play images refuse a
root debug shell. A local process with host or adb control remains outside the tool's boundary.
The tool's part is to refuse a locked phone whose image does not report file-based
encryption and a build that is not debuggable, to refuse a locked phone that has no screen
lock, to shut the phone down on release and at the session cap, cold-boot every new session,
never snapshot it, refuse to act on the lock screen, and never hold the PIN.

## 7. Configuration and state

Both live outside the repository.

- Config: `~/.config/orion-phone/config.toml` (phones, tiers, system image, ports,
  time-to-live values, session cap). The repository ships only `config.example.toml`, and the
  installed package carries the same file, which `init` writes where the tool reads its config
  (the config path above, or `ORION_PHONE_CONFIG`), so an install with no checkout has one to edit.
  `init` never replaces anything already at that path: an existing file, or a link even when it
  points nowhere, is refused with USAGE naming the path, and nothing is written. The file it writes
  is readable by its owner only, and every directory it makes for it is its owner's only, because
  which phones are locked lives in the config. The config appears complete or not at all: a write
  cut short (a full disk, an interruption) is USAGE and leaves nothing at the path, so `init` can
  simply be run again. A path inside a git working tree (links followed) is
  refused with USAGE before anything is created, as every later command would refuse that config;
  with no config at all, a command's USAGE says to run `init`.
- State: `~/.local/state/orion-phone/` (leases, audit records, screenshots, UI dumps),
  created with owner-only permissions.

Phone data (each phone's disk images and snapshots) lives in one directory, the AVD home, which
the tool works out the way the Android tools do: the environment variable `ANDROID_AVD_HOME` when
it is set, else the `avd` directory inside `ANDROID_USER_HOME` when that is set, else
`~/.android/avd`. The tool works it out in one place, and setup, unit installation, and every
phone launch go through that. A variable that gives the home must hold an absolute path (an empty
value, a relative path, or one that starts with `~` is refused). However it is found, the path is
resolved before use, must lie outside any git working tree (links are followed first), and must
not hold a double quote, a control character, or a line or paragraph separator, which a unit file
cannot carry, or `systemctl show` output could be split on. Otherwise the
command stops with USAGE naming the variable (`ANDROID_AVD_HOME`, as the way out, when the default
is what fails). A variable that does not decide is not looked at. Setup makes the directory when
it is missing (owner-only when new) and runs avdmanager with the resolved path as
`ANDROID_AVD_HOME`, because the real tool ignores one that is not an existing directory and puts
the phone in its own default home. A phone's unit always names the resolved home, so it starts
its phone from the directory setup used whatever its own environment holds, and a launch tells
the emulator the same; a launch refuses a home that is not an existing directory, since the
Android tools would look for the phone somewhere else. Commands that never touch a phone's data
(status, lease, release, the actions) do not read these variables, so a bad value there cannot
stop them.

## 8. Repository hygiene (public-ready from the first commit)

- MIT license.
- Test data is synthetic only: a fake debug bridge replays hand-written fixtures. No
  screenshot, UI dump, or audit record from a real phone is ever committed.
- `.gitignore` excludes images, UI dumps, and audit and state directories, except explicitly
  listed synthetic fixtures and documentation images.
- CI runs a secret scanner on every push and pull request. Third-party CI actions are pinned
  to commit SHAs.
- Documentation images come from a clean demo phone signed in to no personal accounts.
- System images are never redistributed. Setup downloads them with the operator's own SDK
  tools. Accepting Google's Android SDK License is the operator's own step before setup: current
  SDK tools show the terms once and do not ask again, so the tool cannot verify acceptance and
  does not claim to.
- Before the repository is made public: a full-history scan for secrets and for the
  operator's personal identifiers returns zero findings, and the review is recorded.

## 9. Test seams

Decision: one primary seam, the command-line boundary (exit code plus JSON output), with a
fake debug bridge on PATH. S1 through S5 are all exercised there; MCP parity tests call MCP
tools over the same fake bridge.

One secondary seam: a real-emulator smoke test (boot, lease, screenshot, release) run locally
by the operator. Not in CI for v1.

## 10. Constraints and dependencies

- Linux host with systemd user services and hardware virtualization (KVM) available to the
  user. WSL2 qualifies when nested virtualization is enabled.
- Android SDK command-line tools, emulator, and platform tools from Google's official
  distribution.
- Python 3.11 or later.
- MCP server built on the official Model Context Protocol Python SDK. Confirm the official
  package and source before adding it.
- No other runtime dependency without a recorded reason.

## 11. Open questions (resolve during planning)

- Q1. Resolved: `system-images;android-36;google_apis_playstore;x86_64`. The 16 KB page
  variants are avoided for app compatibility.
- Q2. Enforced at runtime, and confirmed on the real image except for one point. The tool requires
  a Google Play image in the configuration and refuses a locked phone unless it reports file-based
  encryption (`ro.crypto.type` is `file`) and a build that is not debuggable (`ro.debuggable` is
  `0`). The pinned Android 16 image reports `file` and `0`, so it passes that check, and its debug
  bridge refuses a root shell (`adbd cannot run as root in production builds`), which section 6
  depends on. Still open: independent verification that credential-encrypted storage stays
  sealed before the first unlock on this image. Android's device-encrypted storage can be
  available during Direct Boot. The successful synthetic locked-phone test demonstrates the
  tool's refusals and observed unlock transition; it is not a storage-encryption test or proof
  of who supplied the credential. Q2 remains a disclosed release limitation.
- Q3. Deferred to v2: switching the debug bridge off on a locked phone between sessions, as a
  second layer.
- Q4. Does the phone window run acceptably under the host's display stack with GPU
  acceleration, or does it need software rendering?
- Q5. Resolved: confirmed on the pinned Android 16 image, with dumps of the phone's activity state
  taken while it was unlocked and while it was locked (with the screen off and with it on). The
  lock state is read only from the system's own `KeyguardController:` block of that dump (scenario
  S4), and the image prints that header on a line of its own, indented two columns, with its
  lock-state lines under it (the block's own line four columns deep, the per-display line three)
  and an empty line closing the block, after the activity records that open the dump and after the
  display's own dump. The display policy prints the lock state under another name,
  `isKeyguardShowing=`, earlier in the same dump. The reader does not read that name at all: a
  reader that counted it would take every real dump for tampered with, and no locked lease could
  ever leave NEEDS_OPERATOR with state `unknown`. A shape the reader misses still fails closed
  (every locked lease stays at `unknown`, never unlocked), and a whole forged block, header
  included, would still be read as written if the real one were missing from the dump.
