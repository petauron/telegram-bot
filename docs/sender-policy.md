# Per-chat sender policy

An operator can restrict one Telegram group's analysis to exact sender IDs.
Unlisted senders' messages are still stored, with `sender_not_allowed` as the
filter reason. Other groups and non-Telegram sources are unchanged. Nicknames,
usernames, forwarded authors and channel identities do not grant permission.

Back up the database, stop the collector through the application's managed
runtime, then use the installed image's `python -m app.sender_policy` command:

```
python -m app.sender_policy --chat-id <negative-chat-id> \
  --allow <user-id> <user-id> --score-bonus 40
```

Start the collector through the same managed runtime. Before workers start,
pending jobs outside the allowlist finish as `prefiltered`, preserving original
messages and job history. New excluded messages never enter the model queue.
Manual analysis also respects the policy. Removing or widening a policy does
not automatically replay previously filtered messages or send notifications.

Allowed senders receive the configured score bonus (capped at 100) only after
successful content evaluation. This affects the effective AI score, including
the existing push threshold. Raw model responses retain the unweighted result.
Content classification, confidence checks, exact/semantic deduplication and
notification preparation are not bypassed. The default bonus is 40.

Policy is stored in the existing metadata table under
`analysis_sender_policy:<chat-id>`. Do not put real group/user IDs, credentials,
database backups or deployment details in the repository.
