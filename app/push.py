from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from email.header import Header
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from app.database import utc_now
from app.ntfy_feedback import build_feedback_actions
from app.push_config import validate_ntfy_base_url, validate_ntfy_topic
from app.utils import clean_one_line


LOGGER = logging.getLogger(__name__)
MAX_HTML_UNITS = 3_900
PUSH_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=15.0)
NTFY_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0, write=5.0, pool=5.0)
NTFY_TITLE_MAX_BYTES = 120
NTFY_BODY_MAX_BYTES = 3_500
NTFY_TEXT_SNIPPET_MAX_BYTES = 2_800
NTFY_SUMMARY_MAX_BYTES = 300
NOTIFICATION_EXTERNAL_URL_MAX_BYTES = 1_024
NTFY_PRIORITIES = frozenset({"low", "default", "high", "max"})
NTFY_JSON_PRIORITY_VALUES = {"low": 2, "default": 3, "high": 4, "max": 5}
NTFY_SCORE_COLORS = (
    (90, "🟥"),
    (80, "🟧"),
    (70, "🟨"),
    (60, "🟩"),
)
NTFY_CONTENT_LABELS = {
    "community_signal": "讨论",
    "benefit_deal": "福利",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_MULTILINE_CONTROL_RE = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f]+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_SPECIAL_RE = re.compile(r"([\\`*_{\[\]()\]<>#+.!|>~\-])")
_TITLE_MARKUP_RE = re.compile(r"[`*_~#>|\[\]{}]+")
_TITLE_SENTENCE_RE = re.compile(r"[。！？!?]")
_BODY_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])")
_TRACKING_QUERY_KEYS = frozenset(
    {
        "aff",
        "affid",
        "affiliate",
        "affiliate_id",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_id",
        "referral",
        "source",
    }
)


def _utf16_units(value: str) -> int:
    # Telegram's limit is measured in UTF-16 code units after entity parsing.
    # Counting the still-escaped HTML is conservative and therefore safe.
    return len(value.encode("utf-16-le")) // 2


def _escape(value: object, limit: int = 300) -> str:
    return html.escape(clean_one_line(str(value), limit), quote=True)


def _bounded_paragraph_text(value: object, *, max_bytes: int) -> str:
    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = _MULTILINE_CONTROL_RE.sub(" ", normalized)
    lines: list[str] = []
    previous_blank = True
    for raw_line in normalized.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    while lines and not lines[-1]:
        lines.pop()
    return _truncate_utf8("\n".join(lines), max_bytes, suffix="")


def _is_product_reputation(row: dict[str, Any]) -> bool:
    return bool(
        row.get("content_kind") == "community_signal"
        and row.get("community_signal_type") == "product_review"
    )


def _notification_detail_label(row: dict[str, Any]) -> str:
    if _is_product_reputation(row):
        return "口碑摘要"
    if row.get("content_kind") == "community_signal":
        return "讨论结论"
    if row.get("content_kind") == "benefit_deal":
        return "优惠详情"
    return "资讯摘要"


def _notification_body_items(
    value: object,
    *,
    max_bytes: int,
    max_items: int = 8,
) -> tuple[str, ...]:
    """Turn an already prepared body into bounded readable facts without rewriting it."""
    normalized = _bounded_paragraph_text(value, max_bytes=max_bytes)
    items: list[str] = []
    seen: set[str] = set()
    for paragraph in re.split(r"\n+", normalized):
        for candidate in _BODY_SENTENCE_BOUNDARY_RE.split(paragraph):
            item = candidate.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            items.append(item)
    if not items:
        return ("（无文字）",)
    if len(items) > max_items:
        items = [*items[: max_items - 1], " ".join(items[max_items - 1 :])]
    return tuple(items)


def _render_html_notification_body(value: object, *, max_bytes: int) -> str:
    items = _notification_body_items(value, max_bytes=max_bytes)
    escaped = [html.escape(item, quote=True) for item in items]
    if len(escaped) == 1:
        return escaped[0]
    return "\n".join(f"• {item}" for item in escaped)


def _render_markdown_notification_body(value: object, *, max_bytes: int) -> str:
    items = _notification_body_items(value, max_bytes=max_bytes * 2)
    use_bullets = len(items) > 1
    rendered: list[str] = []
    consumed = 0
    for item in items:
        separator = "\n" if rendered else ""
        prefix = "- " if use_bullets else ""
        remaining = max_bytes - consumed - len(separator.encode("utf-8")) - len(prefix)
        if remaining <= 0:
            break
        escaped = _markdown_escape(item, max_bytes=remaining)
        if not escaped:
            break
        line = f"{prefix}{escaped}"
        rendered.append(line)
        consumed += len(separator.encode("utf-8")) + len(line.encode("utf-8"))
    return "\n".join(rendered) or "（无文字）"


