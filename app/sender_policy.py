"""Operator-only per-chat analysis policy; does not process or delete messages."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--allow", type=int, nargs="+", required=True)
    parser.add_argument("--score-bonus", type=int, default=40)
    parser.add_argument("--service-announcers", type=int, nargs="*", default=[])
    args = parser.parse_args()
    if args.chat_id >= 0 or any(value <= 0 for value in args.allow):
        parser.error("Expected a negative Telegram chat ID and positive user IDs")
    if not 0 <= args.score_bonus <= 100:
        parser.error("Score bonus must be between 0 and 100")
    if any(value not in args.allow for value in args.service_announcers):
        parser.error("Verified service announcers must be in the allowlist")
    path = os.environ.get("DATABASE_PATH", "/database/messages.db")
    connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=10)
    try:
        with connection:
            connection.execute(
                """INSERT INTO metadata(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (
                    f"analysis_sender_policy:{args.chat_id}",
                    json.dumps({
                        "allowed_sender_ids": sorted(set(args.allow)),
                        "score_bonus": args.score_bonus,
                        "service_announcers": sorted(set(args.service_announcers)),
                    }),
                ),
            )
    finally:
        connection.close()
    print("Policy saved. Restart the collector after backup to apply pending jobs.")


if __name__ == "__main__":
    main()
