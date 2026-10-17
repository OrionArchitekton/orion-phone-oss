# Approval of release artifacts

Release preparation and publication are separate actions. Preparing or tagging a
candidate never uploads it to a package index. The maintainer reviews the actual
wheel, source distribution, and their SHA-256 manifest before requesting upload.

## Scenarios and acceptance

1. A prepared candidate passes the source checks and produces an immutable build
   artifact containing exactly one versioned wheel, one source distribution, and
   their manifest. Preparation has no package-publishing credential.
   A recorded build epoch and normalized source archive remove builder identity
   and timestamp differences; a clean export rebuild must match reviewed bytes.
2. A maintainer selects an artifact by its immutable identifier and supplies the
   reviewed manifest digest and version. Verification accepts only those exact
   files and bytes. A changed file, missing file, extra file, symlink, invalid
   version, or unsafe manifest name is refused before publication.
3. Publication runs only when explicitly requested from the default branch and
   after the separately configured publishing-environment approval. It promotes
   the selected files without rebuilding. No repository code or dependency
   installation runs in the job that can request a publishing credential.

## Test seams

The primary seam is the artifact verifier's command-line interface with synthetic
files in a temporary directory. Exercise accepted and refused candidates there.
Workflow validation checks the preparation/promotion wiring without uploading.
An actual package-index upload remains outside private validation and requires
the owner's separate publication approval and configured publisher/environment.
