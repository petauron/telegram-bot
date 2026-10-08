from __future__ import annotations

import unittest
from email.message import EmailMessage

from app.feeds import FeedError
from app.newsletter_imap import (
    NewsletterImapFetcher,
    clean_newsletter_text,
    imap_source_url,
    parse_newsletter_message,
    validate_imap_settings,
)


def newsletter_bytes(*, sender: str = "news@vendor.example") -> bytes:
    message = EmailMessage()
    message["Subject"] = "Cloud platform security release"
    message["From"] = sender
    message["To"] = "reader@example.com"
    message["Date"] = "Tue, 11 Aug 2026 09:00:00 +0000"
    message["Message-ID"] = "<newsletter-001@vendor.example>"
    message.set_content(
        "Cloud platform 2.0 fixes a critical vulnerability.\n\n"
        "https://vendor.example/release?utm_source=email&ref=newsletter\n\n"
        "Unsubscribe\nhttps://vendor.example/unsubscribe?token=secret\n"
    )
    message.add_attachment(b"not-stored", maintype="application", subtype="octet-stream", filename="report.bin")
    return message.as_bytes()


class FakeImap:
    def __init__(self, messages: dict[int, bytes], *, uidnext: int = 11) -> None:
        self.messages = messages
        self.uidnext = uidnext
        self.readonly = None
        self.logged_out = False

    def select(self, _: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.readonly = readonly
        return "OK", [b"1"]

    def status(self, mailbox: str, _: str) -> tuple[str, list[bytes]]:
        return "OK", [f'{mailbox} (UIDNEXT {self.uidnext})'.encode()]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "SEARCH":
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(self.messages))]
        uid = int(str(args[0]))
        raw = self.messages[uid]
        return "OK", [(f"{uid} (BODY[] {{{len(raw)}}})".encode(), raw), b")"]

    def logout(self) -> None:
        self.logged_out = True


class NewsletterImapTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_require_public_imaps_and_source_url_has_no_password(self) -> None:
        config = validate_imap_settings(
            {
                "host": "imap.vendor.example",
                "port": 993,
                "username": "reader@example.com",
                "mailbox": "INBOX/Newsletters",
                "sender_allowlist": ["news@vendor.example", "security.example"],
            }
        )
        url = imap_source_url(config)
        self.assertTrue(url.startswith("imaps://imap.vendor.example:993/"))
        self.assertNotIn("password", url)
        for settings in (
            {**config, "host": "localhost"},
            {**config, "host": "127.0.0.1"},
            {**config, "port": 143},
            {**config, "mailbox": "../Secrets"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate_imap_settings(settings)

    def test_message_cleaning_removes_attachment_footer_and_tracking(self) -> None:
        entry = parse_newsletter_message(
            newsletter_bytes(), uid=11, sender_allowlist=("vendor.example",)
        )
        self.assertIsNotNone(entry)
        self.assertIn("critical vulnerability", entry.body)
        self.assertNotIn("not-stored", entry.body)
        self.assertNotIn("Unsubscribe", entry.body)
        self.assertNotIn("utm_source", entry.body)
        self.assertNotIn("ref=", entry.url)
        blocked = parse_newsletter_message(
            newsletter_bytes(sender="ads@other.example"),
            uid=12,
            sender_allowlist=("vendor.example",),
        )
        self.assertIsNone(blocked)

    def test_cleaning_stops_at_quoted_thread(self) -> None:
        cleaned, _ = clean_newsletter_text(
            "Release facts.\n\nOn Mon, someone wrote:\nOld quoted content"
        )
        self.assertEqual(cleaned, "Release facts.")

    async def test_first_fetch_uses_uidnext_without_replaying_history(self) -> None:
        clients: list[FakeImap] = []

        def factory(_: dict, password: str) -> FakeImap:
            self.assertEqual(password, "test-only-mail-password")
            client = FakeImap({10: newsletter_bytes()}, uidnext=11)
            clients.append(client)
            return client

        fetcher = NewsletterImapFetcher(client_factory=factory)
        result = await fetcher.fetch(
            {
                "initialized": False,
                "settings": {
                    "host": "imap.vendor.example", "port": 993,
                    "username": "reader@example.com", "mailbox": "INBOX",
                    "sender_allowlist": [],
                },
            },
            token="test-only-mail-password",
        )
        self.assertEqual(result.feed.entries, ())
        self.assertEqual(result.cursor_value, "10")
        self.assertTrue(clients[0].readonly)
        self.assertTrue(clients[0].logged_out)

    async def test_incremental_fetch_is_bounded_and_advances_uid_cursor(self) -> None:
        def factory(_: dict, __: str) -> FakeImap:
            return FakeImap({11: newsletter_bytes(), 12: newsletter_bytes(sender="ads@other.example")}, uidnext=13)

        fetcher = NewsletterImapFetcher(client_factory=factory)
        result = await fetcher.fetch(
            {
                "initialized": True,
                "cursor_value": "10",
                "settings": {
                    "host": "imap.vendor.example", "port": 993,
                    "username": "reader@example.com", "mailbox": "INBOX",
                    "sender_allowlist": ["vendor.example"],
                },
            },
            token="test-only-mail-password",
        )
        self.assertEqual(len(result.feed.entries), 1)
        self.assertEqual(result.cursor_value, "12")

    async def test_missing_password_is_a_stable_error(self) -> None:
        fetcher = NewsletterImapFetcher(client_factory=lambda *_: None)
        with self.assertRaises(FeedError) as raised:
            await fetcher.fetch(
                {"settings": {"host": "imap.vendor.example", "port": 993, "username": "reader@example.com", "mailbox": "INBOX"}}
            )
        self.assertEqual(raised.exception.category, "credentials_missing")


if __name__ == "__main__":
    unittest.main()
