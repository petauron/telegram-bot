from __future__ import annotations

import os
from datetime import timedelta

from app.database import Database, MessageRecord, to_iso, utc_now
from app.scoring import normalize_text


SAMPLES = [
    (1, -1001234567890, "运维通知群", "林工", 98, "【紧急】今晚 23:30 进行核心数据库维护，预计影响 15 分钟，请相关同事提前确认。", ("重要关键词（紧急、维护） +35", "日期/截止时间 +15", "行动信息（维护） +15"), 12),
    (2, -1009876543210, "安全公告", "SecOps", 96, "【安全预警】CVE-2026-3094 已出补丁，请尽快升级内核到 5.15.162 或更高版本。", ("重要关键词（CVE、漏洞、升级） +45", "包含 CVE、版本号 +15", "行动信息（升级） +15"), 7),
    (3, -1001234567890, "运维通知群", "林工", 74, "支付服务已恢复正常，若仍有问题请重试或联系 @林工。", ("重要关键词（恢复） +25", "当前窗口多人回复（5 人） +30"), 5),
    (4, -1002468013579, "产品发布", "发布机器人", 62, "【发布】产品控制台 v2.3.0 上线，新增多租户管理与导出功能。", ("重要关键词（发布） +25", "包含版本号 +15", "行动信息（发布） +15"), 3),
    (5, -1001234567890, "运维通知群", "监控机器人", 58, "监控告警：API 错误率持续升高，请排查相关服务。", ("重要关键词（异常） +25",), 2),
    (6, -1001234567890, "运维通知群", "林工", 47, "【提醒】本周五进行机房消防演练，请各位配合。", ("日期/截止时间 +15",), 1),
    (7, -1001357924680, "运维知识库", "知识机器人", 35, "知识库已更新：常见故障排查流程 v1.7。", ("重要关键词（故障） +25", "包含版本号 +15"), 0),
    (8, -1001122334455, "行政通知群", "行政助理", 22, "请大家及时更新个人联系方式。", ("普通信息",), 0),
]


def main() -> None:
    path = os.getenv("DATABASE_PATH", "/database/messages.db")
    database = Database(path)
    now = utc_now()
    database.initialize_digest_clock(now - timedelta(hours=1))
    database.initialize_runtime_config(
        important_keywords=("紧急", "故障", "维护", "漏洞", "CVE", "异常", "恢复", "发布", "升级", "截止"),
        trusted_sender_ids=frozenset({123456789, 987654321}),
        watch_chat_ids=frozenset(item[1] for item in SAMPLES),
        immediate_score=80,
        now=now,
    )
    database.update_runtime_config(
        important_keywords=("紧急", "故障", "维护", "漏洞", "CVE", "异常", "恢复", "发布", "升级", "截止"),
        trusted_sender_ids=frozenset({123456789, 987654321}),
        watch_chat_ids=frozenset(item[1] for item in SAMPLES),
        immediate_score=80,
        now=now,
    )
    database.replace_available_chats(
        (
            {
                "chat_id": chat_id,
                "chat_name": chat_name,
                "chat_type": "channel" if chat_name in {"安全公告", "产品发布"} else "group",
                "username": None,
            }
            for _, chat_id, chat_name, *_ in SAMPLES
        ),
        now=now,
    )
    inserted: list[dict] = []
    for index, chat_id, chat_name, sender, score, text, reasons, replies in SAMPLES:
        timestamp = now - timedelta(minutes=index * 7)
        record = MessageRecord(
            chat_id=chat_id,
            message_id=2000 + index,
            chat_name=chat_name,
            chat_username=None,
            sender_id=1000 + index,
            sender_name=sender,
            sent_at=to_iso(timestamp),
            text=text,
            reply_to_message_id=None,
            thread_root_id=2000 + index,
            base_score=score,
            reasons=reasons,
            link=f"https://t.me/c/{abs(chat_id) - 1_000_000_000_000}/{2000 + index}",
            normalized_text=normalize_text(text),
            primary_url=None,
            created_at=to_iso(timestamp),
        )
        database.insert_message(record)
        with database.connection:
            database.connection.execute(
                """
                UPDATE messages
                SET prefilter_status = 'passed', push_eligible = 1,
                    push_gate_reason = 'eligible'
                WHERE chat_id = ? AND message_id = ?
                """,
                (record.chat_id, record.message_id),
            )
        if replies:
            database.connection.execute(
                "UPDATE messages SET reply_count = ? WHERE chat_id = ? AND message_id = ?",
                (replies, chat_id, 2000 + index),
            )
        row = database.get_message(chat_id, 2000 + index)
        if row:
            inserted.append(row)
    database.connection.commit()
    for row in inserted[:2]:
        database.reserve_immediate(row["chat_id"], row["message_id"], 80, now)
    database.mark_digest_considered(inserted[3:4], inserted[3:4], now)
    database.set_listener_heartbeat(
        connected=True,
        self_id=778899001,
        watch_count=5,
        now=now,
    )
    database.close()


if __name__ == "__main__":
    main()
