from __future__ import annotations

import asyncio
import hashlib
import imaplib
import ipaddress
import re
import socket
import ssl
from datetime import timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from html import unescape
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from app.feeds import FeedEntry, FeedError, FeedFetchResult, ParsedFeed, clean_feed_html
from app.github_releases import GitHubSourcePoller


IMAP_PORT = 993
IMAP_MESSAGE_LIMIT = 50
IMAP_RAW_MESSAGE_MAX_BYTES = 2 * 1024 * 1024
IMAP_SEARCH_MAX_BYTES = 512 * 1024
IMAP_BODY_MAX_CHARS = 12_000
IMAP_CONNECT_TIMEOUT = 15.0
IMAP_HOST_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
IMAP_MAILBOX_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
IMAP_FOOTER_PATTERN = re.compile(
    r"^(?:unsubscribe|manage (?:email )?preferences|view in browser|privacy policy|"
    r"退订|取消订阅|管理订阅|在浏览器中查看)\b",
    re.IGNORECASE,
)
IMAP_QUOTE_PATTERN = re.compile(r"^(?:On .{0,160} wrote:|在 .{0,160} 写道：|From:\s)", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s<>\]\[()\"']+", re.IGNORECASE)
TRACKING_PARAMETERS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source", "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term"}
)


def validate_imap_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    raw = settings or {}
    host = str(raw.get("host") or "").strip().rstrip(".").casefold()
    if not IMAP_HOST_PATTERN.fullmatch(host) or host in {"localhost"} or host.endswith((".local", ".localhost")):
        raise ValueError("IMAP 主机必须是公开域名")
    try:
        port = int(raw.get("port", IMAP_PORT))
    except (TypeError, ValueError) as exc:
        raise ValueError("IMAP 端口必须是 993") from exc
    if port != IMAP_PORT:
        raise ValueError("仅支持标准 IMAPS 993 端口")
    username = str(raw.get("username") or "").strip()
    if not username or len(username) > 254 or any(ord(character) < 32 for character in username):
        raise ValueError("邮箱账号格式无效")
    mailbox = str(raw.get("mailbox") or "INBOX").strip() or "INBOX"
    if not IMAP_MAILBOX_PATTERN.fullmatch(mailbox) or ".." in mailbox:
        raise ValueError("邮箱目录格式无效")
    allowlist = tuple(
        dict.fromkeys(
            str(item).strip().casefold().removeprefix("@")[:254]
            for item in (raw.get("sender_allowlist") or ())
            if str(item).strip()
        )
    )
    if len(allowlist) > 30 or any(" " in item or "/" in item for item in allowlist):
        raise ValueError("发件人白名单格式无效")
    return {
        "host": host,
        "port": port,
        "username": username,
        "mailbox": mailbox,
        "sender_allowlist": list(allowlist),
    }


def imap_source_url(settings: dict[str, Any] | None) -> str:
    config = validate_imap_settings(settings)
    query = urlencode({"account": config["username"]})
    return f"imaps://{config['host']}:{config['port']}/{quote(config['mailbox'], safe='')}?{query}"


def _is_public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_global
    except ValueError:
        return False


def resolve_public_imap_host(host: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError as exc:
        raise FeedError("dns_error") from exc
    addresses = tuple(dict.fromkeys(str(record[4][0]).split("%", 1)[0] for record in records))
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise FeedError("unsafe_address")
    return addresses


class _PinnedIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, address: str) -> None:
        self._pinned_address = address
        super().__init__(
            host=host,
            port=port,
            ssl_context=ssl.create_default_context(),
            timeout=IMAP_CONNECT_TIMEOUT,
        )

    def _create_socket(self, timeout: float | None) -> ssl.SSLSocket:
        raw = socket.create_connection((self._pinned_address, self.port), timeout)
        return self.ssl_context.wrap_socket(raw, server_hostname=self.host)


def _connect_imap(config: dict[str, Any], password: str) -> imaplib.IMAP4_SSL:
    addresses = resolve_public_imap_host(str(config["host"]), int(config["port"]))
    client = _PinnedIMAP4SSL(str(config["host"]), int(config["port"]), addresses[0])
    try:
        client.login(str(config["username"]), password)
    except Exception:
        try:
            client.logout()
        except Exception:
            pass
        raise
    return client


def _safe_article_url(value: str) -> str | None:
    raw = unescape(value).strip().rstrip(".,;:")
    if not raw or len(raw) > 2048 or any(ord(character) < 32 for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 80, 443}
    ):
        return None
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in TRACKING_PARAMETERS
            and not key.casefold().startswith("utm_")
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", query, ""))


