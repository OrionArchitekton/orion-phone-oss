# Contributing

Thanks for looking. orion-phone is small, and its locked tier is security code, so changes come
with tests that show what they promise.

## Where to start

The roadmap in the README lists what is wanted most: real Android devices over adb, iPhone, macOS
and native Windows hosts, and an SMS bridge. Each has an issue labeled `help wanted`; say there
what you plan before a large change.

## Run the tests

The suite needs no emulator: fakes stand in for adb, systemd and the Android tools.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes --force-reinstall -r bootstrap-requirements.txt
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # about 2,500 tests, several minutes
.venv/bin/python -m compileall -q src tests
git grep -n -I -E '[[:blank:]]+$'      # must print nothing
```

CI runs the same three checks and a secret scan on every pull request.

## How changes are made here

- Write the failing test first, then the code. A test that would still pass with the change
  reverted proves nothing: break the code on purpose once and watch the test fail.
- Match the code around you: its naming, its comments, and its habit of saying why.
- Behavior that users or agents rely on (commands, exit codes, the MCP tools, the locked tier's
  refusals) is described in `specs/orion-phone-spec.md`. Change the spec in the same pull request.
- A change to the locked tier must keep every refusal it already makes. Say in the pull request
  which ones your tests cover.
- Never commit screenshots, UI dumps, audit records, or anything from a real phone. Tests use
  synthetic data only, and a hygiene test rejects those files.

## Security issues

Do not open a public issue. Use GitHub's private vulnerability reporting (Security tab, "Report a
vulnerability"), as [SECURITY.md](SECURITY.md) says.
