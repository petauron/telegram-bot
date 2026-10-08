from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from app.prefilter import evaluate_prefilter


RECENT_CONTEXT_RECENT_LIMIT = 12
RECENT_CONTEXT_RELEVANT_LIMIT = 8
RECENT_CONTEXT_LIMIT = 20
RECENT_CONTEXT_CHAR_LIMIT = 10_000
RECENT_CONTEXT_SCAN_LIMIT = 200
RECENT_CONTEXT_RELEVANCE_HOURS = 24
COMMUNITY_CONTEXT_THREAD_LIMIT = 20
COMMUNITY_CONTEXT_RECENT_LIMIT = 20
COMMUNITY_CONTEXT_RELEVANT_LIMIT = 10
COMMUNITY_CONTEXT_LIMIT = 30
COMMUNITY_CONTEXT_CHAR_LIMIT = 12_000
COMMUNITY_CONTEXT_SCAN_LIMIT = 300
COMMUNITY_CONTEXT_THREAD_HOURS = 2
COMMUNITY_CONTEXT_RECENT_MINUTES = 45
COMMUNITY_CONTEXT_RELEVANCE_HOURS = 6
COMMUNITY_INCIDENT_CLUSTER_MINUTES = 15
COMMUNITY_PRODUCT_REVIEW_HOURS = 2
SESSION_NAMESPACE_BYTES = 32
SESSION_KEY_PREFIX = "tgchat-v1-"

