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
A `vVERSION` tag on a successful main commit builds the two native images and
publishes their immutable multiarchitecture index. See `VERSION`.

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
