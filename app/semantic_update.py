from __future__ import annotations

import re
from dataclasses import dataclass


SEMANTIC_UPDATE_TYPES = frozenset(
    {
        "none",
        "confirmation_or_correction",
        "service_status_change",
        "new_version_or_cve",
        "price_or_policy_change",
        "region_or_availability_change",
        "date_or_deadline_change",
        "impact_status_change",
    }
)

_CVE_RE = re.compile(r"(?i)\bCVE-\d{4}-\d{4,}\b")
_VERSION_RE = re.compile(r"(?i)\bv?\d+\.\d+(?:\.\d+){0,2}(?:[-_][a-z0-9.]+)?\b")
_CVE_ACTION_RE = re.compile(
    r"(?i)(?:(?:安全公告|漏洞公告|发布|披露|通报|修复|新增).{0,20}(CVE-\d{4}-\d{4,})|"
    r"(CVE-\d{4}-\d{4,}).{0,20}(?:发布|披露|通报|公告|修复))"
)
_VERSION_ACTION_RE = re.compile(
    r"(?i)(?:(?:发布|推出|上线|升级至|更新至|更新为|版本(?:发布|更新|升级)|release[ds]?|upgrade[ds]?)"
    r".{0,12}(v?\d+\.\d+(?:\.\d+){0,2}(?:[-_][a-z0-9.]+)?)|"
    r"(v?\d+\.\d+(?:\.\d+){0,2}(?:[-_][a-z0-9.]+)?).{0,12}(?:版本)?(?:发布|推出|上线|升级|更新))"
)
_PRICE_RE = re.compile(
    r"(?i)(?:[$¥￥€£]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:元|美元|美金|人民币|欧元|英镑|%))"
)
_PRICE_CHANGE_RE = re.compile(
    r"(?:涨价|降价|调价|价格调整|价格变更|上调|下调|恢复原价|取消涨价|"
    r"由.{0,24}(?:调整|改为|变更)为|从.{0,24}(?:调整|改为|变更)为|收费政策(?:调整|变更))"
)
_DATE_RE = re.compile(
    r"(?i)(?:\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b|"
    r"\b\d{1,2}[-/.月]\d{1,2}日?\b|\b\d{1,2}:\d{2}\b|"
    r"(?:今天|今日|明天|明日|本周|下周|本月|下月|月底|年末))"
)
_DATE_CHANGE_RE = re.compile(
    r"(?:延期|提前|推迟|改期|延长至|缩短至|截止(?:日期|时间|期限)?(?:调整|改为|变更)|"
    r"日期(?:调整|改为|变更)|期限(?:调整|改为|变更))"
)
_REGION_RE = re.compile(
    r"(?:中国大陆|中国内地|中国区|国区|美国|加拿大|欧洲|欧盟|英国|日本|韩国|"
    r"东南亚|亚太|全球|港澳台|香港|澳门|台湾|\w{2,16}(?:地区|区域|市场))"
)
_REGION_CHANGE_RE = re.compile(
    r"(?:新增.{0,20}(?:地区|区域|市场)|扩展至|开放至|上线至|登陆.{0,20}(?:地区|市场)|"
    r"开始在.{0,20}(?:提供|上线|销售)|停止在.{0,20}(?:提供|销售)|可用范围(?:扩大|缩小))"
)
_CORRECTION_RE = re.compile(r"(?:官方|正式)?.{0,8}(?:辟谣|否认|澄清|更正|撤回|纠正)")
_CONFIRMATION_RE = re.compile(r"(?:官方|正式).{0,8}(?:确认|证实)|(?:确认|证实).{0,8}(?:属实|消息|事件)")
_UNCERTAIN_RE = re.compile(
    r"(?:传闻|据称|或将|可能|尚未确认|尚无官方确认|未经证实|未证实|爆料|疑似|网传|社区线索)"
)
_IMPACT_CHANGE_RE = re.compile(
    r"(?:影响(?:范围|用户|地区)?(?:扩大|缩小|增加|减少|已消除)|新增受影响|"
    r"受影响.{0,16}(?:增至|降至|扩大至|缩小至)|损失.{0,12}(?:扩大|增加|降低)|"
    r"风险等级(?:上调|下调)|危害等级(?:上调|下调))"
)

_SERVICE_STATE_PATTERNS = {
    "recovered": re.compile(r"(?:已(?:经)?恢复|恢复正常|故障已解除|问题已解决|服务恢复|恢复服务)"),
    "resolved": re.compile(
        r"(?:(?:故障|服务|中断|事故).{0,6}(?:已修复|已解决)|(?:问题|事件)已解决|影响已消除)"
    ),
    "worsened": re.compile(r"(?:故障扩大|影响扩大|再次中断|再次故障|持续恶化)"),
    "degraded": re.compile(r"(?:性能下降|服务降级|部分不可用|访问缓慢)"),
    "outage": re.compile(r"(?:服务中断|服务不可用|发生故障|出现故障|宕机|停服)"),
}


