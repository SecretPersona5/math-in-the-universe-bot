"""Запуск: `python -m bot` — работа, `python -m bot check` — проверка настроек,
`python -m bot ids` — показать id канала, группы и свой (нужен только BOT_TOKEN)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, TelegramObject, Update
from dotenv import load_dotenv

from . import access
from .access import now_ts
from .config import ConfigError, Settings, load_settings
from .db import Database
from .handlers import admin, join, questions, user
from .yookassa import YooKassa, YooKassaError

log = logging.getLogger("bot")

NO_TELEGRAM = (
    "Не удалось связаться с api.telegram.org: {error}\n"
    "Скорее всего, Telegram с этой машины недоступен. Включите VPN или впишите прокси "
    "в PROXY_URL в файле .env (например socks5://127.0.0.1:1080) и запустите снова."
)

PAYMENTS_EVERY = 20  # сек: как часто спрашиваем ЮKassa о неоплаченных счетах
ACCESS_EVERY = 60    # сек: как часто проверяем истёкшие доступы и напоминания


class RememberUser(BaseMiddleware):
    """Заводит/обновляет запись о человеке при любом его действии в личке."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        chat = data.get("event_chat")
        if tg_user is not None and not tg_user.is_bot and chat is not None and chat.type == "private":
            db: Database = data["db"]
            await db.upsert_user(tg_user.id, tg_user.username, tg_user.full_name, now_ts())
        return await handler(event, data)


def make_bot(settings: Settings) -> Bot:
    session = AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None
    return Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp.update.outer_middleware(RememberUser())
    # Порядок важен: команды преподавателя, заявки в канал, меню ученика и в самом конце —
    # «всё остальное считаем вопросом».
    dp.include_routers(admin.setup(settings), join.router, user.router, questions.router)
    return dp


