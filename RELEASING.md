# Version releases

Release Please v5 is pinned to 45996ed1f6d02564a971a2fa1b5860e934307cf7.
Version PR creation and updates mint a short-lived installation token from the
minimal-permission organization GitHub App (`petauron-nova`: `contents: write`
and `pull-requests: write` via `RELEASE_APP_CLIENT_ID` and
`RELEASE_APP_PRIVATE_KEY`) so normal `pull_request` CI and CodeQL checks run
automatically without a personal PAT. Actual release creation and publication
continue using the built-in `GITHUB_TOKEN`. Existing branch protection and
required CI remain mandatory.

After a push to main, the release workflow waits (at most 30 minutes) for the
exact commit's normal CI to succeed. It then creates the release, finishes
publication, and maintains the next version PR. Review and merge that PR rather
than bumping versions or creating tags manually.

`GITHUB_TOKEN`-created tags do not start another release workflow; publication
runs in the same release workflow from the checked `main` commit.

Publication runs in the same release.yml, gated by release_created, using the
Release Please output tag and immutable source SHA. The source must equal the
workflow commit. Builds and provenance checks remain required; only successful
publication makes the draft public. Tags are not a second release entry point.
On a build failure, use GitHub's Re-run failed jobs for that run; do not move tags
or overwrite published releases. A stale run skips preparation when main moved.

## Recovery after a release-workflow repair

If the failed run cannot use a reviewed workflow repair by rerunning, merge that
repair through a PR and dispatch `release.yml` on protected `main` with the
original `failed_run_id`. The workflow requires real CI on its own exact commit,
then derives the version and original source SHA from the failed main push run.
It admits only that run's existing, unpublished draft pinned to the same SHA,
with successful source preparation, Application checks and the three actual
CodeQL analysis jobs on main (actions, Python and JavaScript/TypeScript), not
the PR-only aggregate CodeQL status. The usual
source/version/manifest checks, native image builds, digest assembly and draft
publication steps still run. No manually supplied tag, version or source SHA is
accepted; a missing draft, changed identity or already published release fails
closed. The original release remains unchanged until all publication steps pass.
Normal main pushes leave any pending draft untouched and skip new release/version
preparation until the original publication is recovered.

GitHub exposes draft releases only to tokens with push access. The source
verification job therefore needs `contents: write` solely to read the draft;
it does not update releases or refs. No PAT or separate release secret is used.


Local workflow edits alone do not activate releases. Push/review the changes,
then verify the first version PR CI and release run before calling the migration
complete. No local builds or tests are implied by this configuration change.

Architecture digests are staged in the GitHub draft release, not Actions
artifacts. Docker build record uploads are disabled; build caches are retained.
