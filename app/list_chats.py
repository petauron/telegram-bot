from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.config import Settings


def _cell(value: object) -> str:
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


async def list_chats() -> None:
    settings = Settings.from_env(require_push=False, require_watch=False)
    settings.ensure_parent_directories(include_database=False)
    client = TelegramClient(
        settings.tg_session_path,
        settings.tg_api_id,
        settings.tg_api_hash,
        auto_reconnect=True,
        connection_retries=None,
        retry_delay=5,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("尚未登录，请先运行 python -m app.login")

        print("类型\t名称\tchat_id\tusername")
        seen: set[int] = set()
        while True:
            try:
                async for dialog in client.iter_dialogs():
                    if not (dialog.is_group or dialog.is_channel) or dialog.id in seen:
                        continue
                    seen.add(dialog.id)
                    entity = dialog.entity
                    kind = "群组" if dialog.is_group else "频道"
                    username = getattr(entity, "username", None)
                    print(
                        f"{kind}\t{_cell(dialog.name)}\t{dialog.id}\t"
                        f"{('@' + username) if username else '-'}"
                    )
                break
            except FloodWaitError as exc:
                seconds = max(1, int(exc.seconds))
                print(f"Telegram 限流，等待 {seconds} 秒后继续……", file=sys.stderr)
                await asyncio.sleep(seconds + 1)
    finally:
        await client.disconnect()


def main() -> None:
    try:
        asyncio.run(list_chats())
    except (ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"读取会话失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n已取消。")


if __name__ == "__main__":
    main()