_ELIGIBLE_HISTORY_STATUSES = frozenset({"success"})
_ELIGIBLE_HISTORY_CATEGORIES = frozenset({"external_information"})
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._:+/-]{1,63}", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_COMMUNITY_INCIDENT_CUE_RE = re.compile(
    r"(?:宕机|中断|故障|挂了|炸了|崩了|崩溃|掉线|断流|无响应|打不开|连不上|"
    r"无法访问|不可访问|无法连接|连接失败|超时|丢包|抖动|延迟|延时|限速|限流|"
    r"恢复了|修好了|正常了|能用了|又好了|不兼容|不能用|失效)",
    re.IGNORECASE,
)
_COMMUNITY_CORROBORATION_RE = re.compile(
    r"(?:我这(?:里|边)?也|我也|这边也是|同样|确实|也是这样|\+1|又炸|又挂|又崩)",
    re.IGNORECASE,
)
_COMMUNITY_EVIDENCE_RE = re.compile(
    r"(?:\d|ms\b|错误码|日志|截图|实测|复现|持续|多次|官网|客户端|节点|线路|"
    r"API\b|版本|仓库|代码)",
    re.IGNORECASE,
)
_COMMUNITY_ACTION_RESULT_RE = re.compile(
    r"(?:调整|修改|切换|更换|重启|重装|清理|关闭|打开|回滚|修复|设置|配置)"
    r".{0,40}(?:成功|有效|可用|恢复|正常|(?:已)?解决(?:了|问题)|不再|修好)",
    re.IGNORECASE,
)
_COMMUNITY_ASCII_SUBJECT_RE = re.compile(
    r"\b(?!https?\b)[a-z][a-z0-9._+-]{1,31}\b", re.IGNORECASE
)
_COMMUNITY_CJK_SUBJECT_RE = re.compile(
    r"([\u3400-\u9fff]{2,16})(?:疑似|好像|是不是|又|已经|都)?"
    r"(?:宕机|中断|故障|挂了|炸了|崩了|崩溃|掉线|断流|无响应|打不开|连不上|"
    r"无法访问|无法连接|连接失败|超时|丢包|限速|限流|恢复了|修好了|正常了|能用了|失效)",
    re.IGNORECASE,
)
_COMMUNITY_GENERIC_SUBJECTS = frozenset(
    {
        "估计",
        "好像",
        "感觉",
        "是不是",
        "怎么",
        "咋",
        "真的",
        "现在",
        "今天",
        "刚才",
        "刚刚",
        "这边",
        "那边",
        "总不至于",
        "全部",
        "全都",
        "又",
    }
)
_PRODUCT_FAMILY_PATTERNS = (
    ("vps", re.compile(r"\bvps\b|云服务器|云主机|虚拟服务器|独服|小鸡", re.IGNORECASE)),
    (
        "proxy_service",
        re.compile(
            r"机场|梯子|代理服务|订阅(?:链接|地址|套餐)|流媒体解锁|"
            r"(?:节点|线路).{0,16}(?:倍率|流量|解锁|延迟|丢包|中转|直连)",
            re.IGNORECASE,
        ),
    ),
    (
        "gpt",
        re.compile(
            r"\bgpt(?:s|[-\s]?\d(?:\.\d+)*(?:-[a-z0-9]+)?)?\b|chatgpt|openai",
            re.IGNORECASE,
        ),
    ),
    ("claude", re.compile(r"\bclaude(?:[-\s]?\d(?:\.\d+)*)?\b|anthropic", re.IGNORECASE)),
    ("gemini", re.compile(r"\bgemini(?:[-\s]?\d(?:\.\d+)*)?\b", re.IGNORECASE)),
)
_PRODUCT_NAMED_ASCII_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9._-]{2,31}\b")
_PRODUCT_CJK_NAME_RE = re.compile(
    r"([\u3400-\u9fffA-Za-z0-9._-]{2,18})(?:机场|VPS|vps|模型|云服务|工具)",
)
_PRODUCT_NAME_STOPWORDS = frozenset(
    {
        "api",
        "http",
        "https",
        "www",
        "com",
        "cpu",
        "ram",
        "linux",
        "windows",
        "android",
        "ios",
        "telegram",
        "github",
        "这个",
        "那个",
        "某个",
        "某产品",
        "一种",
    }
)
_PRODUCT_REVIEW_EXPERIENCE_RE = re.compile(
    r"我(?:在)?用|我用过|用过|用了|买了|购入|续费|实测|体验|测试了|"
    r"对比过|订阅了|开了\s*(?:Plus|Pro)|工单|退款",
    re.IGNORECASE,
)
_PRODUCT_REVIEW_RECOMMENDATION_RE = re.compile(
    r"不推荐|我(?:更)?推荐|推荐(?:买|用|选|入|续费|这个|它)|"
    r"值得|不值得|好用|不好用|别买|避雷|可以入|不要买",
    re.IGNORECASE,
)
_PRODUCT_REVIEW_QUESTION_RE = re.compile(
    r"[?？]|怎么样|如何|求推荐|有没有|哪家|哪个好|能买吗|靠谱吗|稳不稳|"
    r"好不好|值得吗|有人用",
    re.IGNORECASE,
)
_PRODUCT_REVIEW_DIMENSIONS = (
    ("performance", re.compile(r"延迟|速度|带宽|丢包|抖动|性能|跑分|IO\b|CPU|内存|限速", re.IGNORECASE)),
    ("stability", re.compile(r"稳定|不稳|掉线|断流|宕机|炸了|挂了|恢复|被墙", re.IGNORECASE)),
    ("price", re.compile(r"价格|性价比|便宜|贵了|太贵|续费|涨价|降价|套餐|流量|倍率", re.IGNORECASE)),
    ("support", re.compile(r"客服|工单|售后|退款|响应", re.IGNORECASE)),
    ("trust", re.compile(r"跑路|诈骗|可信|口碑|黑店|避雷|风控|封号", re.IGNORECASE)),
    ("access", re.compile(r"解锁|Netflix|流媒体|ChatGPT|中转|直连|专线|线路", re.IGNORECASE)),
    ("quality", re.compile(r"效果|质量|推理|编程|代码|上下文|幻觉|中文|写作|识图|多模态|更强|更差|不如", re.IGNORECASE)),
)


@dataclass(frozen=True, slots=True)
class CommunityGateEvidence:
    """Locally computed conversation structure; never exposes participant identities."""

    same_thread_support_count: int = 0
    related_context_count: int = 0
    incident_message_count: int = 0
    distinct_participant_count: int = 0
    subject_anchor_present: bool = False
    multi_participant_incident: bool = False
    product_anchor_present: bool = False
    product_review_message_count: int = 0
    product_review_participant_count: int = 0
    product_review_dimension_count: int = 0
    multi_participant_product_review: bool = False

    def model_payload(self) -> dict[str, int | bool]:
        return {
            "same_thread_support_count": min(30, max(0, self.same_thread_support_count)),
            "related_context_count": min(30, max(0, self.related_context_count)),
            "incident_message_count": min(30, max(0, self.incident_message_count)),
            "distinct_participant_count": min(
                30, max(0, self.distinct_participant_count)
            ),
            "subject_anchor_present": bool(self.subject_anchor_present),
            "multi_participant_incident": bool(self.multi_participant_incident),
            "product_anchor_present": bool(self.product_anchor_present),
            "product_review_message_count": min(
                30, max(0, self.product_review_message_count)
            ),
            "product_review_participant_count": min(
                30, max(0, self.product_review_participant_count)
            ),
            "product_review_dimension_count": min(
                len(_PRODUCT_REVIEW_DIMENSIONS),
                max(0, self.product_review_dimension_count),
            ),
            "multi_participant_product_review": bool(
                self.multi_participant_product_review
            ),
        }


