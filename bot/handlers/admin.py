"""Команды преподавателя (доступны только id из ADMIN_IDS)."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from .. import access
from ..access import days_word, fmt_dt, now_ts, user_label
from ..config import Settings
from ..db import DAY, Database
from ..export import build_export

log = logging.getLogger(__name__)
router = Router(name="admin")

HELP = (
    "<b>Команды преподавателя</b>\n"
    "/stats — сколько людей с доступом, сколько оплат\n"
    "/users — у кого сейчас есть доступ и до какого числа\n"
    "/export [дней|all] — архив всех вопросов с картинками (по умолчанию за 7 дней)\n"
    "/grant &lt;id&gt; &lt;дней&gt; — выдать или продлить доступ вручную\n"
    "/revoke &lt;id&gt; — закрыть доступ\n"
    "/chatid — показать id этого чата (для настройки)"
)


def setup(settings: Settings) -> Router:
    router.message.filter(lambda m: m.from_user is not None and m.from_user.id in settings.admin_ids)
    return router


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    await message.answer(f"id этого чата: <code>{message.chat.id}</code>")


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    s = await db.stats(now_ts())
    await message.answer(
        f"Всего запускали бота: {s['users']}\n"
        f"С доступом сейчас: {s['active']} (из них оплативших: {s['paying']})\n"
        f"Успешных оплат: {s['payments']} на {s['revenue']:.0f} ₽\n"
        f"Вопросов за 7 дней: {s['questions_week']}"
    )


@router.message(Command("users"))
async def cmd_users(message: Message, db: Database, settings: Settings) -> None:
    users = await db.active_users(now_ts())
    if not users:
        await message.answer("Сейчас доступа нет ни у кого.")
        return
    lines = [
        f"{fmt_dt(u['access_until'], settings.tz)} — {user_label(u['user_id'], u['full_name'], u['username'])} "
        f"<code>{u['user_id']}</code>"
        for u in users
    ]
    # Телеграм режет сообщения длиннее 4096 символов — шлём порциями.
    chunk: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > 3500:
            await message.answer("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    await message.answer("\n".join(chunk))


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit() or int(args[1]) < 1:
        await message.answer("Формат: /grant &lt;id пользователя&gt; &lt;дней&gt;")
        return
    user_id, days = int(args[0]), int(args[1])
    until = await db.grant_days(user_id, days, now_ts())
    await message.answer(f"Готово: доступ для <code>{user_id}</code> до {fmt_dt(until, settings.tz)}.")
    try:
        link = await access.get_invite_link(bot, db, settings)
        await bot.send_message(
            user_id,
            f"Преподаватель открыл вам доступ к каналу на {days_word(days)} — до {fmt_dt(until, settings.tz)} (МСК).\n"
            f"Вход: {link}",
        )
    except TelegramAPIError:
        await message.answer("Сообщить человеку не удалось: он ещё не запускал бота.")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /revoke &lt;id пользователя&gt;")
        return
    if not await db.revoke(int(arg), now_ts()):
        await message.answer("У этого пользователя и так нет действующего доступа.")
        return
    removed = await access.expire_users(bot, db, settings)
    await message.answer("Доступ закрыт." + ("" if removed else " Из канала убрать не получилось — проверьте права бота."))


@router.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings) -> None:
    arg = (command.args or "7").strip().lower()
    if arg == "all":
        since, period = 0, "за всё время"
    elif arg.isdigit() and int(arg) > 0:
        since, period = now_ts() - int(arg) * DAY, f"за {days_word(int(arg))}"
    else:
        await message.answer("Формат: /export, /export 30 или /export all")
        return
    notice = await message.answer("Собираю архив, это может занять минуту…")
    data, count, truncated = await build_export(bot, db, since, settings.tz)
    if count == 0:
        await notice.edit_text(f"Вопросов {period} нет.")
        return
    stamp = datetime.now(ZoneInfo(settings.tz)).strftime("%Y-%m-%d")
    await message.answer_document(
        BufferedInputFile(data, filename=f"voprosy-{stamp}.zip"),
        caption=f"Вопросов {period}: {count}. Распакуйте архив и откройте index.html."
        + (" Архив обрезан по размеру — возьмите период короче." if truncated else ""),
    )
    await notice.delete()
