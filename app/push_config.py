from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


DEFAULT_NTFY_BASE_URL = "https://ntfy.example.com"
MAX_PUSH_SECRET_LENGTH = 2_048
MAX_PUSH_CHAT_ID_LENGTH = 128
MAX_NTFY_TOPIC_LENGTH = 128
_NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_ntfy_base_url(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > 512 or any(ord(character) < 32 for character in raw):
        raise ValueError("ntfy 服务地址无效")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ntfy 服务地址无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in raw
    ):
        raise ValueError("ntfy 服务地址只允许不含凭据、查询参数和片段的 HTTP/HTTPS URL")
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))


def validate_ntfy_topic(value: str, *, required: bool = False) -> str:
    topic = value.strip()
    if not topic:
        if required:
            raise ValueError("启用 ntfy 前必须填写 Topic")
        return ""
    if len(topic) > MAX_NTFY_TOPIC_LENGTH or not _NTFY_TOPIC_RE.fullmatch(topic):
        raise ValueError("ntfy Topic 只能包含 1–128 位英文字母、数字、下划线或连字符")
    return topic


def validate_push_secret(value: str, *, label: str) -> str:
    secret = value.strip()
    if len(secret) > MAX_PUSH_SECRET_LENGTH:
        raise ValueError(f"{label} 长度不能超过 {MAX_PUSH_SECRET_LENGTH} 个字符")
    return secret


def validate_push_chat_id(value: str, *, required: bool = False) -> str:
    chat_id = value.strip()
    if not chat_id:
        if required:
            raise ValueError("启用 Telegram Push Bot 前必须填写接收聊天 ID")
        return ""
    if len(chat_id) > MAX_PUSH_CHAT_ID_LENGTH:
        raise ValueError("Telegram 接收聊天 ID 过长")
    return chat_id
