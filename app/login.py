from __future__ import annotations

import asyncio
import getpass
import sys

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from app.config import Settings
from app.utils import display_name


async def _sleep_for_flood(exc: FloodWaitError) -> None:
    seconds = max(1, int(exc.seconds))
    print(f"Telegram 要求等待 {seconds} 秒，正在等待……")
    await asyncio.sleep(seconds + 1)


async def login() -> None:
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
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"已登录：{display_name(me, getattr(me, 'id', None))}（ID: {me.id}）")
            print("现有 session 可直接供监听服务使用。")
            return

        while True:
            try:
                await client.send_code_request(settings.tg_phone)
                break
            except FloodWaitError as exc:
                await _sleep_for_flood(exc)

        signed_in = False
        needs_password = False
        for _ in range(3):
            code = getpass.getpass("Telegram 验证码（输入不回显）: ").strip()
            try:
                await client.sign_in(settings.tg_phone, code)
                signed_in = True
                break
            except SessionPasswordNeededError:
                needs_password = True
                break
            except PhoneCodeInvalidError:
                print("验证码不正确，请重试。")
            except PhoneCodeExpiredError:
                print("验证码已过期，请重新运行登录命令。")
                return
            except FloodWaitError as exc:
                await _sleep_for_flood(exc)

        if not signed_in and not needs_password:
            raise RuntimeError("验证码验证未通过，请重新运行登录命令")

        if needs_password and not await client.is_user_authorized():
            for _ in range(3):
                password = getpass.getpass("Telegram 两步验证密码（输入不回显）: ")
                try:
                    await client.sign_in(password=password)
                    signed_in = True
                    break
                except PasswordHashInvalidError:
                    print("两步验证密码不正确，请重试。")
                except FloodWaitError as exc:
                    await _sleep_for_flood(exc)

        if not signed_in and not await client.is_user_authorized():
            raise RuntimeError("登录未完成，请重新运行登录命令")

        me = await client.get_me()
        print(f"登录成功：{display_name(me, getattr(me, 'id', None))}（ID: {me.id}）")
        print("session 已写入持久化 data 目录。")
    finally:
        await client.disconnect()


def main() -> None:
    try:
        asyncio.run(login())
    except (ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"登录失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n已取消。")


if __name__ == "__main__":
    main()
