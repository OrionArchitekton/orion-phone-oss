---
verified: 2026-10-17
review_after: 2026-11-17
topics: [release, artifacts, approval, packaging]
references:
  - .github/workflows/prepare-release.yml
  - .github/workflows/release.yml
  - .github/verify_release.py
  - .github/normalize_sdist.py
  - .github/normalize_wheel.py
  - .github/source-date-epoch.txt
  - bootstrap-requirements.txt
  - specs/release-approval-spec.md
---

# Prepare, review, and promote release artifacts

Preparation is reversible and does not publish. Source checks and a pinned build
produce a wheel, source distribution, and SHA256SUMS in the `reviewed-dist`
Actions artifact. A tag push or manual preparation never requests a PyPI token.
Bootstrap tools with `bootstrap-requirements.txt` before the hashed build tools.
Record the source commit and the SOURCE_DATE_EPOCH in `.github/source-date-epoch.txt`.
That tracked epoch controls wheel timestamps; `normalize_sdist.py` also removes
tar/gzip timestamp and builder-identity variation without changing file payloads.
Run that normalizer on the built source distribution before writing SHA256SUMS.
Run `normalize_wheel.py` on the wheel too. It preserves the reviewed v0.2.0
permission profile (0664 Python sources and WHEEL/RECORD, 0644 copied data),
independent of the builder's umask, without changing file contents.
The private build and a clean source-export build must produce matching hashes.
If a later CI preparation differs, stop: do not approve a new digest implicitly.

To reproduce an older approved package after a release-tooling fix, manually
dispatch preparation with `source_commit` set to its full 40-character commit
SHA. The commit must already be on main. Preparation records the source and
tooling commits separately, builds the selected source, and uses the current
wheel normalizer. Every source-check job checks out that same selected commit;
checks on the release-tooling commit do not substitute for source validation.
Compare the resulting manifest with the original approval;
do not retag, rebuild from a different source, or replace the approval digest.

## Review and validation

Download the prepared artifact, record its build run ID and immutable Actions artifact ID, and
calculate the SHA-256 of SHA256SUMS. Verify the selected version and both files:

```bash
python .github/verify_release.py reviewed MANIFEST_SHA256 VERSION
```

Install the exact wheel and source distribution into separate clean environments
outside the checkout, with the patched bootstrap. Run the CLI-boundary suite,
dependency audits, and neutral-app acceptance checks appropriate to any runtime
change. Review licenses, metadata, sanitization, and archive contents. Bind the
approval to the source commit, artifact ID, manifest digest, version, and upload
destinations. Attach these exact files to the GitHub release as well; never use a
different rebuild merely because its version matches.

## Publication, only after explicit approval

In the selected clean export repository, configure the PyPI Trusted Publisher for
`release.yml` and environment `pypi`. Require human approval on that environment
and restrict its allowed deployment branch to main. Verify these settings live
before enabling publication; this source file cannot create or enforce the
server-side reviewer setting. If required reviewers are unavailable, keep the
publisher disabled and complete a separately authorized manual upload of the
approved files. Never substitute an automatically approved environment.

Dispatch `release` from main with the build run ID, immutable artifact ID, approved manifest
digest, and version. Verification rejects any changed, missing, extra, linked,
or unsafe file. The publishing job downloads that same artifact again, rechecks
the approved manifest and checksums, and copies only the two packages to the
upload directory. It never rebuilds or installs repository dependencies.

## Monitoring and rollback

Inspect the workflow's approval target and upload receipt. Download the packages
from their public destinations and compare their hashes with the approved set
before announcing completion. A failed check stops publication. Cancel an
unapproved run, discard its candidate, and prepare a new review set rather than
editing an approved artifact. Package-index uploads cannot be overwritten: after
an erroneous approved upload, stop dependent announcements, assess yanking the
version with the owner, and prepare a corrected version. Do not delete or
republish silently. Private preparation never changes installed estate runtime.
