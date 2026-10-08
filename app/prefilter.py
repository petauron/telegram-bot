from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class PrefilterReason(StrEnum):
    RECENT_EXACT_DUPLICATE = "recent_exact_duplicate"
    TELEGRAM_SERVICE_MESSAGE = "telegram_service_message"
    SYMBOLS_ONLY = "symbols_only"
    BARE_LINK = "bare_link"
    SHORT_UNPROTECTED_TEXT = "short_unprotected_text"
    STICKER_PLACEHOLDER = "sticker_placeholder"
    BOT_COMMAND = "bot_command"
    BOT_STATUS_WORKFLOW = "bot_status_workflow"
    BOT_DIGEST_WORKFLOW = "bot_digest_workflow"
    WELCOME_VERIFICATION = "welcome_verification"
    MODERATION_AUTOMATION = "moderation_automation"
    CHECKIN_POINTS = "checkin_points"
    BACKEND_SELECTION = "backend_selection"
    SUPPORT_AUTOMATION = "support_automation"


PREFILTER_REASON_LABELS: dict[PrefilterReason, str] = {
    PrefilterReason.RECENT_EXACT_DUPLICATE: "同群 72 小时内已处理过相同内容",
    PrefilterReason.TELEGRAM_SERVICE_MESSAGE: "Telegram 服务事件",
    PrefilterReason.SYMBOLS_ONLY: "仅包含 Emoji、符号、标点或空白",
    PrefilterReason.BARE_LINK: "仅包含链接和装饰符号，缺少可分析正文",
    PrefilterReason.SHORT_UNPROTECTED_TEXT: "4 个及以下字符且未命中短信号白名单",
    PrefilterReason.STICKER_PLACEHOLDER: "Telegram 贴纸文件占位",
    PrefilterReason.BOT_COMMAND: "纯 Bot 命令工作流",
    PrefilterReason.BOT_STATUS_WORKFLOW: "Bot 处理中或低信息状态",
    PrefilterReason.BOT_DIGEST_WORKFLOW: "Bot 自动生成的群聊日报",
    PrefilterReason.WELCOME_VERIFICATION: "入群欢迎或验证自动流程",
    PrefilterReason.MODERATION_AUTOMATION: "群管自动处置通知",
    PrefilterReason.CHECKIN_POINTS: "签到积分自动流程",
    PrefilterReason.BACKEND_SELECTION: "Bot 后端选择自动流程",
    PrefilterReason.SUPPORT_AUTOMATION: "固定客服或帮助自动回复",
}


@dataclass(frozen=True, slots=True)
class PrefilterResult:
    filtered: bool
    reason_code: str | None = None
    reason: str | None = None


# Only these context-free, high-precision results may finish before an
# analysis_job is created. Keep this allowlist explicit so a future filter is
# not accidentally moved ahead of same-chat ordering or community context.
PREQUEUE_SAFE_REASONS = frozenset(
    {
        PrefilterReason.TELEGRAM_SERVICE_MESSAGE.value,
        PrefilterReason.SYMBOLS_ONLY.value,
        PrefilterReason.BARE_LINK.value,
        PrefilterReason.STICKER_PLACEHOLDER.value,
        PrefilterReason.BOT_COMMAND.value,
        PrefilterReason.BOT_STATUS_WORKFLOW.value,
        PrefilterReason.BOT_DIGEST_WORKFLOW.value,
        PrefilterReason.WELCOME_VERIFICATION.value,
        PrefilterReason.MODERATION_AUTOMATION.value,
        PrefilterReason.CHECKIN_POINTS.value,
        PrefilterReason.BACKEND_SELECTION.value,
        PrefilterReason.SUPPORT_AUTOMATION.value,
    }
)


