"""Вопросы учеников: фото задачи + что непонятно.

Каждый вопрос сохраняется в базу (для /export) и сразу пересылается в чат преподавателя,
где его можно просто листать как ленту.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Message,
    ReplyParameters,
)

from ..access import is_active, now_ts, pay_button, user_label
from ..config import Settings
from ..db import Database

log = logging.getLogger(__name__)
router = Router(name="questions")
router.message.filter(F.chat.type == "private")

ALBUM_WAIT = 1.5          # сек: ждём, пока Telegram дошлёт все фото альбома
CAPTION_WINDOW = 15 * 60  # сек: текст после фото без подписи считаем подписью к нему
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4000


def _reply_to(message_id: int | None) -> ReplyParameters | None:
    if not message_id:
        return None
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True)


def _text_body(header: str, text: str) -> str:
    """Шапка + текст ученика, обрезанный под лимит сообщения Telegram."""
    return f"{header}\n{html.escape(text[: TEXT_LIMIT - len(header)])}"

# Альбом приходит отдельными сообщениями с общим media_group_id; копим их здесь.
_albums: dict[str, list[Message]] = {}


def _file_of(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return "photo", message.photo[-1].file_id  # последний элемент — самый большой размер
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return "document", message.document.file_id
    return None


async def _allowed(message: Message, db: Database, settings: Settings) -> bool:
    if not settings.questions_subscribers_only or message.from_user.id in settings.admin_ids:
        return True
    user = await db.get_user(message.from_user.id)
    if is_active(user, now_ts()):
        return True
    await message.answer(
        "Вопросы по задачам принимаю от участников канала. Включите пробный период или оплатите подписку — /start",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[pay_button(settings)]]),
    )
    return False


def _header(message: Message) -> str:
    user = message.from_user
    return f"❓ {user_label(user.id, user.full_name, user.username)}"


async def _forward(bot: Bot, settings: Settings, messages: list[Message], caption: str | None) -> int | None:
    """Отправляет вопрос в чат преподавателя. Возвращает id первого сообщения там."""
    if settings.questions_chat_id is None:
        return None
    chat_id = settings.questions_chat_id
    header = _header(messages[0])
    body = f"{header}\n{html.escape(caption)}" if caption else header
    files = [_file_of(m) for m in messages]
    files = [f for f in files if f]
    if not files:
        sent = await bot.send_message(chat_id, _text_body(header, caption or ""))
        return sent.message_id

    fits = len(body) <= CAPTION_LIMIT
    first_caption = body if fits else header
    if len(files) == 1:
        kind, file_id = files[0]
        send = bot.send_photo if kind == "photo" else bot.send_document
        sent = await send(chat_id, file_id, caption=first_caption)
        first_id = sent.message_id
    else:
        media = []
        for index, (kind, file_id) in enumerate(files):
            cls = InputMediaPhoto if kind == "photo" else InputMediaDocument
            media.append(cls(media=file_id, caption=first_caption if index == 0 else None))
        sent_group = await bot.send_media_group(chat_id, media)
        first_id = sent_group[0].message_id
    if not fits:
        await bot.send_message(chat_id, _text_body(header, caption), reply_parameters=_reply_to(first_id))
    return first_id


async def _save_and_forward(bot: Bot, db: Database, settings: Settings, messages: list[Message]) -> None:
    messages.sort(key=lambda m: m.message_id)
    caption = next((m.caption for m in messages if m.caption), None)
    now = now_ts()
    ids = []
    for index, m in enumerate(messages):
        kind, file_id = _file_of(m)
        ids.append(
            await db.add_question(
                m.from_user.id, kind, file_id, caption if index == 0 else None, m.media_group_id, now
            )
        )
    try:
        group_message_id = await _forward(bot, settings, messages, caption)
    except TelegramAPIError:
        log.exception("Не удалось переслать вопрос в чат преподавателя")
        group_message_id = None
    if group_message_id:
        await db.set_question_group_message(ids[0], group_message_id)

    count = "Фото получил" if len(messages) == 1 else f"Получил фото: {len(messages)}"
    if caption:
        await messages[0].answer(f"{count}, вопрос передал преподавателю ✅")
    else:
        await messages[0].answer(f"{count}. Напишите следующим сообщением, что именно непонятно.")


async def _flush_album(bot: Bot, db: Database, settings: Settings, group_id: str) -> None:
    await asyncio.sleep(ALBUM_WAIT)
    messages = _albums.pop(group_id, [])
    if messages:
        try:
            await _save_and_forward(bot, db, settings, messages)
        except Exception:
            log.exception("Ошибка при обработке альбома %s", group_id)


@router.message(F.photo | F.document)
async def on_media(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    if _file_of(message) is None:
        await message.answer("Пришлите, пожалуйста, задачу картинкой (фото или файл-изображение).")
        return
    group_id = message.media_group_id
    if group_id and group_id in _albums:
        _albums[group_id].append(message)  # проверку доступа прошло первое фото альбома
        return
    if not await _allowed(message, db, settings):
        return
    if group_id:
        _albums[group_id] = [message]
        asyncio.create_task(_flush_album(bot, db, settings, group_id))
        return
    await _save_and_forward(bot, db, settings, [message])


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    if not await _allowed(message, db, settings):
        return
    now = now_ts()
    text = message.text.strip()
    last = await db.last_question(message.from_user.id)
    # Ищем начало последнего вопроса: у альбома подпись хранится в первой записи.
    if last and last["kind"] != "text" and now - last["created_at"] <= CAPTION_WINDOW:
        first = last
        if last["media_group_id"]:
            rows = await db.questions_since(last["created_at"] - 5)
            same = [r for r in rows if r["media_group_id"] == last["media_group_id"]]
            first = same[0] if same else last
        if not first["caption"]:
            await db.set_question_caption(first["id"], text)
            if settings.questions_chat_id is not None:
                try:
                    await bot.send_message(
                        settings.questions_chat_id,
                        _text_body(_header(message), text),
                        reply_parameters=_reply_to(first["group_message_id"]),
                    )
                except TelegramAPIError:
                    log.exception("Не удалось переслать подпись к вопросу")
            await message.answer("Записал, вопрос передал преподавателю ✅")
            return

    question_id = await db.add_question(message.from_user.id, "text", None, text, None, now)
    if settings.questions_chat_id is not None:
        try:
            sent = await bot.send_message(
                settings.questions_chat_id, _text_body(_header(message), text)
            )
            await db.set_question_group_message(question_id, sent.message_id)
        except TelegramAPIError:
            log.exception("Не удалось переслать вопрос в чат преподавателя")
    await message.answer("Вопрос передал преподавателю ✅ Если есть фото задачи — пришлите и его.")
