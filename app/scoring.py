from __future__ import annotations

import re
from dataclasses import dataclass


URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+|\bt\.me/[A-Za-z0-9_+/?=&.-]+")
CVE_RE = re.compile(r"(?i)\bCVE-\d{4}-\d{4,7}\b")
IP_RE = re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])")
VERSION_RE = re.compile(r"(?i)(?<![\w.])v?\d+(?:\.\d+){1,3}(?:[-+_][a-z0-9.]+)?(?![\w.])")
MONEY_RE = re.compile(
    r"(?i)(?:[$€£¥￥]\s?\d[\d,.]*|\d[\d,.]*\s?(?:元|块|美元|人民币|USD|CNY|USDT|EUR|GBP))"
)
DATE_RE = re.compile(
    r"(?:\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b|"
    r"\b\d{1,2}[-/.月]\d{1,2}日?\b|"
    r"\b\d{1,2}:\d{2}\b|"
    r"(?:今天|明天|本周|下周|月底|年底).{0,8}(?:前|内|截止)?|"
    r"截止(?:时间|日期)?\s*[:：]?\s*\S{1,24})"
)

ACTION_WORDS = ("维护", "恢复", "故障", "发布", "升级", "截止")
AD_PATTERNS = (
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"返佣|返利|推广|代理招募|拉人头|邀请.{0,8}奖励",
        r"稳赚|保本高收益|带单|跟单群|内部群",
        r"加群|进群|扫码.{0,6}(?:入群|添加)|邀请码",
        r"客服.{0,12}(?:微信|WeChat|QQ)|私聊.{0,8}(?:领取|咨询)",
        r"空投.{0,12}(?:领取|福利)|注册送|充值返",
    )
)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: int
    reasons: tuple[str, ...]
    normalized_text: str
    primary_url: str | None


def normalize_text(text: str) -> str:
    value = URL_RE.sub(" <url> ", text.casefold())
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\u3400-\u9fff<>]+", "", value)
    return value[:2000]


def first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?，。；：！？)]}>'\"")


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    folded = text.casefold()
    return [keyword for keyword in keywords if keyword.casefold() in folded]


def score_message(
    text: str,
    *,
    mentioned_me: bool,
    reply_to_me: bool,
    trusted_sender: bool,
    keywords: tuple[str, ...],
    repeated_recently: bool = False,
) -> ScoreResult:
    score = 0
    reasons: list[str] = []

    if mentioned_me:
        score += 100
        reasons.append("提及你 +100")

    if reply_to_me:
        score += 100
        reasons.append("回复你的消息 +100")

    if trusted_sender:
        score += 35
        reasons.append("可信发送者 +35")

    keyword_hits = _keyword_hits(text, keywords)
    if keyword_hits:
        keyword_score = min(60, 25 + max(0, len(keyword_hits) - 1) * 10)
        score += keyword_score
        shown = "、".join(keyword_hits[:6])
        if len(keyword_hits) > 6:
            shown += f"等 {len(keyword_hits)} 个"
        reasons.append(f"重要关键词（{shown}） +{keyword_score}")

    structured: list[str] = []
    if CVE_RE.search(text):
        structured.append("CVE")
    if IP_RE.search(text):
        structured.append("IP")
    if VERSION_RE.search(text):
        structured.append("版本号")
    if MONEY_RE.search(text):
        structured.append("金额")
    if DATE_RE.search(text):
        structured.append("日期/截止时间")
    if structured:
        score += 15
        reasons.append(f"包含{'、'.join(structured)} +15")

    primary_url = first_url(text)
    if primary_url:
        score += 10
        reasons.append("包含链接 +10")

    action_hits = [word for word in ACTION_WORDS if word in text]
    if action_hits:
        score += 15
        reasons.append(f"行动信息（{'、'.join(action_hits)}） +15")

    if any(pattern.search(text) for pattern in AD_PATTERNS):
        score -= 35
        reasons.append("疑似广告/返佣/拉人内容 -35")

    if repeated_recently:
        score -= 25
        reasons.append("近期重复推广/重复内容 -25")

    return ScoreResult(
        score=score,
        reasons=tuple(reasons),
        normalized_text=normalize_text(text),
        primary_url=primary_url,
    )


def reply_bonus(reply_count: int) -> int:
    """Score bonus for distinct repliers in the current digest window."""
    if reply_count >= 5:
        return 30
    if reply_count >= 3:
        return 20
    if reply_count >= 2:
        return 10
    return 0
