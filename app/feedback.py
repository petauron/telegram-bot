from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


FEEDBACK_WINDOW_DAYS = 90
FEEDBACK_RECORD_SCAN_LIMIT = 500
FEEDBACK_MAX_INTERESTS = 12
FEEDBACK_MAX_TOPIC_PREFERENCES = 20
FEEDBACK_TOPIC_TITLE_LENGTH = 120
FEEDBACK_CONTENT_LABELS = {
    "news": "新闻资讯",
    "community_signal": "社区线索",
    "benefit_deal": "福利羊毛",
}


def feedback_source_key(
    *,
    source_type: object,
    source_id: object,
    chat_id: object,
) -> str:
    source = str(source_type or "telegram")[:40]
    identity = source_id if source_id is not None else chat_id
    material = f"{source}:{identity}".encode("utf-8", errors="ignore")
    return hashlib.sha256(material).hexdigest()


def feedback_interest_tags(
    text: object,
    keywords: Iterable[object],
) -> tuple[str, ...]:
    haystack = str(text or "").casefold()
    matched: list[str] = []
    seen: set[str] = set()
    for raw_keyword in keywords:
        keyword = str(raw_keyword or "").strip()
        normalized = keyword.casefold()
        if not normalized or normalized in seen or normalized not in haystack:
            continue
        seen.add(normalized)
        matched.append(keyword[:40])
        if len(matched) >= FEEDBACK_MAX_INTERESTS:
            break
    return tuple(matched)


def decode_feedback_tags(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(
        str(item)[:40]
        for item in parsed[:FEEDBACK_MAX_INTERESTS]
        if isinstance(item, str) and item.strip()
    )


def _vote_counts(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    up = 0
    down = 0
    for record in records:
        if record.get("vote") == "up":
            up += 1
        elif record.get("vote") == "down":
            down += 1
    return {"up": up, "down": down, "total": up + down}


@dataclass(frozen=True, slots=True)
class FeedbackGuidance:
    sample_count: int
    summary: str
    payload: dict[str, Any]


def build_feedback_guidance(
    records: Iterable[dict[str, Any]],
    *,
    content_kind: str,
    source_key: str,
    current_tags: Iterable[str],
) -> FeedbackGuidance:
    bounded = tuple(records)[:FEEDBACK_RECORD_SCAN_LIMIT]
    del content_kind, source_key, current_tags
    topic_preferences: list[dict[str, str]] = []
    for index, record in enumerate(
        bounded[:FEEDBACK_MAX_TOPIC_PREFERENCES], start=1
    ):
        vote = str(record.get("vote") or "")
        title = " ".join(str(record.get("title") or "").split())
        if vote not in {"up", "down"} or not title:
            continue
        topic_preferences.append(
            {
                "id": f"p{index}",
                "vote": vote,
                "topic": title[:FEEDBACK_TOPIC_TITLE_LENGTH],
            }
        )
    sample_count = len(topic_preferences)
    if sample_count == 0:
        summary = "暂无可用于具体主题匹配的有效反馈，评分不做偏好校准"
    else:
        counts = _vote_counts(topic_preferences)
        summary = (
            f"近 {FEEDBACK_WINDOW_DAYS} 天有 {sample_count} 条具体主题反馈"
            f"（{counts['up']} 赞/{counts['down']} 踩）；仅同一具体产品或主题才校准"
        )
    return FeedbackGuidance(
        sample_count=sample_count,
        summary=summary[:240],
        payload={
            "window_days": FEEDBACK_WINDOW_DAYS,
            "topic_sample_count": sample_count,
            "topic_preferences": topic_preferences,
            "calibration_policy": "same_specific_topic",
        },
    )