def derive_chat_session_key(namespace: bytes, chat_id: int) -> str:
    """Derive a stable opaque per-chat key without exposing the Telegram chat ID."""
    if len(namespace) != SESSION_NAMESPACE_BYTES:
        raise ValueError("模型会话命名空间无效")
    message = b"telegram-llm-chat:v1\x00" + str(int(chat_id)).encode("ascii")
    digest = hmac.new(namespace, message, hashlib.sha256).hexdigest()
    return f"{SESSION_KEY_PREFIX}{digest}"


def derive_dedupe_session_key(namespace: bytes) -> str:
    """Derive one opaque, stable identity for cross-source event comparison."""
    if len(namespace) != SESSION_NAMESPACE_BYTES:
        raise ValueError("模型会话命名空间无效")
    digest = hmac.new(
        namespace,
        b"telegram-news-semantic-dedupe:v1",
        hashlib.sha256,
    ).hexdigest()
    return f"{SESSION_KEY_PREFIX}{digest}"


def is_eligible_history(
    row: Mapping[str, Any],
    *,
    protected_keywords: tuple[str, ...] = (),
) -> bool:
    """Return whether a persisted message may be exposed as model context."""
    if bool(row.get("is_service_message")):
        return False
    if row.get("ai_status") not in _ELIGIBLE_HISTORY_STATUSES:
        return False
    if row.get("ai_category") not in _ELIGIBLE_HISTORY_CATEGORIES:
        return False
    if row.get("prefilter_status") != "passed":
        return False
    text = str(row.get("text") or "").strip()
    if not text or evaluate_prefilter(
        text,
        protected_keywords=protected_keywords,
    ).filtered:
        return False
    return True


