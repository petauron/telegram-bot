from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from app.analysis_queue import PersistentAnalysisQueue
from app.cisa_kev import CisaKevPoller
from app.config import Settings
from app.database import (
    QUEUE_METRIC_SAMPLE_INTERVAL_SECONDS,
    Database,
    MessageRecord,
    to_iso,
    utc_now,
)
from app.delivery_queue import PersistentDeliveryQueue
from app.feeds import RSSPoller
from app.github_advisories import GitHubAdvisoriesPoller
from app.github_releases import GitHubReleasesPoller
from app.llm import OpenAICompatibleClient
from app.nvd_cve import NvdCvePoller
from app.vendor_status import VendorStatusPoller
from app.hacker_news import HackerNewsPoller
from app.bluesky import BlueskyJetstreamPoller
from app.mastodon import MastodonPoller
from app.newsletter_imap import NewsletterImapPoller
from app.ntfy_feedback import NtfyFeedbackCollector
from app.push import PushDispatcher
from app.scoring import normalize_text, score_message
from app.utils import display_name, extract_processable_text, message_link


LOGGER = logging.getLogger("telegram_priority")
RECENT_ACTIVITY_WINDOW_MINUTES = 15


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class MessageProcessor:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        analysis_queue: PersistentAnalysisQueue,
        delivery_queue: PersistentDeliveryQueue,
        self_id: int,
    ) -> None:
        self.settings = settings
        self.database = database
        self.analysis_queue = analysis_queue
        self.delivery_queue = delivery_queue
        self.self_id = self_id

    def _runtime_rules(self) -> dict[str, Any]:
        configured = self.database.get_runtime_config()
        if configured is not None:
            return configured
        return {
            "important_keywords": self.settings.important_keywords,
            "trusted_sender_ids": self.settings.trusted_sender_ids,
            "watch_chat_ids": self.settings.watch_chat_ids,
            "immediate_score": self.settings.immediate_score,
        }

    async def process(self, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        runtime_rules = self._runtime_rules()
        if chat_id is None or chat_id not in runtime_rules["watch_chat_ids"]:
            return
        sender_id = event.sender_id
        if event.out or sender_id == self.self_id:
            return

        message = event.message
        is_service_message = bool(getattr(message, "action", None))
        text = extract_processable_text(message)
        if not text and is_service_message:
            action_name = type(getattr(message, "action", None)).__name__[:80]
            text = f"Telegram 服务事件：{action_name}"
        if not text:
            return

        message_id = int(message.id)
        if self.database.get_message(chat_id, message_id) is not None:
            return

        try:
            chat_entity = await event.get_chat()
        except FloodWaitError:
            raise
        except Exception:
            chat_entity = getattr(event, "chat", None)
        try:
            sender_entity = await event.get_sender()
        except FloodWaitError:
            raise
        except Exception:
            sender_entity = getattr(event, "sender", None)
        reply_to_message_id = getattr(message, "reply_to_msg_id", None)
        reply_to_me = await self._is_reply_to_me(event, chat_id, reply_to_message_id)

        observed_at = utc_now()
        normalized = normalize_text(text)
        repeated = self.database.count_recent_normalized(
            chat_id,
            normalized,
            observed_at - timedelta(minutes=RECENT_ACTIVITY_WINDOW_MINUTES),
        ) > 0
        scoring = score_message(
            text,
            mentioned_me=bool(getattr(message, "mentioned", False)),
            reply_to_me=reply_to_me,
            trusted_sender=sender_id in runtime_rules["trusted_sender_ids"],
            keywords=runtime_rules["important_keywords"],
            repeated_recently=repeated,
        )

        thread_root_id = self.database.thread_root_for(chat_id, reply_to_message_id)
        if thread_root_id is None:
            thread_root_id = message_id

        sent_at = getattr(message, "date", None) or observed_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        record = MessageRecord(
            chat_id=chat_id,
            message_id=message_id,
            chat_name=display_name(chat_entity, chat_id),
            chat_username=getattr(chat_entity, "username", None),
            sender_id=sender_id,
            sender_name=display_name(sender_entity, sender_id),
            sent_at=to_iso(sent_at),
            text=text,
            reply_to_message_id=reply_to_message_id,
            thread_root_id=thread_root_id,
            base_score=scoring.score,
            reasons=scoring.reasons,
            link=message_link(chat_id, message_id, chat_entity),
            normalized_text=scoring.normalized_text,
            primary_url=scoring.primary_url,
            created_at=to_iso(observed_at),
            is_service_message=is_service_message,
        )
        if not self.database.insert_message(
            record,
            enqueue_analysis=True,
            now=observed_at,
        ):
            return
        self.analysis_queue.wake()

        if reply_to_message_id is not None:
            parent = self.database.record_reply_and_update_parent(
                chat_id=chat_id,
                parent_message_id=int(reply_to_message_id),
                sender_id=sender_id,
                replied_at=observed_at,
                window_minutes=RECENT_ACTIVITY_WINDOW_MINUTES,
            )
            if parent is not None:
                await self._push_if_immediate(parent, observed_at)

    async def _is_reply_to_me(
        self,
        event: events.NewMessage.Event,
        chat_id: int,
        reply_to_message_id: int | None,
    ) -> bool:
        if reply_to_message_id is None:
            return False
        known_parent = self.database.get_message(chat_id, int(reply_to_message_id))
        if known_parent is not None:
            return known_parent.get("sender_id") == self.self_id
        try:
            reply = await event.get_reply_message()
        except FloodWaitError:
            raise
        except Exception:
            return False
        if reply is None:
            return False
        return bool(getattr(reply, "out", False)) or getattr(reply, "sender_id", None) == self.self_id

    async def _push_if_immediate(self, row: dict[str, Any], now: datetime) -> None:
        created = self.database.enqueue_immediate_deliveries(
            int(row["chat_id"]),
            int(row["message_id"]),
            now=now,
        )
        if created:
            self.delivery_queue.wake()


async def maintenance_loop(
    settings: Settings,
    database: Database,
    stop_event: asyncio.Event,
) -> None:
    interval = 15 * 60
    while not stop_event.is_set():
        now = utc_now()
        try:
            deleted = database.cleanup(settings.retention_days, now)
            if deleted:
                LOGGER.info("已清理超过保留期的消息：%s 条", deleted)
        except Exception as exc:  # maintenance failure must not stop the listener
            LOGGER.error("维护任务异常：%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass


async def heartbeat_loop(
    settings: Settings,
    database: Database,
    client: TelegramClient,
    self_id: int,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        now = utc_now()
        runtime = database.get_runtime_config()
        watch_count = len(runtime["watch_chat_ids"]) if runtime else len(settings.watch_chat_ids)
        database.set_listener_heartbeat(
            connected=client.is_connected(),
            self_id=self_id,
            watch_count=watch_count,
            now=now,
        )
        try:
            database.record_queue_metric_sample(now=now)
        except Exception as exc:  # metrics must never stop the listener heartbeat
            LOGGER.warning("保存队列历史失败：%s", type(exc).__name__)
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=QUEUE_METRIC_SAMPLE_INTERVAL_SECONDS,
            )
        except TimeoutError:
            pass


async def sync_available_chats(client: TelegramClient, database: Database) -> int:
    chats: list[dict[str, Any]] = []
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        chats.append(
            {
                "chat_id": int(dialog.id),
                "chat_name": str(dialog.name or dialog.id),
                "chat_type": "group" if dialog.is_group else "channel",
                "username": getattr(entity, "username", None),
            }
        )
    synced_at = utc_now()
    count = database.replace_available_chats(chats, now=synced_at)
    runtime = database.get_runtime_config()
    if runtime is not None:
        available_ids = frozenset(chat["chat_id"] for chat in chats)
        valid_watch_ids = runtime["watch_chat_ids"] & available_ids
        if valid_watch_ids != runtime["watch_chat_ids"]:
            database.update_runtime_config(
                important_keywords=runtime["important_keywords"],
                trusted_sender_ids=runtime["trusted_sender_ids"],
                watch_chat_ids=valid_watch_ids,
                immediate_score=runtime["immediate_score"],
                now=synced_at,
            )
            removed_count = len(runtime["watch_chat_ids"] - valid_watch_ids)
            LOGGER.warning("已移除账号当前不可见的监听会话：%s 个", removed_count)
    return count


async def chat_sync_loop(
    client: TelegramClient,
    database: Database,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15 * 60)
            break
        except TimeoutError:
            pass
        try:
            count = await sync_available_chats(client, database)
            LOGGER.info("已同步账号可见会话：%s 个", count)
        except FloodWaitError as exc:
            LOGGER.warning("同步会话遇到 FloodWait：%s 秒后重试", exc.seconds)
        except Exception as exc:
            LOGGER.warning("同步账号会话失败：%s", type(exc).__name__)


async def run_service(settings: Settings) -> None:
    settings.ensure_parent_directories()
    started_at = utc_now()
    startup_result: dict[str, int] = {}

    def initialize_database() -> None:
        startup_database = Database(settings.database_path)
        try:
            startup_database.initialize_runtime_config(
                important_keywords=settings.important_keywords,
                trusted_sender_ids=settings.trusted_sender_ids,
                watch_chat_ids=settings.watch_chat_ids,
                immediate_score=settings.immediate_score,
                now=started_at,
            )
            startup_database.initialize_push_config(
                telegram_bot_token=settings.push_bot_token,
                telegram_chat_id=settings.push_chat_id,
                now=started_at,
            )
            startup_result["retired_digest_deliveries"] = (
                startup_database.retire_pending_digest_deliveries()
            )
        finally:
            startup_database.close()

    # Schema migration and first-run configuration can touch many existing
    # rows, so keep that one-time work off the async listener event loop.
    await asyncio.to_thread(initialize_database)
    if startup_result.get("retired_digest_deliveries"):
        LOGGER.warning(
            "已取消旧摘要待投递任务：%s 条",
            startup_result["retired_digest_deliveries"],
        )
    database = Database(settings.database_path, initialize=False)
    pusher = PushDispatcher(database)
    model_client = OpenAICompatibleClient()
    delivery_queue = PersistentDeliveryQueue(settings.database_path, pusher)

    async def on_live_analysis_success(row: dict[str, Any]) -> None:
        _ = row
        delivery_queue.wake()

    analysis_queue = PersistentAnalysisQueue(
        settings.database_path,
        model_client,
        on_live_success=on_live_analysis_success,
        worker_count=settings.analysis_workers,
        batch_target=settings.analysis_batch_target,
        batch_max_items=settings.analysis_batch_max_items,
        batch_max_chars=settings.analysis_batch_max_chars,
        batch_max_wait_seconds=settings.analysis_batch_max_wait_seconds,
    )
    recovered_analysis = await analysis_queue.start()
    recovered_deliveries = await delivery_queue.start()
    feed_poller = RSSPoller(settings.database_path, analysis_queue)
    recovered_feeds = await feed_poller.start()
    github_releases_poller = GitHubReleasesPoller(settings.database_path, analysis_queue)
    recovered_github_releases = await github_releases_poller.start()
    github_advisories_poller = GitHubAdvisoriesPoller(settings.database_path, analysis_queue)
    recovered_github_advisories = await github_advisories_poller.start()
    cisa_kev_poller = CisaKevPoller(settings.database_path, analysis_queue)
    recovered_cisa_kev = await cisa_kev_poller.start()
    nvd_cve_poller = NvdCvePoller(settings.database_path, analysis_queue)
    recovered_nvd_cve = await nvd_cve_poller.start()
    vendor_status_poller = VendorStatusPoller(settings.database_path, analysis_queue)
    recovered_vendor_status = await vendor_status_poller.start()
    hacker_news_poller = HackerNewsPoller(settings.database_path, analysis_queue)
    recovered_hacker_news = await hacker_news_poller.start()
    bluesky_poller = BlueskyJetstreamPoller(settings.database_path, analysis_queue)
    recovered_bluesky = await bluesky_poller.start()
    mastodon_poller = MastodonPoller(settings.database_path, analysis_queue)
    recovered_mastodon = await mastodon_poller.start()
    newsletter_poller = NewsletterImapPoller(settings.database_path, analysis_queue)
    recovered_newsletter = await newsletter_poller.start()
    feedback_collector = NtfyFeedbackCollector(settings.database_path)
    await feedback_collector.start()
    if recovered_analysis["interrupted"] or recovered_analysis["adopted"]:
        LOGGER.warning(
            "已恢复模型分析任务：中断=%s 旧任务=%s",
            recovered_analysis["interrupted"],
            recovered_analysis["adopted"],
        )
    if recovered_analysis.get("semantic_dedupe"):
        LOGGER.warning(
            "已恢复跨来源语义去重检查：%s 条",
            recovered_analysis["semantic_dedupe"],
        )
    if recovered_deliveries:
        LOGGER.warning("已恢复渠道投递任务：%s 条", recovered_deliveries)
    if recovered_feeds:
        LOGGER.warning("已恢复 RSS/Atom 采集任务：%s 个", recovered_feeds)
    if recovered_github_releases:
        LOGGER.warning("已恢复 GitHub Releases 采集任务：%s 个", recovered_github_releases)
    if recovered_github_advisories:
        LOGGER.warning("已恢复 GitHub Security Advisories 采集任务：%s 个", recovered_github_advisories)
    if recovered_cisa_kev:
        LOGGER.warning("已恢复 CISA KEV 采集任务：%s 个", recovered_cisa_kev)
    if recovered_nvd_cve:
        LOGGER.warning("已恢复 NVD CVE 采集任务：%s 个", recovered_nvd_cve)
    if recovered_vendor_status:
        LOGGER.warning("已恢复厂商状态页采集任务：%s 个", recovered_vendor_status)
    if recovered_hacker_news:
        LOGGER.warning("已恢复 Hacker News 采集任务：%s 个", recovered_hacker_news)
    if recovered_bluesky:
        LOGGER.warning("已恢复 Bluesky Jetstream 采集任务：%s 个", recovered_bluesky)
    if recovered_mastodon:
        LOGGER.warning("已恢复 Mastodon 采集任务：%s 个", recovered_mastodon)
    if recovered_newsletter:
        LOGGER.warning("已恢复邮件 Newsletter 采集任务：%s 个", recovered_newsletter)
    client = TelegramClient(
        settings.tg_session_path,
        settings.tg_api_id,
        settings.tg_api_hash,
        auto_reconnect=True,
        connection_retries=None,
        request_retries=5,
        retry_delay=5,
        flood_sleep_threshold=300,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram 个人账号尚未登录，请先运行 app.login")
        me = await client.get_me()
        if me is None:
            raise RuntimeError("无法读取当前 Telegram 账号")

        processor = MessageProcessor(
            settings=settings,
            database=database,
            analysis_queue=analysis_queue,
            delivery_queue=delivery_queue,
            self_id=int(me.id),
        )

        @client.on(events.NewMessage(incoming=True))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            for attempt in range(2):
                try:
                    await processor.process(event)
                    return
                except FloodWaitError as exc:
                    seconds = max(1, int(exc.seconds))
                    LOGGER.warning("Telegram FloodWait：等待 %s 秒", seconds)
                    await asyncio.sleep(seconds + 1)
                    if attempt == 1:
                        return
                except Exception as exc:
                    LOGGER.error(
                        "单条消息处理失败：chat_id=%s message_id=%s error=%s",
                        event.chat_id,
                        getattr(event.message, "id", None),
                        type(exc).__name__,
                    )
                    return

        try:
            count = await sync_available_chats(client, database)
            LOGGER.info("已同步账号可见会话：%s 个", count)
        except FloodWaitError as exc:
            LOGGER.warning("首次同步会话遇到 FloodWait：%s 秒", exc.seconds)
        except Exception as exc:
            LOGGER.warning("首次同步账号会话失败：%s", type(exc).__name__)

        maintenance_task = asyncio.create_task(
            maintenance_loop(settings, database, stop_event),
            name="maintenance-loop",
        )
        heartbeat_task = asyncio.create_task(
            heartbeat_loop(settings, database, client, int(me.id), stop_event),
            name="listener-heartbeat",
        )
        chat_sync_task = asyncio.create_task(
            chat_sync_loop(client, database, stop_event),
            name="chat-sync-loop",
        )
        runtime = database.get_runtime_config()
        watch_count = len(runtime["watch_chat_ids"]) if runtime else len(settings.watch_chat_ids)
        LOGGER.info(
            "只读监听已启动：账号 ID=%s，监控会话数=%s",
            me.id,
            watch_count,
        )

        while not stop_event.is_set():
            if not client.is_connected():
                try:
                    await client.connect()
                    LOGGER.info("已重新连接 Telegram")
                except FloodWaitError as exc:
                    await asyncio.sleep(max(1, int(exc.seconds)) + 1)
                    continue
                except (OSError, ConnectionError):
                    LOGGER.warning("Telegram 连接失败，5 秒后重试")
                    await asyncio.sleep(5)
                    continue

            disconnected = asyncio.ensure_future(client.disconnected)
            stopping = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {disconnected, stopping},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stopping in done and stop_event.is_set():
                break
            LOGGER.warning("Telegram 连接已断开，5 秒后重连")
            await asyncio.sleep(5)

        stop_event.set()
        maintenance_task.cancel()
        heartbeat_task.cancel()
        chat_sync_task.cancel()
        await asyncio.gather(
            maintenance_task,
            heartbeat_task,
            chat_sync_task,
            return_exceptions=True,
        )
    finally:
        if "me" in locals() and me is not None:
            runtime = database.get_runtime_config()
            watch_count = len(runtime["watch_chat_ids"]) if runtime else len(settings.watch_chat_ids)
            database.set_listener_heartbeat(
                connected=False,
                self_id=int(me.id),
                watch_count=watch_count,
                now=utc_now(),
            )
        if client.is_connected():
            await client.disconnect()
        await feedback_collector.close()
        await newsletter_poller.close()
        await mastodon_poller.close()
        await bluesky_poller.close()
        await hacker_news_poller.close()
        await vendor_status_poller.close()
        await nvd_cve_poller.close()
        await cisa_kev_poller.close()
        await github_advisories_poller.close()
        await github_releases_poller.close()
        await feed_poller.close()
        await analysis_queue.close()
        await delivery_queue.close()
        await model_client.close()
        await pusher.close()
        database.close()
        LOGGER.info("服务已停止")


async def wait_until_stopped() -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    LOGGER.info("Collector disabled; waiting for configuration or session migration")
    await stopped.wait()


def main() -> None:
    configure_logging()
    if os.getenv("COLLECTOR_ENABLED", "true").lower() == "false":
        asyncio.run(wait_until_stopped())
        return
    try:
        settings = Settings.from_env(require_push=False, require_watch=False)
        asyncio.run(run_service(settings))
    except (ValueError, RuntimeError) as exc:
        LOGGER.error("启动失败：%s", exc)
        raise SystemExit(1) from None
    except Exception as exc:
        LOGGER.error("服务异常退出：%s", type(exc).__name__)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