@dataclass(frozen=True, slots=True)
class SemanticUpdateValidation:
    valid: bool
    rejection_reason: str | None = None


def validate_semantic_update(
    update_type: str | None,
    *,
    current: dict,
    candidate: dict,
) -> SemanticUpdateValidation:
    """Require deterministic evidence before a same-event update can be pushed."""
    if update_type not in SEMANTIC_UPDATE_TYPES or update_type == "none":
        return SemanticUpdateValidation(False, "模型未提供可验证的更新类型")

    current_text = _event_text(current)
    candidate_text = _event_text(candidate)
    current_summary = _summary_text(current) or current_text
    if not current_text:
        return SemanticUpdateValidation(False, "当前资讯缺少可验证正文")

    if update_type == "confirmation_or_correction":
        if _CORRECTION_RE.search(current_text):
            return SemanticUpdateValidation(True)
        if _CONFIRMATION_RE.search(current_text) and _UNCERTAIN_RE.search(candidate_text):
            return SemanticUpdateValidation(True)
        return SemanticUpdateValidation(False, "没有从未确认状态转为正式确认或更正的证据")

    if update_type == "service_status_change":
        current_states = _service_states(current_text)
        candidate_states = _service_states(candidate_text)
        if current_states and current_states - candidate_states:
            return SemanticUpdateValidation(True)
        return SemanticUpdateValidation(False, "没有可验证的服务状态变化")

    if update_type == "new_version_or_cve":
        # Bind new identifiers to the event summary so a release article's
        # benchmark table or vulnerability-discovery details cannot become a
        # separate update merely because its long body contains new numbers.
        current_cves = _group_tokens(_CVE_ACTION_RE, current_summary)
        candidate_cves = _tokens(_CVE_RE, candidate_text)
        if current_cves - candidate_cves:
            return SemanticUpdateValidation(True)
        current_versions = _group_tokens(_VERSION_ACTION_RE, current_summary)
        candidate_versions = _tokens(_VERSION_RE, candidate_text)
        if current_versions - candidate_versions:
            return SemanticUpdateValidation(True)
        return SemanticUpdateValidation(False, "没有候选中不存在的新版本或 CVE 标识")

    if update_type == "price_or_policy_change":
        if not _PRICE_CHANGE_RE.search(current_text):
            return SemanticUpdateValidation(False, "只补充价格或方案细节，没有再次调价或政策变更")
        if _tokens(_PRICE_RE, current_text) - _tokens(_PRICE_RE, candidate_text):
            return SemanticUpdateValidation(True)
        if any(term in current_text for term in ("取消涨价", "恢复原价", "收费政策调整", "收费政策变更")):
            return SemanticUpdateValidation(True)
        return SemanticUpdateValidation(False, "没有可验证的新价格或收费政策锚点")

    if update_type == "region_or_availability_change":
        if _REGION_CHANGE_RE.search(current_text) and (
            _tokens(_REGION_RE, current_text) - _tokens(_REGION_RE, candidate_text)
        ):
            return SemanticUpdateValidation(True)
        return SemanticUpdateValidation(False, "没有可验证的新增地区或可用范围变化")

    if update_type == "date_or_deadline_change":
        if _DATE_CHANGE_RE.search(current_text) and (
            _tokens(_DATE_RE, current_text) - _tokens(_DATE_RE, candidate_text)
        ):
            return SemanticUpdateValidation(True)
        return SemanticUpdateValidation(False, "没有可验证的新日期或期限变化")

    if update_type == "impact_status_change":
        if _IMPACT_CHANGE_RE.search(current_text):
            return SemanticUpdateValidation(True)
        return SemanticUpdateValidation(False, "只有影响细节补充，没有实际影响状态变化")

    return SemanticUpdateValidation(False, "更新类型不可用")


def _event_text(value: dict) -> str:
    return " ".join(
        " ".join(str(value.get(field) or "").split())
        for field in ("ai_summary", "summary", "text")
    ).strip().casefold()


def _summary_text(value: dict) -> str:
    return " ".join(
        " ".join(str(value.get(field) or "").split())
        for field in ("ai_summary", "summary")
    ).strip().casefold()


def _tokens(pattern: re.Pattern[str], value: str) -> set[str]:
    return {match.group(0).casefold().replace(" ", "") for match in pattern.finditer(value)}


def _group_tokens(pattern: re.Pattern[str], value: str) -> set[str]:
    return {
        token.casefold().replace(" ", "")
        for match in pattern.finditer(value)
        for token in match.groups()
        if token
    }


def _service_states(value: str) -> set[str]:
    return {
        state
        for state, pattern in _SERVICE_STATE_PATTERNS.items()
        if pattern.search(value)
    }
