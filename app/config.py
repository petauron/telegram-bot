from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.secrets import read_setting


DEFAULT_KEYWORDS = (
    "宕机,服务中断,停服,被墙,故障,维护,服务异常,服务恢复,漏洞,CVE,零日,0day,"
    "数据泄露,供应链攻击,安全事件,重大更新,版本发布,正式发布,开源,涨价,"
    "价格调整,退款政策,政策变更,封禁政策,新规,制裁"
)


def _csv_ints(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise ValueError(f"{name} 必须是逗号分隔的整数") from exc
    return frozenset(values)


def _csv_strings(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def _required(name: str) -> str:
    value = read_setting(name).strip()
    if not value:
        raise ValueError(f"缺少必填环境变量 {name}")
    return value


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session_path: str
    push_bot_token: str
    push_chat_id: str
    watch_chat_ids: frozenset[int]
    trusted_sender_ids: frozenset[int]
    important_keywords: tuple[str, ...]
    immediate_score: int
    analysis_workers: int
    analysis_batch_target: int
    analysis_batch_max_items: int
    analysis_batch_max_chars: int
    analysis_batch_max_wait_seconds: int
    database_path: str
    retention_days: int

    @classmethod
    def from_env(
        cls,
        *,
        require_push: bool = True,
        require_watch: bool = True,
    ) -> "Settings":
        api_id_raw = _required("TG_API_ID")
        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise ValueError("TG_API_ID 必须是整数") from exc

        watch_chat_ids = _csv_ints("WATCH_CHAT_IDS")
        if require_watch and not watch_chat_ids:
            raise ValueError("WATCH_CHAT_IDS 至少需要一个群或频道 ID")

        bot_token = read_setting("PUSH_BOT_TOKEN").strip()
        push_chat_id = os.getenv("PUSH_CHAT_ID", "").strip()
        if require_push and not bot_token:
            raise ValueError("缺少必填环境变量 PUSH_BOT_TOKEN")
        if require_push and not push_chat_id:
            raise ValueError("缺少必填环境变量 PUSH_CHAT_ID")

        analysis_batch_target = _positive_int("ANALYSIS_BATCH_TARGET", 20)
        analysis_batch_max_items = _positive_int("ANALYSIS_BATCH_MAX_ITEMS", 50)
        if analysis_batch_target > analysis_batch_max_items:
            raise ValueError("ANALYSIS_BATCH_TARGET 不能大于 ANALYSIS_BATCH_MAX_ITEMS")

        return cls(
            tg_api_id=api_id,
            tg_api_hash=_required("TG_API_HASH"),
            tg_phone=_required("TG_PHONE"),
            tg_session_path=os.getenv("TG_SESSION_PATH", "/session/telegram").strip()
            or "/session/telegram",
            push_bot_token=bot_token,
            push_chat_id=push_chat_id,
            watch_chat_ids=watch_chat_ids,
            trusted_sender_ids=_csv_ints("TRUSTED_SENDER_IDS"),
            important_keywords=_csv_strings("IMPORTANT_KEYWORDS", DEFAULT_KEYWORDS),
            immediate_score=60,
            analysis_workers=_positive_int("ANALYSIS_WORKERS", 8),
            analysis_batch_target=analysis_batch_target,
            analysis_batch_max_items=analysis_batch_max_items,
            analysis_batch_max_chars=_positive_int(
                "ANALYSIS_BATCH_MAX_CHARS", 12_000, minimum=8_100
            ),
            analysis_batch_max_wait_seconds=_positive_int(
                "ANALYSIS_BATCH_MAX_WAIT_SECONDS", 2
            ),
            database_path=os.getenv("DATABASE_PATH", "/database/messages.db").strip()
            or "/database/messages.db",
            retention_days=_positive_int("RETENTION_DAYS", 3),
        )

    def ensure_parent_directories(self, *, include_database: bool = True) -> None:
        Path(self.tg_session_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if include_database:
            Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