def build_recent_context(
    rows_newest_first: Iterable[Mapping[str, Any]],
    *,
    current_text: str = "",
    relevant_rows_newest_first: Iterable[Mapping[str, Any]] | None = None,
    recent_limit: int = RECENT_CONTEXT_RECENT_LIMIT,
    relevant_limit: int = RECENT_CONTEXT_RELEVANT_LIMIT,
    limit: int = RECENT_CONTEXT_LIMIT,
    character_limit: int = RECENT_CONTEXT_CHAR_LIMIT,
    protected_keywords: tuple[str, ...] = (),
) -> tuple[dict[str, str], ...]:
    """Select recent plus locally relevant history, then return it chronologically."""
    if limit < 1 or character_limit < 1:
        return ()

    eligible_recent = [
        row
        for row in rows_newest_first
        if is_eligible_history(row, protected_keywords=protected_keywords)
    ]
    recent = eligible_recent[: max(0, min(recent_limit, limit))]
    selected_keys = {_row_identity(row) for row in recent}

    relevant_source = (
        relevant_rows_newest_first
        if relevant_rows_newest_first is not None
        else eligible_recent[len(recent) :]
    )
    ranked: list[tuple[float, str, tuple[Any, ...], Mapping[str, Any]]] = []
    for row in relevant_source:
        if not is_eligible_history(row, protected_keywords=protected_keywords):
            continue
        identity = _row_identity(row)
        if identity in selected_keys:
            continue
        score = relevance_score(current_text, str(row.get("text") or ""))
        if score <= 0:
            continue
        ranked.append(
            (
                score,
                str(row.get("sent_at") or row.get("created_at") or ""),
                identity,
                row,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    relevant: list[Mapping[str, Any]] = []
    for _, _, identity, row in ranked:
        if identity in selected_keys:
            continue
        relevant.append(row)
        selected_keys.add(identity)
        if len(relevant) >= max(0, min(relevant_limit, limit - len(recent))):
            break

    # Spend the shared character budget in selection-priority order: newest
    # eligible rows first, then the strongest deterministic matches.
    bounded: list[tuple[str, tuple[Any, ...], dict[str, str]]] = []
    used_characters = 0
    for row in (*recent, *relevant):
        sent_at = str(row.get("sent_at") or row.get("created_at") or "")[:64]
        text = str(row.get("text") or "").strip()
        remaining = character_limit - used_characters - len(sent_at)
        if remaining <= 0:
            break
        bounded_text = text[:remaining].rstrip()
        if not bounded_text:
            continue
        bounded.append(
            (
                sent_at,
                _row_identity(row),
                {"time": sent_at, "text": bounded_text},
            )
        )
        used_characters += len(sent_at) + len(bounded_text)
        if len(bounded) >= limit or len(bounded_text) < len(text):
            break

    bounded.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in bounded)


def community_has_incident_cue(value: str) -> bool:
    return bool(_COMMUNITY_INCIDENT_CUE_RE.search(str(value or "")))


def community_has_corroboration(value: str) -> bool:
    return bool(_COMMUNITY_CORROBORATION_RE.search(str(value or "")))


def community_has_concrete_evidence(value: str) -> bool:
    return bool(_COMMUNITY_EVIDENCE_RE.search(str(value or "")))


def community_has_action_result(value: str) -> bool:
    return bool(_COMMUNITY_ACTION_RESULT_RE.search(str(value or "")))


def community_has_subject_anchor(value: str) -> bool:
    normalized = _URL_RE.sub(" ", str(value or ""))
    if _COMMUNITY_ASCII_SUBJECT_RE.search(normalized):
        return True
    for match in _COMMUNITY_CJK_SUBJECT_RE.finditer(normalized):
        candidate = match.group(1).strip()
        # CJK matching can greedily include conversational prefixes. Reject
        # known subjectless phrases while still accepting product names.
        if candidate not in _COMMUNITY_GENERIC_SUBJECTS and not any(
            candidate.endswith(generic) for generic in _COMMUNITY_GENERIC_SUBJECTS
        ):
            return True
    return False


def product_subject_keys(value: str) -> frozenset[str]:
    """Extract conservative local product keys without exposing them to the model."""
    text = _URL_RE.sub(" ", str(value or ""))
    keys: set[str] = set()
    for label, pattern in _PRODUCT_FAMILY_PATTERNS:
        if pattern.search(text):
            keys.add(f"family:{label}")
    for match in _PRODUCT_NAMED_ASCII_RE.finditer(text):
        original = match.group(0)
        folded = original.casefold()
        if folded in _PRODUCT_NAME_STOPWORDS:
            continue
        # Product names in chat are usually capitalized, mixed-case, versioned,
        # dotted or hyphenated. Plain lowercase prose is too ambiguous.
        if (
            any(character.isupper() for character in original)
            or any(character.isdigit() for character in original)
            or "." in original
            or "-" in original
        ):
            keys.add(f"name:{folded[:32]}")
    for match in _PRODUCT_CJK_NAME_RE.finditer(text):
        candidate = match.group(1).strip().casefold()
        if candidate and candidate not in _PRODUCT_NAME_STOPWORDS:
            keys.add(f"name:{candidate[:32]}")
    return frozenset(keys)


def product_review_dimensions(value: str) -> frozenset[str]:
    text = str(value or "")
    return frozenset(
        label for label, pattern in _PRODUCT_REVIEW_DIMENSIONS if pattern.search(text)
    )


def community_has_product_review(value: str) -> bool:
    """Return whether a turn contains an evaluation rather than a bare question."""
    text = " ".join(str(value or "").split())
    dimensions = product_review_dimensions(text)
    if not dimensions:
        return False
    has_experience = bool(_PRODUCT_REVIEW_EXPERIENCE_RE.search(text))
    has_recommendation = bool(_PRODUCT_REVIEW_RECOMMENDATION_RE.search(text))
    if _PRODUCT_REVIEW_QUESTION_RE.search(text) and not (
        has_experience or has_recommendation
    ):
        return False
    return bool(has_experience or has_recommendation or len(text) >= 6)


def build_community_gate_evidence(
    rows_newest_first: Iterable[Mapping[str, Any]],
    *,
    current_row: Mapping[str, Any],
    target_time: datetime,
    protected_keywords: tuple[str, ...] = (),
) -> CommunityGateEvidence:
    """Summarize same-thread and multi-participant incident corroboration locally."""
    eligible: list[Mapping[str, Any]] = []
    for row in rows_newest_first:
        if bool(row.get("is_service_message")):
            continue
        if row.get("prefilter_status") != "passed":
            continue
        if row.get("ai_category") != "discussion":
            continue
        if row.get("community_status") not in {"filtered", "valuable"}:
            continue
        text = str(row.get("text") or "").strip()
        if not text or evaluate_prefilter(
            text,
            protected_keywords=protected_keywords,
        ).filtered:
            continue
        sent_at = _parse_context_time(row.get("sent_at") or row.get("created_at"))
        if sent_at is None or sent_at > target_time:
            continue
        eligible.append(row)

    current_text = str(current_row.get("text") or "").strip()
    current_thread = int(current_row.get("thread_root_id") or 0)
    thread_since = target_time - timedelta(hours=COMMUNITY_CONTEXT_THREAD_HOURS)
    cluster_since = target_time - timedelta(minutes=COMMUNITY_INCIDENT_CLUSTER_MINUTES)
    related_since = target_time - timedelta(minutes=COMMUNITY_CONTEXT_RECENT_MINUTES)

    same_thread_rows = [
        row
        for row in eligible
        if int(row.get("thread_root_id") or 0) == current_thread
        and (_parse_context_time(row.get("sent_at") or row.get("created_at")) or target_time)
        >= thread_since
        and (
            community_has_incident_cue(str(row.get("text") or ""))
            or community_has_corroboration(str(row.get("text") or ""))
            or community_has_action_result(str(row.get("text") or ""))
        )
    ]
    recent_rows = [
        row
        for row in eligible
        if (_parse_context_time(row.get("sent_at") or row.get("created_at")) or target_time)
        >= cluster_since
        and (
            community_has_incident_cue(str(row.get("text") or ""))
            or community_has_corroboration(str(row.get("text") or ""))
        )
    ]
    related_count = sum(
        1
        for row in eligible
        if (_parse_context_time(row.get("sent_at") or row.get("created_at")) or target_time)
        >= related_since
        and relevance_score(current_text, str(row.get("text") or "")) >= 0.18
    )

    incident_rows: list[Mapping[str, Any]] = list(recent_rows)
    if community_has_incident_cue(current_text) or community_has_corroboration(
        current_text
    ):
        incident_rows.append(current_row)
    participants = {
        key
        for row in incident_rows
        if (key := _participant_identity(row)) is not None
    }
    subject_anchor = any(
        community_has_subject_anchor(str(row.get("text") or ""))
        for row in incident_rows
    )
    incident_count = len(incident_rows)
    participant_count = len(participants)

    review_since = target_time - timedelta(hours=COMMUNITY_PRODUCT_REVIEW_HOURS)
    review_candidates = [
        row
        for row in eligible
        if (_parse_context_time(row.get("sent_at") or row.get("created_at")) or target_time)
        >= review_since
        and community_has_product_review(str(row.get("text") or ""))
    ]
    current_is_review = community_has_product_review(current_text)
    current_keys = product_subject_keys(current_text)
    same_thread_keys: set[str] = set(current_keys)
    for row in eligible:
        sent_at = _parse_context_time(row.get("sent_at") or row.get("created_at"))
        if (
            sent_at is not None
            and sent_at >= review_since
            and int(row.get("thread_root_id") or 0) == current_thread
        ):
            same_thread_keys.update(product_subject_keys(str(row.get("text") or "")))
    effective_keys = frozenset(current_keys or same_thread_keys)

    related_review_rows: list[Mapping[str, Any]] = []
    for row in review_candidates:
        row_keys = product_subject_keys(str(row.get("text") or ""))
        same_thread = int(row.get("thread_root_id") or 0) == current_thread
        if same_thread and (effective_keys or row_keys):
            related_review_rows.append(row)
        elif effective_keys and row_keys:
            shared = effective_keys.intersection(row_keys)
            # A generic family such as “机场” or “VPS” must not merge reviews
            # of unrelated providers outside one reply thread.
            if any(key.startswith("name:") for key in shared):
                related_review_rows.append(row)
    if current_is_review:
        related_review_rows.append(current_row)
    review_participants = {
        key
        for row in related_review_rows
        if (key := _participant_identity(row)) is not None
    }
    review_dimensions = {
        dimension
        for row in related_review_rows
        for dimension in product_review_dimensions(str(row.get("text") or ""))
    }
    product_anchor = bool(effective_keys) or any(
        product_subject_keys(str(row.get("text") or ""))
        for row in related_review_rows
    )
    multi_product_review = bool(
        current_is_review
        and product_anchor
        and len(related_review_rows) >= 2
        and len(review_participants) >= 2
    )
    return CommunityGateEvidence(
        same_thread_support_count=len(same_thread_rows),
        related_context_count=related_count,
        incident_message_count=incident_count,
        distinct_participant_count=participant_count,
        subject_anchor_present=subject_anchor,
        multi_participant_incident=bool(
            subject_anchor and incident_count >= 2 and participant_count >= 2
        ),
        product_anchor_present=product_anchor,
        product_review_message_count=len(related_review_rows),
        product_review_participant_count=len(review_participants),
        product_review_dimension_count=len(review_dimensions),
        multi_participant_product_review=multi_product_review,
    )


def build_community_context(
    rows_newest_first: Iterable[Mapping[str, Any]],
    *,
    current_text: str,
    target_time: datetime,
    target_thread_root_id: int,
    protected_keywords: tuple[str, ...] = (),
) -> tuple[dict[str, str], ...]:
    """Build thread-first, recent, then locally related discussion context."""
    eligible: list[Mapping[str, Any]] = []
    for row in rows_newest_first:
        if bool(row.get("is_service_message")):
            continue
        if row.get("prefilter_status") != "passed":
            continue
        if row.get("ai_category") != "discussion":
            continue
        if row.get("community_status") not in {"filtered", "valuable"}:
            continue
        text = str(row.get("text") or "").strip()
        if not text or evaluate_prefilter(
            text,
            protected_keywords=protected_keywords,
        ).filtered:
            continue
        sent_at = _parse_context_time(row.get("sent_at") or row.get("created_at"))
        # The database query already applies the stable sent_at/message_id/id
        # boundary, so equal-second predecessors remain valid context.
        if sent_at is None or sent_at > target_time:
            continue
        eligible.append(row)

    thread_since = target_time - timedelta(hours=COMMUNITY_CONTEXT_THREAD_HOURS)
    cluster_since = target_time - timedelta(minutes=COMMUNITY_INCIDENT_CLUSTER_MINUTES)
    recent_since = target_time - timedelta(minutes=COMMUNITY_CONTEXT_RECENT_MINUTES)
    relevant_since = target_time - timedelta(hours=COMMUNITY_CONTEXT_RELEVANCE_HOURS)
    thread = [
        row
        for row in eligible
        if int(row.get("thread_root_id") or 0) == int(target_thread_root_id)
        and (_parse_context_time(row.get("sent_at") or row.get("created_at")) or target_time)
        >= thread_since
    ][:COMMUNITY_CONTEXT_THREAD_LIMIT]
    recent = [
        row
        for row in eligible
        if (_parse_context_time(row.get("sent_at") or row.get("created_at")) or target_time)
        >= recent_since
    ][:COMMUNITY_CONTEXT_RECENT_LIMIT]
    incident_cluster = [
        row
        for row in eligible
        if (_parse_context_time(row.get("sent_at") or row.get("created_at")) or target_time)
        >= cluster_since
        and (
            community_has_incident_cue(str(row.get("text") or ""))
            or community_has_corroboration(str(row.get("text") or ""))
            or community_has_action_result(str(row.get("text") or ""))
        )
    ][:COMMUNITY_CONTEXT_RECENT_LIMIT]

    selected: list[Mapping[str, Any]] = []
    selected_keys: set[tuple[Any, ...]] = set()

    def add(row: Mapping[str, Any]) -> bool:
        identity = _row_identity(row)
        if identity in selected_keys or len(selected) >= COMMUNITY_CONTEXT_LIMIT:
            return False
        selected.append(row)
        selected_keys.add(identity)
        return True

    for row in thread:
        add(row)
    # For outage-like chat, prioritize the nearby group corroboration needed to
    # resolve subjectless turns such as “我也” or “又炸了”.
    if community_has_incident_cue(current_text) or community_has_corroboration(
        current_text
    ):
        for row in incident_cluster:
            if len(selected) >= COMMUNITY_CONTEXT_RECENT_LIMIT:
                break
            add(row)
    # Thread, incident cluster and latest same-chat discussion form a bounded
    # conversational base. Returned model data still contains only time/text.
    for row in recent:
        if len(selected) >= COMMUNITY_CONTEXT_RECENT_LIMIT:
            break
        add(row)

    ranked: list[tuple[float, str, tuple[Any, ...], Mapping[str, Any]]] = []
    for row in eligible:
        identity = _row_identity(row)
        if identity in selected_keys:
            continue
        sent_at = _parse_context_time(row.get("sent_at") or row.get("created_at"))
        if sent_at is None or sent_at < relevant_since:
            continue
        score = relevance_score(current_text, str(row.get("text") or ""))
        if score <= 0:
            continue
        ranked.append((score, str(row.get("sent_at") or ""), identity, row))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    for _, _, _, row in ranked[:COMMUNITY_CONTEXT_RELEVANT_LIMIT]:
        add(row)

    # When fewer than ten related rows exist, spend the remaining allowance on
    # recent conversation turns instead of leaving useful context capacity idle.
    for row in recent:
        add(row)

    bounded: list[tuple[str, tuple[Any, ...], dict[str, str]]] = []
    used_characters = 0
    for row in selected:
        sent_at = str(row.get("sent_at") or row.get("created_at") or "")[:64]
        text = str(row.get("text") or "").strip()
        remaining = COMMUNITY_CONTEXT_CHAR_LIMIT - used_characters - len(sent_at)
        if remaining <= 0:
            break
        bounded_text = text[:remaining].rstrip()
        if not bounded_text:
            continue
        bounded.append(
            (
                sent_at,
                _row_identity(row),
                {"time": sent_at, "text": bounded_text},
            )
        )
        used_characters += len(sent_at) + len(bounded_text)
        if len(bounded) >= COMMUNITY_CONTEXT_LIMIT or len(bounded_text) < len(text):
            break

    bounded.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in bounded)


