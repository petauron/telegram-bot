"""Conservative, source-backed VPS deal checks and burst grouping."""
from __future__ import annotations

import re
from typing import Any

_VPS = re.compile(r"vps|vds|\bkvm\b|云服务器|云主机|虚拟服务器|virtual private server", re.I)
_PRICE = re.compile(r"(?:US\$|CA\$|C\$|HK\$|\$|¥|￥|€|£|USD|CNY|RMB)\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:美元|美金|加元|人民币|元|USD|CNY|RMB)", re.I)
_DISCOUNT = re.compile(r"(?:[1-9](?:\.\d+)?\s*折)|(?:\d+(?:\.\d+)?\s*%\s*(?:off|折扣|优惠))|(?:(?:优惠|折扣|discount)\s*\d+(?:\.\d+)?\s*%)|(?:(?:直降|立减|减免|便宜|save)\D{0,8}(?:\d+(?:\.\d+)?\s*(?:%|元|美元|美金|加元|USD|CNY)|[$¥￥€£]\s*\d))", re.I)
_FREE = re.compile(r"(?:限时免费|永久免费|免费试用|免费领取|(?<![\d.])0(?:\.0+)?\s*元(?![\d.])|[$¥￥€£]\s*0(?:\.0+)?(?![\d.]))", re.I)
_COMPARE = re.compile(r"原价|原售价|原本|现价|原为|从.{0,24}(?:降至|降到|降为)|(?:比|低于).{0,30}(?:便宜|低|价格|售价)|历史最低|历史新低|同配置|同规格|市场价|usual(?:ly)?|regular price|was\s+.*now", re.I)
_VENDOR = re.compile(r"^\s*([A-Za-z][A-Za-z0-9._-]{2,39}|[\u4e00-\u9fff]{2,12})(?=\s|[:：])")
_GENERIC = frozenset({'vps','vds','vps补货','云服务器','云主机','美国','日本','香港','新加坡','限时','优惠','福利'})


def is_vps(row: dict[str, Any]) -> bool:
    return row.get('content_kind') == 'benefit_deal' and bool(_VPS.search(' '.join(str(row.get(k) or '') for k in ('text', 'benefit_title', 'benefit_summary'))))


def has_value_evidence(row: dict[str, Any]) -> bool:
    if not is_vps(row):
        return True
    source = str(row.get('text') or '')
    prices = _PRICE.findall(source)
    return bool((prices and (_DISCOUNT.search(source) or (len(prices) >= 2 and _COMPARE.search(source)))) or _FREE.search(source))


def vendor_key(row: dict[str, Any]) -> str | None:
    if not is_vps(row):
        return None
    title = str(row.get('benefit_title') or '').strip()
    match = _VENDOR.match(title)
    if not match:
        return None
    vendor = match.group(1).casefold()
    if vendor in _GENERIC or vendor not in str(row.get('text') or '').casefold():
        return None
    return vendor