def _source_label(row: dict) -> str:
    source = str(row.get("chat_name") or "未知来源")
    if row.get("content_kind") == "benefit_deal" and row.get("_source_chat_type") != "channel":
        sender = str(row.get("sender_name") or "").strip()
        return f"{source} · 发言者：{sender or '未知'}（未经官方身份认证）"
    return source


def format_message_block(row: dict, *, snippet_limit: int = 650) -> str:
    del snippet_limit
    title_value = _notification_title_value(row)
    if _is_product_reputation(row):
        title_value = f"【产品口碑】{title_value}"
    elif row.get("content_kind") == "community_signal":
        title_value = f"【社区线索】{title_value}"
    elif row.get("content_kind") == "benefit_deal":
        title_value = f"【福利羊毛】{title_value}"
    title = _escape(title_value, 180)
    source = _escape(_source_label(row), 240)
    detail_label = _notification_detail_label(row)
    body = _render_html_notification_body(
        _notification_body_value(row), max_bytes=7_200
    )
    lines = [
        f"<b>{title}</b>",
        "",
        "<b>来源</b>",
        source,
        "",
        f"<b>{detail_label}</b>",
        body,
    ]
    external_link = notification_external_url(row)
    if external_link:
        safe_link = html.escape(external_link, quote=True)
        lines.append("")
        lines.append(f'<a href="{safe_link}">查看原文</a>')
    block = "\n".join(lines)
    if _utf16_units(block) <= 3_300:
        return block
    return "\n".join(
        (
            f"<b>{title}</b>",
            "",
            "<b>来源</b>",
            source,
            "",
            f"<b>{detail_label}</b>",
            _render_html_notification_body(
                _notification_body_value(row), max_bytes=3_000
            ),
        )
    )


def pack_html_blocks(header: str, blocks: Iterable[str]) -> list[str]:
    chunks: list[str] = []
    current = header
    for block in blocks:
        addition = f"\n\n{block}"
        if _utf16_units(current + addition) <= MAX_HTML_UNITS:
            current += addition
            continue
        if current:
            chunks.append(current)
        current = f"{header}\n\n{block}"
        if _utf16_units(current) > MAX_HTML_UNITS:
            # All user-controlled fields are already truncated. This is a final
            # conservative guard that preserves valid HTML by dropping detail.
            current = f"{header}\n\n消息内容过长，已安全截断。"
    if current:
        chunks.append(current)
    return chunks


def immediate_chunks(row: dict) -> list[str]:
    header = (
        "⭐ <b>产品口碑提醒</b>"
        if _is_product_reputation(row)
        else "💬 <b>社区线索即时提醒</b>"
        if row.get("content_kind") == "community_signal"
        else "🎁 <b>福利羊毛即时提醒</b>"
        if row.get("content_kind") == "benefit_deal"
        else "📰 <b>重要资讯即时提醒</b>"
    )
    return pack_html_blocks(header, [format_message_block(row)])


def digest_chunks(rows: list[dict]) -> list[str]:
    kinds = {row.get("content_kind") for row in rows}
    if rows and all(_is_product_reputation(row) for row in rows):
        label = "产品口碑摘要"
    elif kinds == {"community_signal"}:
        label = "社区线索摘要"
    elif kinds == {"benefit_deal"}:
        label = "福利羊毛摘要"
    elif kinds <= {"news", None}:
        label = "重要资讯摘要"
    else:
        label = "资讯、社区、口碑与福利摘要"
    header = f"📌 <b>{label}</b>（{len(rows)} 条）"
    blocks = [format_message_block(row, snippet_limit=520) for row in rows]
    return pack_html_blocks(header, blocks)


