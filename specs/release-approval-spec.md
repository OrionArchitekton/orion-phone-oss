# Approval of release artifacts

Release preparation and publication are separate actions. Preparing or tagging a
candidate never uploads it to a package index. The maintainer reviews the actual
wheel, source distribution, and their SHA-256 manifest before requesting upload.

## Scenarios and acceptance

1. A prepared candidate passes the source checks and produces an immutable build
   artifact containing exactly one versioned wheel, one source distribution, and
   their manifest. Preparation has no package-publishing credential.
   A recorded build epoch and normalized archives remove builder identity,
   timestamp, and host permission differences; a clean export rebuild must match
   reviewed bytes. Preparation may select a previously reviewed source commit
   already on the default branch, retaining the current release tooling. The
   source and tooling commits are recorded separately. Reproduction never changes
   the reviewed package contents or implicitly approves a new manifest.
   All source checks run against the same selected commit that is packaged;
   passing checks for another commit never qualify a candidate for upload.
   Credential-free preparation may use automation from a review branch. Tagged
   preparation packages that tag's commit; it cannot select a different source.
   Archive normalization uses the recorded automation revision. Build dependency
   pins and the recorded build epoch belong to the selected package source, so
   historical reproduction preserves its approved build inputs.
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
Archive normalization uses its command-line interface with synthetic archives,
checking unchanged payloads and identical bytes across host permission differences.
Workflow validation checks the preparation/promotion wiring without uploading.
An actual package-index upload remains outside private validation and requires
the owner's separate publication approval and configured publisher/environment.