_STICKER_PLACEHOLDER_RE = re.compile(
    r"\A\s*(?:文件名\s*[:：]\s*)?"
    r"(?:sticker\.(?:webp|webm)|animatedsticker\.tgs)\s*\Z",
    re.IGNORECASE,
)
_BOT_COMMAND_RE = re.compile(
    r"\A\s*/[a-z0-9_\u3400-\u9fff]{1,64}(?:@[a-z0-9_]{5,32})?\s*\Z",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>]+|\bt\.me/[A-Za-z0-9_+/?=&.-]+"
)
_VERIFY_BUTTON_RE = re.compile(
    r"请在\s*\d{1,3}\s*(?:分钟|秒)(?:钟)?内.{0,24}(?:点击|点按).{0,16}(?:按钮|链接).{0,20}(?:完成|通过).{0,8}验证",
    re.IGNORECASE | re.DOTALL,
)
_ENGLISH_WELCOME_RE = re.compile(
    r"\bwelcome\b.{0,100}\b(?:join(?:ed|ing)?|group|member)\b.{0,120}"
    r"\b(?:verify|verification|complete)\b|"
    r"\b(?:verify|verification|complete)\b.{0,120}\bwelcome\b",
    re.IGNORECASE | re.DOTALL,
)
_BACKEND_SELECTION_PROMPT_RE = re.compile(
    r"\A\s*请选择.{0,12}后端\s*[:：]\s*\Z",
    re.DOTALL,
)
_PURE_CHECKIN_RE = re.compile(r"\A/?[签簽]到\Z")
_CHECKIN_POINTS_REPLY_RE = re.compile(
    r"\A.{0,32}感谢您的签到.{0,16}分到账.{0,32}\Z",
    re.DOTALL,
)
_REPEATED_ALNUM_RE = re.compile(r"\A([a-z0-9])\1{2,}\Z", re.IGNORECASE)
_BOT_STATUS_RE = re.compile(
    r"\A(?:🤔\s*)?thinking[.…。\s]*\Z|\A[▎|｜\s]*(?:解\s*析\s*中|处理中)[.…。\s]*\Z",
    re.IGNORECASE,
)
_SHORT_SIGNAL_ALLOWLIST = frozenset(
    {
        "炸了",
        "又炸了",
        "挂了",
        "又挂了",
        "崩了",
        "又崩了",
        "恢复",
        "已修复",
        "已恢复",
        "已升级",
        "已更新",
        "已发布",
        "已上线",
        "已下线",
        "停服了",
        "停运了",
        "断网了",
        "开源了",
        "限免",
        "限时免费",
        "永久免费",
        "免费领取",
        "免费试用",
        "优惠码",
        "兑换码",
        "折扣码",
        "代金券",
        "补货",
        "补货了",
        "免费",
        "优惠",
        "羊毛",
        "免单",
        "零元",
        "0元",
    }
)


def _result(reason: PrefilterReason) -> PrefilterResult:
    return PrefilterResult(
        filtered=True,
        reason_code=reason.value,
        reason=PREFILTER_REASON_LABELS[reason],
    )


def recent_exact_duplicate_result() -> PrefilterResult:
    """Return the stable audit result for a database-confirmed duplicate."""
    return _result(PrefilterReason.RECENT_EXACT_DUPLICATE)


def _is_symbols_only(text: str) -> bool:
    visible = "".join(character for character in text if not character.isspace())
    if not visible:
        return True
    categories = [unicodedata.category(character) for character in visible]
    if not any(category[0] in {"L", "N"} for category in categories):
        return True
    # Keycap emoji contain an ASCII digit plus a combining keycap mark.
    if "\u20e3" in visible:
        remaining = visible.replace("\ufe0f", "").replace("\u20e3", "")
        return bool(remaining) and all(character in "0123456789#*" for character in remaining)
    return False


def _is_bare_link(text: str) -> bool:
    """Reject links that have no locally assessable words outside the URL."""
    if _URL_RE.search(text) is None:
        return False
    without_links = _URL_RE.sub(" ", text)
    return _is_symbols_only(without_links)


def _is_short_unprotected(
    text: str,
    *,
    protected_keywords: tuple[str, ...],
) -> bool:
    visible = "".join(character for character in text if not character.isspace())
    if len(visible) > 4:
        return False
    return not has_protected_signal(visible, protected_keywords=protected_keywords)


def has_protected_signal(
    text: str,
    *,
    protected_keywords: tuple[str, ...] = (),
) -> bool:
    """Return whether local interests or the fixed short-signal list match."""
    folded = str(text or "").casefold()
    allowlist = (*_SHORT_SIGNAL_ALLOWLIST, *protected_keywords)
    return any(
        keyword.strip() and keyword.strip().casefold() in folded
        for keyword in allowlist
    )


