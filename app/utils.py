from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any

from telethon.tl import types
from telethon.utils import get_display_name


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def display_name(entity: Any, fallback_id: int | None = None) -> str:
    if entity is not None:
        name = get_display_name(entity).strip()
        username = getattr(entity, "username", None)
        if name:
            return name
        if username:
            return f"@{username}"
    return str(fallback_id) if fallback_id is not None else "未知"


def message_link(chat_id: int, message_id: int, chat_entity: Any) -> str | None:
    username = getattr(chat_entity, "username", None)
    if username and USERNAME_RE.fullmatch(username):
        return f"https://t.me/{username}/{message_id}"

    if isinstance(chat_entity, types.Channel):
        internal_id = abs(chat_id) - 1_000_000_000_000
        if internal_id > 0:
            return f"https://t.me/c/{internal_id}/{message_id}"
    return None


def extract_processable_text(message: Any) -> str:
    """Extract text metadata without downloading Telegram media."""
    parts: list[str] = []
    raw_text = (getattr(message, "raw_text", None) or "").strip()
    if raw_text:
        parts.append(raw_text)

    media = getattr(message, "media", None)
    webpage = getattr(media, "webpage", None)
    if webpage is not None:
        title = (getattr(webpage, "title", None) or "").strip()
        url = (getattr(webpage, "url", None) or "").strip()
        if title and title not in parts:
            parts.append(f"链接标题：{title}")
        if url and url not in raw_text:
            parts.append(url)

    file_obj = getattr(message, "file", None)
    filename = (getattr(file_obj, "name", None) or "").strip() if file_obj else ""
    if filename:
        safe_name = PurePath(filename).name
        if safe_name and safe_name not in raw_text:
            parts.append(f"文件名：{safe_name}")

    return "\n".join(parts).strip()


def clean_one_line(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"