def _row_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    if row.get("id") is not None:
        return ("id", int(row["id"]))
    return (
        "content",
        str(row.get("sent_at") or row.get("created_at") or ""),
        str(row.get("text") or ""),
    )


def _participant_identity(row: Mapping[str, Any]) -> tuple[str, str] | None:
    sender_id = row.get("sender_id")
    if sender_id is not None:
        try:
            return ("id", str(int(sender_id)))
        except (TypeError, ValueError):
            pass
    sender_name = str(row.get("sender_name") or "").strip().casefold()
    if sender_name and sender_name not in {"匿名", "unknown", "未知"}:
        return ("name", sender_name[:128])
    return None


def _relevance_features(value: str) -> tuple[str, frozenset[str]]:
    normalized = _URL_RE.sub(" ", value.casefold())
    compact = re.sub(r"\s+", " ", normalized).strip()
    tokens = set(_ASCII_TOKEN_RE.findall(compact))
    for sequence in _CJK_RE.findall(compact):
        if len(sequence) == 1:
            tokens.add(sequence)
        else:
            tokens.update(
                sequence[index : index + 2] for index in range(len(sequence) - 1)
            )
    return compact, frozenset(tokens)


def relevance_score(current: str, historical: str) -> float:
    current_text, current_tokens = _relevance_features(current)
    history_text, history_tokens = _relevance_features(historical)
    if not current_text or not history_text:
        return 0.0
    overlap = current_tokens & history_tokens
    token_score = len(overlap) / max(1, min(len(current_tokens), len(history_tokens)))
    sequence_score = SequenceMatcher(
        None, current_text[:600], history_text[:600]
    ).ratio()
    # Exact short signals such as “宕机” still match without a minimum length.
    substring_bonus = (
        0.35 if current_text in history_text or history_text in current_text else 0.0
    )
    score = token_score * 0.7 + sequence_score * 0.3 + substring_bonus
    return score if overlap or substring_bonus else 0.0


def _parse_context_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def context_character_count(context: Iterable[Mapping[str, str]]) -> int:
    return sum(
        len(str(item.get("time") or "")) + len(str(item.get("text") or ""))
        for item in context
    )
