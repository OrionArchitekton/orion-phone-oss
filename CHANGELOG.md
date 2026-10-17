# Changelog

## 0.2.0 (unreleased)

The first public release.

- `orion-phone init` writes the example config where orion-phone reads its config, for an install
  from a package, which has no checkout to copy it from. It never replaces an existing config, and
  refuses a path inside a git working tree. A missing config's error now says to run it.
- Every phone's emulator starts with crash reports disabled, so a phone starting again after a
  crash no longer waits behind the emulator's report dialog.
- Every phone's unit sets `LimitCORE=1`, so a crash of its emulator leaves no dump of the phone's
  memory (an open phone's dump ran to 9 GB, and its restart waited for it). A locked phone's limits
  are checked like the session cap: a lease refuses a unit whose loaded core limits are not 1, and
  install-units stops a locked phone running without them.
- Every phone cold-boots. An open phone no longer saves or loads quick-boot snapshots, which hold
  the phone's disks as well as its memory, so one started again after a crash no longer comes back
  without the sign-ins and installs made since its last save; a launch deletes any left over. Both
  changes reach an open phone at its next start: restart open phones once after upgrading.
- `ui` tries a dump that wrote nothing once more, a second later (an app that is still starting
  can leave Android with no window to dump), and otherwise says why in uiautomator's own words.
- A lease that finds a locked phone's lock screen wakes its display, so its owner is not asked to
  unlock a dark screen.
- Docs: viewing phones with scrcpy, what a holder can read once a locked phone is unlocked, the
  emulator's clipboard sharing (and the WSLg crash it can set off), crash dumps, and `ui` on a
  screen that changes every second.
- Releases build with tools pinned by hash and without build isolation, and CI builds the package
  the same way on every pull request.

## 0.1.0

Private releases: leases, the locked tier, the audit trail, the CLI and the MCP server.