def _is_welcome_verification(text: str) -> bool:
    if _VERIFY_BUTTON_RE.search(text) or _ENGLISH_WELCOME_RE.search(text):
        return True
    has_join_context = any(
        anchor in text
        for anchor in ("入群", "加入群组", "加入本群", "新成员", "群组验证")
    )
    has_welcome_or_verify = any(anchor in text for anchor in ("欢迎", "验证"))
    has_workflow_action = any(
        anchor in text
        for anchor in (
            "完成验证",
            "完成入群验证",
            "通过验证",
            "点击按钮",
            "点按按钮",
            "分钟内",
            "秒内",
            "验证失败",
            "验证成功",
        )
    )
    private_verification = all(
        anchor in text for anchor in ("欢迎", "点击下方按钮", "私信完成验证", "限时")
    )
    return (has_join_context and has_welcome_or_verify and has_workflow_action) or private_verification


def _is_moderation_automation(text: str) -> bool:
    follow_gate_notice = (
        "请先关注" in text
        and "加入群组后才能发言" in text
        and re.search(r"惩罚\s*[:：]\s*禁言(?:到)?", text) is not None
    )
    if follow_gate_notice:
        return True
    signatures = (
        ("已禁言", "入群风控"),
        ("禁止发送外部引用消息", "现有警告", "处理"),
        ("检测到违规消息", "已删除", "封禁"),
        ("提问无截图", "自动踢出", "群聊"),
        ("违规消息已删除", "自动封禁"),
        ("风险用户", "自动禁言", "入群"),
        ("发言权限尚未解锁", "请先完成", "中文文字发言"),
        ("验证已过期", "入群验证", "已被封禁"),
        ("未能完成入群验证", "已被封禁"),
        ("自动拦截", "命中封禁阈值", "处理：已封禁"),
        ("已将 用户", "封禁", "原因：触发拦截词"),
        ("封禁操作", "对象：用户", "已在封禁状态"),
        ("封禁操作", "对象：用户", "处理：已封禁"),
        ("已标记为广告", "消息已删除", "用户已封禁"),
        ("常见问题", "置顶或者机器人帮助", "本群主要以聊天为主", "本群退群后自动封禁"),
        ("欢迎加入群组", "新人建议先看官方文档", "群内可以吹水", "注意群规"),
        ("禁止发送外部引用消息", "警告("),
    )
    return any(all(anchor in text for anchor in signature) for signature in signatures)


def _is_checkin_points_workflow(text: str) -> bool:
    if _PURE_CHECKIN_RE.fullmatch(text) or _CHECKIN_POINTS_REPLY_RE.fullmatch(text):
        return True
    fixed_signatures = (
        ("今日活跃任务完成", "积分"),
        ("积分获取方式", "积分使用规则", "请查阅"),
        ("积分使用", "群内的积分", "付费购买积分"),
    )
    if any(all(anchor in text for anchor in signature) for signature in fixed_signatures):
        return True
    has_checkin = any(anchor in text for anchor in ("签到", "签 到", "check-in", "check in"))
    has_result = any(
        anchor in text
        for anchor in (
            "签到成功",
            "今日已签到",
            "重复签到",
            "连续签到",
            "获得积分",
            "奖励积分",
            "当前积分",
            "积分余额",
            "签到排名",
        )
    )
    return has_checkin and has_result


def _is_backend_selection_workflow(text: str) -> bool:
    if _BACKEND_SELECTION_PROMPT_RE.fullmatch(text):
        return True
    has_backend = any(anchor in text.casefold() for anchor in ("后端", "backend"))
    has_selection = any(
        anchor in text
        for anchor in ("选择后端", "请选择", "点击按钮", "当前后端", "切换后端")
    )
    has_bot_workflow = any(
        anchor in text
        for anchor in ("按钮", "菜单", "已选择", "选择一个", "当前后端", "切换后端")
    )
    return has_backend and has_selection and has_bot_workflow


def _is_bot_status_workflow(text: str) -> bool:
    if _BOT_STATUS_RE.fullmatch(text) or _REPEATED_ALNUM_RE.fullmatch(text):
        return True
    signatures = (
        ("为您创建了一个测试任务", "请选择测试的类型"),
        ("任务提交成功", "正在处理中", "任务名称", "测试项"),
        ("本日的规则触发数量上限", "明日再试"),
    )
    return text.strip() == "正在完成操作..." or any(
        all(anchor in text for anchor in signature) for signature in signatures
    )


