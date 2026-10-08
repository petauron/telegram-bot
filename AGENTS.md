# Telegram Bot

- Keep production credentials, databases, sessions, logs and host details out of Git.
- Build and publish images through GitHub Actions only; do not run local builds.
- Do not run local tests or linters without an explicit request.
- Use isolated fixtures in CI. Never contact production accounts in tests.
- Install and upgrade through Vastora's declarative application runtime.
- Preserve the database and Telegram session during migration; never run two
  collectors on the same account/session during cutover.
- Do not publish private source or images publicly without explicit approval.