async def background(bot: Bot, db: Database, yk: YooKassa, settings: Settings) -> None:
    last_access_check = 0
    while True:
        try:
            await access.sync_pending_payments(bot, db, yk, settings)
            if now_ts() - last_access_check >= ACCESS_EVERY:
                last_access_check = now_ts()
                await access.send_reminders(bot, db, settings)
                await access.expire_users(bot, db, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в фоновой задаче")
        await asyncio.sleep(PAYMENTS_EVERY)


async def run(settings: Settings) -> None:
    db = Database(settings.db_path)
    await db.connect()
    yk = YooKassa(settings.yk_shop_id, settings.yk_secret)
    bot = make_bot(settings)

    dp = build_dispatcher(settings)
    dp["db"], dp["yk"] = db, yk

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Меню и доступ к каналу"),
            BotCommand(command="status", description="До какого числа доступ"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    worker = asyncio.create_task(background(bot, db, yk, settings))
    try:
        log.info("Бот запущен")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await yk.close()
        await db.close()
        await bot.session.close()


async def check(settings: Settings) -> bool:
    """Проверяет токен, права бота в канале, чат для вопросов и ключи ЮKassa."""
    ok = True
    bot = make_bot(settings)
    yk = YooKassa(settings.yk_shop_id, settings.yk_secret, retries=1)
    try:
        try:
            me = await bot.me()
            print(f"[ok] Telegram: бот @{me.username}")
            chat = await bot.get_chat(settings.channel_id)
            member = await bot.get_chat_member(settings.channel_id, me.id)
            can_invite = getattr(member, "can_invite_users", False)
            can_ban = getattr(member, "can_restrict_members", False)
            if member.status != "administrator" or not (can_invite and can_ban):
                ok = False
                print(
                    f"[!!] Канал «{chat.title}»: бот должен быть администратором с правами "
                    "«Добавление подписчиков» и «Блокировка пользователей»"
                )
            else:
                print(f"[ok] Канал «{chat.title}»: права администратора есть")
            if settings.questions_chat_id is None:
                print("[--] QUESTIONS_CHAT_ID не задан: вопросы будут только в базе и в /export")
            else:
                qchat = await bot.get_chat(settings.questions_chat_id)
                print(f"[ok] Чат для вопросов: «{qchat.title or qchat.full_name}»")
        except Exception as exc:
            ok = False
            print(f"[!!] Telegram: {type(exc).__name__}: {exc}")
        try:
            shop = await yk.me()
            mode = "ТЕСТОВЫЙ магазин" if shop.get("test") else "боевой магазин"
            print(f"[ok] ЮKassa: {mode}, id {shop.get('account_id')}, статус {shop.get('status')}")
        except YooKassaError as exc:
            ok = False
            print(f"[!!] ЮKassa: {exc}")
    finally:
        await yk.close()
        await bot.session.close()
    return ok


def describe_update(update: Update) -> str | None:
    """Строка для режима `ids`: какой чат и какой у него id."""
    if update.my_chat_member:
        event = update.my_chat_member
        kind = {"channel": "канал", "supergroup": "группа", "group": "группа"}.get(event.chat.type)
        if kind is None:
            return None
        status = event.new_chat_member.status
        rights = ""
        if status == "administrator":
            invite = getattr(event.new_chat_member, "can_invite_users", False)
            ban = getattr(event.new_chat_member, "can_restrict_members", False)
            rights = " — права для канала в порядке" if invite and ban else (
                " — НЕ ХВАТАЕТ ПРАВ: нужны «Добавление подписчиков» и «Блокировка пользователей»"
            )
        return f"{kind} «{event.chat.title}»: id {event.chat.id}, бот теперь {status}{rights}"
    message = update.message or update.channel_post
    if message is None:
        return None
    if message.chat.type == "private":
        return f"личка: ваш id {message.from_user.id} ({message.from_user.full_name}) — это значение для ADMIN_IDS"
    kind = "канал" if message.chat.type == "channel" else "группа"
    return f"{kind} «{message.chat.title}»: id {message.chat.id}"


async def show_ids() -> None:
    """Печатает id чатов по мере того, как бота куда-то добавляют или ему пишут."""
    load_dotenv(".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Впишите BOT_TOKEN в файл .env")
    proxy = os.getenv("PROXY_URL", "").strip() or None
    bot = Bot(token, session=AiohttpSession(proxy=proxy) if proxy else None)
    try:
        me = await bot.me()
        print(f"Бот @{me.username} слушает. Не закрывая это окно:")
        print("  1) напишите боту в личку любое сообщение — покажу ваш id;")
        print("  2) добавьте бота администратором в канал — покажу id канала;")
        print("  3) добавьте бота в группу для вопросов — покажу id группы.")
        print("Выход — Ctrl+C.\n")
        offset = None
        while True:
            updates = await bot.get_updates(
                offset=offset,
                timeout=25,
                allowed_updates=["message", "channel_post", "my_chat_member"],
                request_timeout=40,
            )
            for update in updates:
                offset = update.update_id + 1
                line = describe_update(update)
                if line:
                    print(line, flush=True)
    finally:
        await bot.session.close()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "ids":
        try:
            asyncio.run(show_ids())
        except KeyboardInterrupt:
            pass
        except TelegramUnauthorizedError:
            sys.exit("Telegram не принял токен: проверьте BOT_TOKEN в .env (скопируйте заново у @BotFather).")
        except TelegramNetworkError as exc:
            sys.exit(NO_TELEGRAM.format(error=exc))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.exit(f"Ошибка настроек: {exc}")
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        sys.exit(0 if asyncio.run(check(settings)) else 1)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except TelegramUnauthorizedError:
        sys.exit("Telegram не принял токен: проверьте BOT_TOKEN в .env (скопируйте заново у @BotFather).")
    except TelegramNetworkError as exc:
        sys.exit(NO_TELEGRAM.format(error=exc))


if __name__ == "__main__":
    main()