def _is_bot_digest_workflow(text: str) -> bool:
    return "群聊吃瓜日报" in text and "Daily Gossip" in text


def _is_support_automation(text: str) -> bool:
    if text.strip() in {"推荐的代理工具：", "推荐的代理工具:"}:
        return True
    signatures = (
        ("浏览器挂梯子", "关闭插件", "访问官网"),
        ("访问ChatGPT等AI网站", "香港", "切换", "节点"),
        ("目前没有活动内容", "套餐已经很便宜", "下单"),
        ("禁止使用订阅转换", "检查你使用的工具", "重新导入"),
        ("手机电脑教程", "查看使用教程", "软路由"),
        ("点击下方按钮", "下载clash meta"),
        ("客服不是24小时在线", "有问题留言", "不要催"),
        ("手动重置订阅链接", "用户中心", "订阅管理"),
        ("本群认证店铺", "欢迎选购"),
        ("测试邀请议程", "选择下面的按钮", "提交测试链接"),
        ("一次性不限时套餐", "续费相同月付套餐", "购买不同月付套餐", "覆盖掉原套餐"),
        ("订阅连接", "账号", "新密码", "官网工单", "信息发全"),
        ("订阅链接", "账号", "新密码", "官网工单", "信息发全"),
        ("建议使用vless协议", "hy2节点", "晚高峰", "美国节点"),
        ("使用非香港节点访问tiktok", "登陆良心云后台", "安装使用教程"),
    )
    folded = text.casefold()
    return any(
        all(anchor.casefold() in folded for anchor in signature)
        for signature in signatures
    )


def evaluate_prefilter(
    text: str,
    *,
    is_service_message: bool = False,
    protected_keywords: tuple[str, ...] = (),
) -> PrefilterResult:
    """Apply conservative, local-only workflow filters before model analysis."""
    candidate = str(text or "").strip()
    if is_service_message:
        return _result(PrefilterReason.TELEGRAM_SERVICE_MESSAGE)
    if _is_symbols_only(candidate):
        return _result(PrefilterReason.SYMBOLS_ONLY)
    if _is_bare_link(candidate):
        return _result(PrefilterReason.BARE_LINK)
    if _STICKER_PLACEHOLDER_RE.fullmatch(candidate):
        return _result(PrefilterReason.STICKER_PLACEHOLDER)
    if _BOT_COMMAND_RE.fullmatch(candidate) and not _PURE_CHECKIN_RE.fullmatch(
        candidate
    ):
        return _result(PrefilterReason.BOT_COMMAND)
    if _is_bot_status_workflow(candidate):
        return _result(PrefilterReason.BOT_STATUS_WORKFLOW)
    if _is_bot_digest_workflow(candidate):
        return _result(PrefilterReason.BOT_DIGEST_WORKFLOW)
    if _is_moderation_automation(candidate):
        return _result(PrefilterReason.MODERATION_AUTOMATION)
    if _is_welcome_verification(candidate):
        return _result(PrefilterReason.WELCOME_VERIFICATION)
    if _is_checkin_points_workflow(candidate):
        return _result(PrefilterReason.CHECKIN_POINTS)
    if _is_backend_selection_workflow(candidate):
        return _result(PrefilterReason.BACKEND_SELECTION)
    if _is_support_automation(candidate):
        return _result(PrefilterReason.SUPPORT_AUTOMATION)
    if _is_short_unprotected(candidate, protected_keywords=protected_keywords):
        return _result(PrefilterReason.SHORT_UNPROTECTED_TEXT)
    return PrefilterResult(filtered=False)


def evaluate_prequeue_prefilter(
    text: str,
    *,
    is_service_message: bool = False,
) -> PrefilterResult:
    """Return only deterministic results safe before durable queue creation.

    Short-text and database-confirmed duplicate decisions deliberately remain
    in the ordered worker pipeline because they can depend on protected terms,
    same-thread evidence, earlier messages, or terminal processing state.
    """
    result = evaluate_prefilter(text, is_service_message=is_service_message)
    if result.filtered and result.reason_code in PREQUEUE_SAFE_REASONS:
        return result
    return PrefilterResult(filtered=False)