class BotPusher:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        api_base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._endpoint = f"{api_base_url.rstrip('/')}/bot{token}/sendMessage"
        self._client = httpx.AsyncClient(
            timeout=PUSH_HTTP_TIMEOUT,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "telegram-priority-push/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send_chunks(self, chunks: Iterable[str]) -> bool:
        for chunk in chunks:
            if not await self._send_one(chunk):
                return False
        return True

    async def _send_one(self, text: str) -> bool:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        delay = 2.0
        for attempt in range(1, 6):
            try:
                response = await self._client.post(self._endpoint, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError):
                LOGGER.warning("Bot API 网络错误，第 %s/5 次尝试", attempt)
            else:
                if response.status_code == 200:
                    return True
                if response.status_code == 429:
                    retry_after = 5
                    try:
                        retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
                    except (ValueError, TypeError):
                        pass
                    LOGGER.warning("Bot API 限流，等待 %s 秒", retry_after)
                    await asyncio.sleep(max(1, retry_after))
                    continue
                if 500 <= response.status_code < 600:
                    LOGGER.warning("Bot API 暂时不可用（HTTP %s）", response.status_code)
                else:
                    LOGGER.error("Bot API 拒绝推送（HTTP %s）", response.status_code)
                    return False

            if attempt < 5:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        return False


def _truncate_utf8(value: str, max_bytes: int, *, suffix: str = "…") -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) > max_bytes:
        return suffix_bytes[:max_bytes].decode("utf-8", errors="ignore")
    budget = max(0, max_bytes - len(suffix_bytes))
    truncated = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return f"{truncated}{suffix}" if truncated else suffix[:1]


def _clean_untrusted_line(value: object, *, max_bytes: int) -> str:
    without_controls = _CONTROL_RE.sub(" ", str(value))
    compact = re.sub(r"\s+", " ", without_controls).strip()
    return _truncate_utf8(compact, max_bytes)


def _markdown_escape(value: object, *, max_bytes: int) -> str:
    cleaned = _clean_untrusted_line(value, max_bytes=max_bytes)
    escaped = _MARKDOWN_SPECIAL_RE.sub(r"\\\1", cleaned)
    if len(escaped.encode("utf-8")) <= max_bytes:
        return escaped
    return _truncate_utf8(escaped, max_bytes, suffix="").rstrip("\\")


def _title_candidate(value: object) -> str:
    cleaned = _clean_untrusted_line(value, max_bytes=NTFY_SUMMARY_MAX_BYTES)
    cleaned = _URL_RE.sub("", cleaned)
    cleaned = _TITLE_MARKUP_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,。.;；-—")
    if not cleaned:
        return ""
    sentence_end = _TITLE_SENTENCE_RE.search(cleaned)
    sentence = cleaned[: sentence_end.end()] if sentence_end else cleaned
    return _truncate_utf8(sentence, NTFY_TITLE_MAX_BYTES)


def _notification_title_value(row: dict[str, Any]) -> str:
    prepared = _title_candidate(row.get("notification_title") or "")
    if prepared:
        return prepared
    summary = (
        _title_candidate(row.get("ai_summary") or "")
        if row.get("ai_status") == "success"
        else ""
    )
    if summary:
        return summary
    for line in str(row.get("text") or "").splitlines():
        value = _title_candidate(line)
        if value:
            return value
    return "重要资讯提醒"


def _notification_body_value(row: dict[str, Any]) -> str:
    prepared = str(row.get("notification_body") or "").strip()
    return prepared or str(row.get("text") or "（无文字）").strip()


def _sanitized_external_url(value: object) -> str | None:
    # Structural URL characters are percent-encoded below, so do not trim a
    # legitimate closing bracket/parenthesis from the path. Only prose
    # punctuation that commonly follows a pasted URL is removed here.
    candidate = str(value or "").strip().rstrip(".,，。;；!?！？")
    if not candidate or _CONTROL_RE.search(candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "\\" in parsed.netloc
        or any(character.isspace() for character in parsed.netloc)
    ):
        return None
    hostname = parsed.hostname.casefold()
    if hostname in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        return None
    safe_path = quote(
        parsed.path,
        safe="/:@-._~!$&'+,;=%",
    )
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in _TRACKING_QUERY_KEYS
        ],
        doseq=True,
    )
    sanitized = urlunsplit((parsed.scheme, parsed.netloc, safe_path, query, ""))
    if len(sanitized.encode("utf-8")) > NOTIFICATION_EXTERNAL_URL_MAX_BYTES:
        return None
    return sanitized


def notification_external_url(row: dict[str, Any]) -> str | None:
    candidates = [row.get("primary_url"), *_URL_RE.findall(str(row.get("text") or ""))]
    for candidate in candidates:
        sanitized = _sanitized_external_url(candidate)
        if sanitized:
            return sanitized
    return None


