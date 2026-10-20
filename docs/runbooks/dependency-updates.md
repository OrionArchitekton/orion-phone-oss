---
verified: 2026-10-19
review_after: 2027-01-19
topics: [dependencies, ci]
references:
  - .github/dependabot.yml
  - .github/workflows/ci.yml
---

# Dependency updates

Dependabot runs weekly. Existing GitHub Actions version updates arrive in one group, including major versions; review runner and input compatibility before merging. Where pip is configured, build tools are grouped across versions, routine minor and patch updates are grouped, other major updates remain separate, and security updates have their own group. Grouping grants no merge or publication authority.

Prepare each batch from current main. Combine overlapping manifest edits, retain action commit pins, and regenerate changed requirement hashes from the intended package files. Keep build backend requirements and every corresponding hashed lock file consistent. Never remove a hash or relax an artifact check to make an update pass.

Validation: Install the hashed bootstrap requirements with --require-hashes --force-reinstall, install .[dev], run python -m pytest -q, and build with the hashed .github/build-requirements.txt tools. Check the diff and secret scan, then require all candidate-head CI checks and review findings to be satisfied.

After an authorized merge, verify main contains the tested batch and its CI passes; close only the superseded bot PRs. Inspect the next Dependabot run for grouped PRs. Package publication and runtime upgrades are separate actions.

Rollback: prepare a PR reverting the batch commit, rerun the same validation, and obtain merge authority. Keep published release artifacts immutable.
