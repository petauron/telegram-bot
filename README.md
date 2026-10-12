# Telegram Bot

Petauron application for collecting Telegram and other feeds, classifying
messages, and managing notification delivery through a web dashboard.

## Deployment

Images are built only by GitHub Actions for Linux AMD64 and ARM64. Use the exact
`ghcr.io/petauron/telegram-bot@sha256:...` reference from `image.txt` in a release.
Image visibility is managed separately from repository visibility. Private images
require a read-only registry credential in Vastora; never put tokens in a recipe.

The Vastora package consists of a collector and web container sharing the
`database` volume. The collector also owns a `session` volume. The web service
listens on port 8080 and exposes `/healthz`. Configure an access entry in Vastora.

Sensitive configuration supports explicit `TG_API_HASH_FILE`, `TG_PHONE_FILE`,
`PUSH_BOT_TOKEN_FILE` and `WEB_PASSWORD_FILE` credential paths. Do not provide
both a value and its file. Regular environment settings remain available for
standalone Compose installations.

Collection starts disabled in the managed recipe. For a new account, run
`python -m app.login` in the collector container with an interactive terminal,
then enable collection through Vastora configuration. A migrated instance reuses
its existing session and does not require a new Telegram login.

## Existing-instance cutover

1. Install the managed application with `collector_enabled=false`.
2. Record the source/target volume identities. Stop the old collector and web,
   and stop the new application before transferring data.
3. Transfer the entire database directory and session directory, including all
   SQLite companion files, while both copies are stopped. Preserve UID/GID 10001.
   Do not send session files, database contents or credentials to GitHub.
4. Start the managed web application with collection still disabled. Verify
   existing configuration and history; then enable the managed collector.
5. Switch the access entry after confirming health. Keep the old collector
   stopped; remove its deployment only after the managed instance is accepted.

Do not run two collectors on the same session or delete the source data before
acceptance. The current production instance is not automatically changed by a
Git push, image release or catalog publication.

## Development and release

Production data and secrets must remain outside this repository. CI runs Python and frontend tests and builds web assets.
Release Please maintains the version PR. After its CI succeeds and it is merged,
GitHub Actions builds the two native images and publishes their immutable
multiarchitecture index from that checked main commit. See `RELEASING.md`.

## Configuration and privacy

Service URLs using `example.com` are placeholders. Configure your own model and
notification endpoints before enabling either integration. Existing saved settings
are retained. Never commit `.env` files, Telegram session files, databases, backups,
logs, exported messages or access tokens. Use synthetic data when reporting bugs.

## Standalone installation

1. Select a release and copy the immutable image reference from `image.txt`.
2. Copy `.env.example` to `.env`, set `TELEGRAM_BOT_IMAGE`, Telegram API settings,
   and a unique Web password. Keep `COLLECTOR_ENABLED=false` until login is ready.
3. Create the external Compose network with `docker network create services` if
   it does not exist. Create `data/database` and `data/session`, owned by UID/GID
   10001, and run `docker compose up -d`.
4. For a new session, run `docker compose run --rm app python -m app.login`.
   Enable collection in `.env` and recreate the collector with `docker compose up -d`.
5. Put the loopback-bound Web service behind an HTTPS reverse proxy and set
   `WEB_COOKIE_SECURE=true` when using HTTPS.

The Vastora JSON file is a recipe template, not a published catalog entry. Replace
its image placeholder with the verified release digest before packaging it.

## License and security

Licensed under Apache-2.0; see `LICENSE`. Third-party dependencies retain their
own licenses. For vulnerability reports and handling sensitive data, see
`SECURITY.md`.

## Source context and monitoring statistics

Classification, scoring, community and benefit assessment receive the recorded
source name and dialog type as bounded context. A name or a broadcast channel is
not an official-source verification. The push threshold remains 60; scam,
referral and VPS bandwidth exclusions remain in effect. Existing-customer quota
upgrades recognize G/T as capacity only with a quota/capacity context, not Gbps.
Practical self-hosted tools are judged by their concrete usefulness, not fame.
For mixed digests, scoring and notification preparation focus on one independently
valuable item rather than promoting unrelated rumors or averaging them into it.

The watch picker separates recorded messages, filtered content, analysis failures,
delivery failures and unread images. Cumulative recorded/pushed/image counters
survive message retention cleanup. A successful push means at least one delivery
target recorded success; a second target's failure remains visible and two
successful targets count as one message. Filtering and error counts shown in the
retained window are not lifetime totals.

The forward-only activity migration creates a verified, owner-readable SQLite
backup beside the database before modifying a populated instance. Backup or
migration failure prevents startup; no automatic downgrade is supported. Protect
these backups like the original database, and do not publish them. Retained old
messages seed only a partial historical baseline. Old subscription start times
are marked unknown, not invented. Subsequent watch enable/disable events and each
new listening interval are persisted atomically with the watch configuration.
Upgrading never queues or pushes historical messages.

Image OCR is not enabled: the collector does not download images or send them
to the model API. Captionless photos/image documents are stored as unread-image
audit records and do not invoke the model. Captions continue through the normal
text pipeline. Old file-only image records can be marked unread from metadata;
images previously discarded without a record cannot be reconstructed.

An absence of recorded messages alone does not prove a collection fault. Compare the watch
configuration start time, source posts and collector heartbeat before concluding
that updates were lost. There is no automatic Telegram history backfill.
