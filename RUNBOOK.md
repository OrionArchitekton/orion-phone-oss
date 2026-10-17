# Repository operations

Changes use pull requests with successful CI, secret scanning, resolved conversations,
and an approving human review after the final push. Do not force-push or bypass
protection. The first reviewed source snapshot is tagged separately from repository
bootstrap checks. Release downloads retain their approved bytes and checksums.

Validate the repository commands in its README before opening a PR. Dependency
updates use Dependabot and the PR dependency-review check. A failed or missing
required check blocks merge; it is not permission to retry an outward action.

Publications require the owner's explicit authorization. For a correction, prepare
and review a new version. Do not overwrite an approved release asset. No repository
operation installs or updates an existing user's runtime automatically.

Package publication follows `docs/runbooks/release-artifacts.md`.