def clean_newsletter_text(value: str, *, html: bool = False) -> tuple[str, str | None]:
    text = clean_feed_html(value, limit=64_000) if html else value
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    links: list[str] = []

    def replace_url(match: re.Match[str]) -> str:
        safe = _safe_article_url(match.group(0))
        if safe and not any(marker in safe.casefold() for marker in ("unsubscribe", "optout", "/track")):
            links.append(safe)
            return safe
        return ""

    text = URL_PATTERN.sub(replace_url, text)
    raw_lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in text.splitlines()]
    compact: list[str] = []
    for index, line in enumerate(raw_lines):
        if line.startswith(">") or IMAP_QUOTE_PATTERN.match(line):
            break
        if index >= max(3, len(raw_lines) // 2) and IMAP_FOOTER_PATTERN.match(line):
            break
        if not line and (not compact or not compact[-1]):
            continue
        compact.append(line)
    cleaned = "\n".join(compact).strip()[:IMAP_BODY_MAX_CHARS]
    return cleaned, next(iter(dict.fromkeys(links)), None)


def _message_body(message: Any) -> tuple[str, str | None]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except Exception:
            continue
        if not isinstance(value, str):
            continue
        target = plain_parts if content_type == "text/plain" else html_parts
        target.append(value)
        if sum(len(item) for item in target) >= 64_000:
            break
    if plain_parts:
        return clean_newsletter_text("\n".join(plain_parts))
    return clean_newsletter_text("\n".join(html_parts), html=True)


def parse_newsletter_message(
    raw: bytes, *, uid: int, sender_allowlist: tuple[str, ...]
) -> FeedEntry | None:
    if not raw or len(raw) > IMAP_RAW_MESSAGE_MAX_BYTES:
        raise FeedError("response_too_large")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise FeedError("invalid_response") from exc
    sender = parseaddr(str(message.get("From") or ""))[1].casefold()
    if sender_allowlist and not any(
        sender == allowed or sender.endswith(f"@{allowed}") for allowed in sender_allowlist
    ):
        return None
    subject = clean_feed_html(str(message.get("Subject") or "Newsletter"), limit=300)
    body, article_url = _message_body(message)
    if not body:
        return None
    try:
        sent_at = parsedate_to_datetime(str(message.get("Date") or ""))
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        published = sent_at.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        published = None
    message_id = str(message.get("Message-ID") or f"uid:{uid}")
    fingerprint = hashlib.sha256(message_id.encode("utf-8", errors="replace")).hexdigest()[:20]
    return FeedEntry(
        external_id=f"imap-message:{uid}:{fingerprint}",
        title=subject or "Newsletter",
        body=body,
        url=article_url,
        published_at=published,
    )


def _response_bytes(data: object) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, tuple):
        return b"".join(item for item in data if isinstance(item, bytes))
    if isinstance(data, list):
        return b"".join(_response_bytes(item) for item in data)
    return b""


def _fetch_message_bytes(data: object) -> bytes:
    candidates: list[bytes] = []

    def collect(value: object) -> None:
        if isinstance(value, bytes):
            candidates.append(value)
        elif isinstance(value, (tuple, list)):
            for item in value:
                collect(item)

    collect(data)
    return max(candidates, key=len, default=b"")


class NewsletterImapFetcher:
    def __init__(
        self,
        *,
        client_factory: Callable[[dict[str, Any], str], Any] = _connect_imap,
    ) -> None:
        self._client_factory = client_factory

    async def close(self) -> None:
        return None

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        return await asyncio.to_thread(self._fetch_sync, source, token)

    def _fetch_sync(self, source: dict[str, Any], password: str) -> FeedFetchResult:
        if not password:
            raise FeedError("credentials_missing")
        config = validate_imap_settings(source.get("settings"))
        client: Any | None = None
        try:
            client = self._client_factory(config, password)
            status, _ = client.select(str(config["mailbox"]), readonly=True)
            if status != "OK":
                raise FeedError("mailbox_error")
            if not bool(source.get("initialized")):
                status, data = client.status(str(config["mailbox"]), "(UIDNEXT)")
                raw_status = _response_bytes(data)
                match = re.search(rb"UIDNEXT\s+(\d+)", raw_status)
                if status != "OK" or not match:
                    raise FeedError("invalid_response")
                cursor = str(max(0, int(match.group(1)) - 1))
                return FeedFetchResult(False, ParsedFeed(()), None, None, 200, cursor)
            cursor = int(str(source.get("cursor_value") or "0"))
            status, data = client.uid("SEARCH", None, f"UID {cursor + 1}:*")
            raw_ids = _response_bytes(data)
            if status != "OK":
                raise FeedError("mailbox_error")
            if len(raw_ids) > IMAP_SEARCH_MAX_BYTES:
                raise FeedError("response_too_large")
            uids = [int(item) for item in raw_ids.split() if item.isdigit()]
            entries: list[FeedEntry] = []
            newest = cursor
            allowlist = tuple(config["sender_allowlist"])
            for uid in sorted(uids)[:IMAP_MESSAGE_LIMIT]:
                status, fetched = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    raise FeedError("mailbox_error")
                raw_message = _fetch_message_bytes(fetched)
                entry = parse_newsletter_message(
                    raw_message, uid=uid, sender_allowlist=allowlist
                )
                newest = max(newest, uid)
                if entry is not None:
                    entries.append(entry)
            return FeedFetchResult(
                False, ParsedFeed(tuple(entries)), None, None, 200, str(newest)
            )
        except FeedError:
            raise
        except imaplib.IMAP4.error as exc:
            raise FeedError("authentication_error") from exc
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise FeedError("tls_error") from exc
        except (OSError, TimeoutError) as exc:
            raise FeedError("network_error") from exc
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass


class NewsletterImapPoller(GitHubSourcePoller):
    SOURCE_KIND = "newsletter_imap"
    SENDER_NAME = "邮件 Newsletter"
    LOG_LABEL = "邮件 Newsletter"
    FETCHER_TYPE = NewsletterImapFetcher
    CREDENTIAL_PROVIDER = None
    SOURCE_CREDENTIAL = True
