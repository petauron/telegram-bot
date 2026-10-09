# Version releases

Release Please v5 is pinned to 45996ed1f6d02564a971a2fa1b5860e934307cf7.
Like Vastora, it uses only the built-in GITHUB_TOKEN; no extra release secret or
personal token is needed. Repository settings must allow Actions to create pull
requests. Existing branch protection and required CI remain mandatory.

After a push to main, the release workflow waits (at most 30 minutes) for the
exact commit's normal CI to succeed. It then creates the release, finishes
publication, and maintains the next version PR. Review and merge that PR rather
than bumping versions or creating tags manually.

GITHUB_TOKEN-created tags do not start another release workflow, and generated
PR workflows can require manual approval. The release workflow explicitly
dispatches ci.yml on the version PR branch to run the existing checks.

Publication runs in the same release.yml, gated by release_created, using the
Release Please output tag and immutable source SHA. The source must equal the
workflow commit. Builds and provenance checks remain required; only successful
publication makes the draft public. Tags are not a second release entry point.
On a build failure, use GitHub's Re-run failed jobs for that run; do not move tags
or overwrite published releases. A stale run skips preparation when main moved.


Local workflow edits alone do not activate releases. Push/review the changes,
then verify the first version PR CI and release run before calling the migration
complete. No local builds or tests are implied by this configuration change.
