# Security policy

Report security vulnerabilities using GitHub private vulnerability reporting:
https://github.com/petauron/telegram-bot/security/advisories/new

Do not include tokens, passwords, Telegram sessions, databases, private messages,
phone numbers or private service addresses in public issues or pull requests.
Provide a minimal reproduction with synthetic data instead.

This application is a single-administrator service. Protect deployments with
HTTPS, use a unique password, and restrict administrative access as appropriate.
Keep credentials and persistent data outside the image and repository. Configure
your own model and notification endpoints; example.com addresses are placeholders.

Only the latest released version receives security fixes during the alpha phase.