def _safe_score(value: object) -> int:
    try:
        return max(0, min(100, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def ntfy_priority(row: dict[str, Any]) -> str:
    score = _safe_score(row.get("ai_score"))
    if score >= 90:
        return "max"
    if score >= 80:
        return "high"
    if score >= 60:
        return "default"
    return "low"


def ntfy_score_color(row: dict[str, Any]) -> str:
    score = _safe_score(row.get("ai_score"))
    return next(
        (color for minimum, color in NTFY_SCORE_COLORS if score >= minimum),
        "⬜",
    )


def ntfy_title(row: dict[str, Any]) -> str:
    essence = _notification_title_value(row)
    score = _safe_score(row.get("ai_score"))
    color = ntfy_score_color(row)
    content_label = (
        "口碑"
        if _is_product_reputation(row)
        else NTFY_CONTENT_LABELS.get(row.get("content_kind"), "新闻")
    )
    prefix = f"{color}{score}｜{content_label}｜"
    essence_budget = NTFY_TITLE_MAX_BYTES - len(prefix.encode("utf-8"))
    return f"{prefix}{_truncate_utf8(essence, essence_budget)}"


@dataclass(frozen=True, slots=True)
class NtfyNotification:
    title: str
    body: str
    priority: str
    actions: tuple[dict[str, Any], ...] = ()


def build_ntfy_notification(row: dict[str, Any]) -> NtfyNotification:
    title = ntfy_title(row)
    source = _markdown_escape(
        _source_label(row),
        max_bytes=240,
    )
    external_link = notification_external_url(row)
    link_suffix = f"\n\n[查看原文]({external_link})" if external_link else ""
    detail_label = _notification_detail_label(row)
    body_prefix = f"**来源**\n{source}\n\n**{detail_label}**\n"
    text_budget = max(
        0,
        NTFY_BODY_MAX_BYTES
        - len(body_prefix.encode("utf-8"))
        - len(link_suffix.encode("utf-8")),
    )
    text = _render_markdown_notification_body(
        _notification_body_value(row),
        max_bytes=min(NTFY_TEXT_SNIPPET_MAX_BYTES, text_budget),
    )
    body = f"{body_prefix}{text}{link_suffix}"
    return NtfyNotification(
        title=title,
        body=body,
        priority=ntfy_priority(row),
        actions=tuple(row.get("_ntfy_feedback_actions") or ()),
    )


def _encode_ntfy_header(value: str) -> str:
    safe = _CONTROL_RE.sub(" ", value).strip()
    return Header(safe, "utf-8", maxlinelen=998).encode(linesep="")


class NtfyPusher:
    def __init__(
        self,
        base_url: str,
        topic: str,
        access_token: str | None,
        *,
        content_topics: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_base = validate_ntfy_base_url(base_url)
        normalized_topic = validate_ntfy_topic(topic, required=True)
        self._base_url = normalized_base
        self._topic = normalized_topic
        supplied_topics = content_topics or {}
        self._content_topics = {
            "news": validate_ntfy_topic(
                supplied_topics.get("news", normalized_topic), required=True
            ),
            "community_signal": validate_ntfy_topic(
                supplied_topics.get("community_signal", normalized_topic), required=True
            ),
            "benefit_deal": validate_ntfy_topic(
                supplied_topics.get("benefit_deal", normalized_topic), required=True
            ),
        }
        headers = {
            "User-Agent": "telegram-priority-push/1.0",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.AsyncClient(
            timeout=NTFY_HTTP_TIMEOUT,
            follow_redirects=False,
            transport=transport,
            headers=headers,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send_rows(self, rows: Iterable[dict[str, Any]]) -> bool:
        for row in rows:
            content_kind = str(row.get("content_kind") or "news")
            topic = self._content_topics.get(content_kind, self._topic)
            if not await self.send_notification(
                build_ntfy_notification(row), topic=topic
            ):
                return False
        return True

    async def send_test(self) -> bool:
        return await self.send_notification(
            NtfyNotification(
                title="🟡 测试｜群讯雷达渠道可用",
                body="**来源：** 群讯雷达\n\n渠道配置可用，后续资讯将使用优先等级标题和简洁正文。",
                priority="default",
            )
        )

    async def send_notification(
        self,
        notification: NtfyNotification,
        *,
        topic: str | None = None,
    ) -> bool:
        if notification.priority not in NTFY_PRIORITIES:
            raise ValueError("ntfy 通知元数据无效")
        target_topic = validate_ntfy_topic(topic or self._topic, required=True)
        if notification.actions:
            if any(
                action.get("action") != "http"
                or action.get("method") != "POST"
                or not action.get("url")
                or not action.get("body")
                for action in notification.actions
            ):
                raise ValueError("ntfy 反馈动作无效")
            endpoint = self._base_url
            headers = {"Content-Type": "application/json; charset=utf-8"}
            content = json.dumps(
                {
                    "topic": target_topic,
                    "title": notification.title,
                    "message": notification.body,
                    "priority": NTFY_JSON_PRIORITY_VALUES[notification.priority],
                    "markdown": True,
                    "actions": list(notification.actions),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            endpoint = f"{self._base_url}/{quote(target_topic, safe='')}"
            headers = {
                "Content-Type": "text/markdown; charset=utf-8",
                "X-Title": _encode_ntfy_header(notification.title),
                "X-Priority": notification.priority,
                "X-Markdown": "yes",
            }
            content = notification.body.encode("utf-8")
        try:
            response = await self._client.post(
                endpoint,
                content=content,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            LOGGER.warning("ntfy 网络错误")
            return False
        if 200 <= response.status_code < 300:
            return True
        if response.status_code == 429:
            LOGGER.warning("ntfy 限流，当前推送未完成")
        elif 500 <= response.status_code < 600:
            LOGGER.warning("ntfy 暂时不可用（HTTP %s）", response.status_code)
        else:
            LOGGER.error("ntfy 拒绝推送（HTTP %s）", response.status_code)
        return False


class PushDispatcher:
    """Resolve saved channels for every send so Web changes apply immediately."""

    def __init__(self, database: Any) -> None:
        self._database = database
        self._signature: tuple[object, ...] | None = None
        self._channels: tuple[tuple[str, BotPusher | NtfyPusher], ...] = ()
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            await self._close_channels()

    async def _close_channels(self) -> None:
        channels, self._channels = self._channels, ()
        self._signature = None
        await asyncio.gather(
            *(channel.close() for _, channel in channels),
            return_exceptions=True,
        )

    async def _refresh_channels(self) -> None:
        config = self._database.get_push_config(include_secrets=True)
        telegram = config["telegram"]
        ntfy = config["ntfy"]
        signature = (
            telegram["enabled"],
            telegram.get("bot_token"),
            telegram["chat_id"],
            ntfy["enabled"],
            ntfy["base_url"],
            ntfy["topic"],
            ntfy["community_topic"],
            ntfy["benefit_topic"],
            ntfy.get("access_token"),
        )
        if signature == self._signature:
            return
        await self._close_channels()
        channels: list[tuple[str, BotPusher | NtfyPusher]] = []
        if telegram["enabled"] and telegram.get("bot_token") and telegram["chat_id"]:
            channels.append(
                (
                    "telegram",
                    BotPusher(str(telegram["bot_token"]), str(telegram["chat_id"])),
                )
            )
        if (
            ntfy["enabled"]
            and ntfy["topic"]
            and ntfy["community_topic"]
            and ntfy["benefit_topic"]
        ):
            channels.append(
                (
                    "ntfy",
                    NtfyPusher(
                        str(ntfy["base_url"]),
                        str(ntfy["topic"]),
                        str(ntfy["access_token"]) if ntfy.get("access_token") else None,
                        content_topics={
                            "news": str(ntfy["topic"]),
                            "community_signal": str(ntfy["community_topic"]),
                            "benefit_deal": str(ntfy["benefit_topic"]),
                        },
                    ),
                )
            )
        self._channels = tuple(channels)
        self._signature = signature

    def _with_ntfy_feedback(
        self,
        rows: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        config = self._database.get_push_config(include_secrets=True)
        ntfy = config["ntfy"]
        feedback_topic = ntfy.get("feedback_topic")
        signing_key = ntfy.get("feedback_signing_key")
        if not ntfy.get("enabled") or not feedback_topic or not signing_key:
            return rows
        prepared: list[dict[str, Any]] = []
        for row in rows:
            row_id = row.get("id")
            if row_id is None:
                prepared.append(row)
                continue
            target = self._database.prepare_ntfy_feedback_target(
                int(row_id),
                now=utc_now(),
            )
            prepared.append(
                {
                    **row,
                    "_ntfy_feedback_actions": build_feedback_actions(
                        base_url=str(ntfy["base_url"]),
                        feedback_topic=str(feedback_topic),
                        signing_key=str(signing_key),
                        public_id=str(target["public_id"]),
                        expires_at=int(target["expires_at"]),
                    ),
                }
            )
        return tuple(prepared)

    def _with_source_context(self, row: dict[str, Any]) -> dict[str, Any]:
        row_id = row.get("id")
        chat_type = (
            self._database.message_chat_type(int(row_id))
            if row_id is not None and row.get("content_kind") == "benefit_deal"
            else "unknown"
        )
        return {**row, "_source_chat_type": chat_type}

    async def send_immediate(self, row: dict[str, Any]) -> bool:
        row = self._with_source_context(row)
        return await self._send(
            telegram_chunks=tuple(immediate_chunks(row)),
            ntfy_rows=(row,),
        )

    async def send_digest(self, rows: Iterable[dict[str, Any]]) -> bool:
        materialized_rows = tuple(self._with_source_context(row) for row in rows)
        if not materialized_rows:
            return True
        return await self._send(
            telegram_chunks=tuple(digest_chunks(list(materialized_rows))),
            ntfy_rows=materialized_rows,
        )

    async def send_channel(
        self,
        channel_name: str,
        delivery_type: str,
        rows: Iterable[dict[str, Any]],
    ) -> bool:
        """Send exactly one persisted channel unit without touching other channels."""
        materialized_rows = tuple(self._with_source_context(row) for row in rows)
        if not materialized_rows:
            return True
        if channel_name not in {"telegram", "ntfy"}:
            raise ValueError("未知推送渠道")
        if delivery_type not in {"immediate", "digest"}:
            raise ValueError("未知投递类型")
        async with self._lock:
            await self._refresh_channels()
            channel = next(
                (value for name, value in self._channels if name == channel_name),
                None,
            )
            if channel is None:
                LOGGER.warning("推送渠道未启用或配置不完整：%s", channel_name)
                return False
            if channel_name == "telegram":
                chunks = (
                    tuple(immediate_chunks(materialized_rows[0]))
                    if delivery_type == "immediate"
                    else tuple(digest_chunks(list(materialized_rows)))
                )
                return await channel.send_chunks(chunks)
            return await channel.send_rows(self._with_ntfy_feedback(materialized_rows))

    async def _send(
        self,
        *,
        telegram_chunks: tuple[str, ...],
        ntfy_rows: tuple[dict[str, Any], ...],
    ) -> bool:
        async with self._lock:
            await self._refresh_channels()
            if not self._channels:
                LOGGER.warning("没有已启用且配置完整的推送渠道")
                return False
            names = [name for name, _ in self._channels]
            feedback_rows = self._with_ntfy_feedback(ntfy_rows)
            sends = [
                channel.send_chunks(telegram_chunks)
                if name == "telegram"
                else channel.send_rows(feedback_rows)
                for name, channel in self._channels
            ]
            results = await asyncio.gather(
                *sends,
                return_exceptions=True,
            )
        any_success = False
        for name, result in zip(names, results, strict=True):
            if result is True:
                any_success = True
            elif isinstance(result, Exception):
                LOGGER.error("%s 推送异常：%s", name, type(result).__name__)
            else:
                LOGGER.error("%s 推送失败", name)
        return any_success


async def send_push_test(config: dict[str, Any], channel_name: str) -> bool:
    test_message = "🔔 <b>群讯雷达推送测试</b>\n渠道配置可用。"
    if channel_name == "telegram":
        telegram = config["telegram"]
        if not telegram["enabled"] or not telegram.get("bot_token") or not telegram["chat_id"]:
            raise ValueError("Telegram Push Bot 尚未启用或配置不完整")
        channel: BotPusher | NtfyPusher = BotPusher(
            str(telegram["bot_token"]), str(telegram["chat_id"])
        )
    elif channel_name == "ntfy":
        ntfy = config["ntfy"]
        if (
            not ntfy["enabled"]
            or not ntfy["topic"]
            or not ntfy["community_topic"]
            or not ntfy["benefit_topic"]
        ):
            raise ValueError("ntfy 尚未启用或配置不完整")
        channel = NtfyPusher(
            str(ntfy["base_url"]),
            str(ntfy["topic"]),
            str(ntfy["access_token"]) if ntfy.get("access_token") else None,
            content_topics={
                "news": str(ntfy["topic"]),
                "community_signal": str(ntfy["community_topic"]),
                "benefit_deal": str(ntfy["benefit_topic"]),
            },
        )
    else:
        raise ValueError("未知推送渠道")
    try:
        if channel_name == "telegram":
            return await channel.send_chunks((test_message,))
        return await channel.send_test()
    finally:
        await channel.close()
