"""Заявки на вступление в канал: одобряем только тех, у кого есть действующий доступ."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatJoinRequest, InlineKeyboardMarkup, Message

from .. import access
from ..access import is_active, now_ts
from ..config import Settings
from ..db import Database

log = logging.getLogger(__name__)
router = Router(name="join")


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, bot: Bot, db: Database, settings: Settings) -> None:
    if request.chat.id != settings.channel_id:
        return
    user_id = request.from_user.id
    user = await db.get_user(user_id)
    if user_id in settings.admin_ids or is_active(user, now_ts()):
        await request.approve()
        return
    await request.decline()
    try:
        # user_chat_id позволяет написать подавшему заявку, даже если он не запускал бота.
        await bot.send_message(
            request.user_chat_id,
            "Заявку в канал пришлось отклонить: действующего доступа нет. "
            "Нажмите /start, чтобы включить пробный период или оплатить подписку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[access.pay_button(settings)]]),
        )
    except TelegramAPIError:
        log.info("Не удалось написать %s после отклонения заявки", user_id)


@router.channel_post(F.text == "/chatid")
async def channel_chat_id(post: Message) -> None:
    """Помощь при настройке: писать в канал могут только его админы, так что это безопасно."""
    await post.answer(f"id этого канала: <code>{post.chat.id}</code>")
