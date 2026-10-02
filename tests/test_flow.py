"""Сквозные сценарии: настоящий диспетчер aiogram, вместо сети Telegram — заглушка.

Проверяют то, что юнит-тесты не ловят: фильтры, порядок роутеров, передачу зависимостей.
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    ApproveChatJoinRequest,
    BanChatMember,
    CreateChatInviteLink,
    DeclineChatJoinRequest,
    GetChatMember,
    GetMe,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    UnbanChatMember,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatInviteLink,
    ChatJoinRequest,
    ChatMemberMember,
    Message,
    PhotoSize,
    Update,
    User,
)

from bot.access import now_ts
from bot.db import DAY
from bot.handlers import questions
from bot.main import build_dispatcher

from .conftest import ADMIN, CHANNEL, FakeYooKassa, make_settings

BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="mybot")
QUESTIONS_CHAT = -2002
_ids = itertools.count(100)


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, CreateChatInviteLink):
            return ChatInviteLink(
                invite_link="https://t.me/+invite", creator=BOT_USER, creates_join_request=True,
                is_primary=False, is_revoked=False,
            )
        if isinstance(method, GetChatMember):
            return ChatMemberMember(user=User(id=method.user_id, is_bot=False, first_name="x"))
        if isinstance(method, (SendMessage, SendPhoto)):
            return _msg(method.chat_id, "private" if method.chat_id > 0 else "supergroup", BOT_USER)
        if isinstance(method, SendMediaGroup):
            return [_msg(method.chat_id, "supergroup", BOT_USER) for _ in method.media]
        return True

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


def _msg(chat_id, chat_type, user, **fields) -> Message:
    return Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=user,
        **fields,
    )


def _student(user_id=42) -> User:
    return User(id=user_id, is_bot=False, first_name="Аня", username="anya")


def _photo(file_id="file-1"):
    return [PhotoSize(file_id=file_id + "-small", file_unique_id="s", width=90, height=90),
            PhotoSize(file_id=file_id, file_unique_id="b", width=1280, height=960)]


@pytest.fixture(scope="module")
def settings():
    return make_settings(send_receipt=True)


@pytest.fixture(scope="module")
def dispatcher(settings):
    return build_dispatcher(settings)


@pytest_asyncio.fixture
async def env(db, dispatcher, settings, monkeypatch):
    monkeypatch.setattr(questions, "ALBUM_WAIT", 0.05)
    session = MockSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    yk = FakeYooKassa()
    dispatcher["db"], dispatcher["yk"] = db, yk
    await dispatcher.storage.close()

    class Env:
        pass

    e = Env()
    e.bot, e.db, e.yk, e.session, e.settings = bot, db, yk, session, settings

    async def feed(**kwargs):
        await dispatcher.feed_update(bot, Update(update_id=next(_ids), **kwargs))

    async def text(value, user=None, chat_id=None, chat_type="private"):
        user = user or _student()
        await feed(message=_msg(chat_id or user.id, chat_type, user, text=value))

    async def click(data, user=None):
        user = user or _student()
        await feed(
            callback_query=CallbackQuery(
                id=str(next(_ids)), from_user=user, chat_instance="x", data=data,
                message=_msg(user.id, "private", BOT_USER, text="menu"),
            )
        )

    e.feed, e.text, e.click = feed, text, click
    return e


def _buttons(method) -> list:
    return [b for row in method.reply_markup.inline_keyboard for b in row]


async def test_start_trial_and_join(env):
    await env.text("/start")
    start = env.session.of(SendMessage)[-1]
    assert [b.callback_data for b in _buttons(start)] == ["trial", "pay", "ask"]
    assert "3 дня" in start.text

    await env.click("trial")
    user = await env.db.get_user(42)
    assert user["full_name"] == "Аня" and user["access_until"] > now_ts() + 3 * DAY - 60
    invite = env.session.of(CreateChatInviteLink)[0]
    assert invite.chat_id == CHANNEL and invite.creates_join_request is True
    assert _buttons(env.session.of(SendMessage)[-1])[0].url == "https://t.me/+invite"

    # второй раз триал не выдаётся
    await env.click("trial")
    assert "уже был использован" in env.session.of(SendMessage)[-1].text

    await env.feed(
        chat_join_request=ChatJoinRequest(
            chat=Chat(id=CHANNEL, type="channel"), from_user=_student(), user_chat_id=42,
            date=datetime.now(timezone.utc),
        )
    )
    assert [c.user_id for c in env.session.of(ApproveChatJoinRequest)] == [42]

    # меню после триала: вход в канал и продление, кнопки триала уже нет
    await env.text("/status")
    labels = [(b.url, b.callback_data) for b in _buttons(env.session.of(SendMessage)[-1])]
    assert labels == [("https://t.me/+invite", None), (None, "pay"), (None, "ask")]


async def test_join_request_without_access_is_declined(env):
    stranger = _student(77)
    for chat_id, expected in ((CHANNEL, 1), (-555, 1)):  # чужой чат бот не трогает
        await env.feed(
            chat_join_request=ChatJoinRequest(
                chat=Chat(id=chat_id, type="channel"), from_user=stranger, user_chat_id=77,
                date=datetime.now(timezone.utc),
            )
        )
        assert len(env.session.of(DeclineChatJoinRequest)) == expected
    assert env.session.of(ApproveChatJoinRequest) == []
    assert env.session.of(SendMessage)[-1].chat_id == 77


async def test_payment_with_receipt_email(env):
    await env.text("/start")
    await env.click("pay")
    assert "e-mail" in env.session.of(SendMessage)[-1].text
    assert env.yk.created == []

    await env.text("не почта")
    assert "Не похоже" in env.session.of(SendMessage)[-1].text
    assert await env.db.questions_since(0) == []  # ввод почты не превращается в «вопрос»

    await env.text("parent@example.com")
    created = env.yk.created[0]
    assert created["receipt"]["customer"]["email"] == "parent@example.com"
    assert created["return_url"] == "https://t.me/mybot"
    link_btn, check_btn = _buttons(env.session.of(SendMessage)[-1])
    assert link_btn.url == "https://yoomoney.ru/pay/yk-1"

    # нажал «Я оплатил» раньше времени — доступа нет
    await env.click(check_btn.callback_data)
    assert (await env.db.get_user(42))["access_until"] is None

    env.yk.set_status("yk-1", "succeeded")
    await env.click(check_btn.callback_data)
    assert (await env.db.get_user(42))["access_until"] > now_ts() + 30 * DAY - 60
    texts = [(m.chat_id, m.text) for m in env.session.of(SendMessage)]
    assert any(chat == 42 and "Оплата получена" in t for chat, t in texts)
    assert any(chat == ADMIN and "990.00" in t for chat, t in texts)

    # чужой платёж проверить нельзя
    await env.click(check_btn.callback_data, user=_student(77))
    assert await env.db.get_user(77) is not None and (await env.db.get_user(77))["access_until"] is None


async def test_photo_question_goes_to_teacher(env):
    await env.db.grant_days(42, 30, now_ts())
    await env.feed(message=_msg(42, "private", _student(), photo=_photo(), caption="Не понимаю пункт <б>"))

    sent = env.session.of(SendPhoto)[0]
    assert sent.chat_id == QUESTIONS_CHAT and sent.photo == "file-1"  # берём самый большой размер
    assert "Аня" in sent.caption and "@anya" in sent.caption and "&lt;б&gt;" in sent.caption
    row = (await env.db.questions_since(0))[0]
    assert (row["kind"], row["file_id"], row["caption"]) == ("photo", "file-1", "Не понимаю пункт <б>")
    assert row["group_message_id"] is not None
    assert "передал" in env.session.of(SendMessage)[-1].text


async def test_album_is_forwarded_as_one_question(env):
    await env.db.grant_days(42, 30, now_ts())
    await env.feed(message=_msg(42, "private", _student(), photo=_photo("a"), media_group_id="g1", caption="Обе задачи"))
    await env.feed(message=_msg(42, "private", _student(), photo=_photo("b"), media_group_id="g1"))
    await asyncio.sleep(0.2)

    groups = env.session.of(SendMediaGroup)
    assert len(groups) == 1 and [m.media for m in groups[0].media] == ["a", "b"]
    assert "Обе задачи" in groups[0].media[0].caption and groups[0].media[1].caption is None
    assert len(await env.db.questions_since(0)) == 2
    # ученику — одно подтверждение на весь альбом
    assert sum(1 for m in env.session.of(SendMessage) if m.chat_id == 42) == 1


async def test_text_after_photo_becomes_its_caption(env):
    await env.db.grant_days(42, 30, now_ts())
    await env.feed(message=_msg(42, "private", _student(), photo=_photo()))
    assert "что именно непонятно" in env.session.of(SendMessage)[-1].text

    await env.text("Откуда взялась двойка?")
    rows = await env.db.questions_since(0)
    assert len(rows) == 1 and rows[0]["caption"] == "Откуда взялась двойка?"
    to_teacher = [m for m in env.session.of(SendMessage) if m.chat_id == QUESTIONS_CHAT][-1]
    assert "Откуда взялась двойка?" in to_teacher.text
    assert to_teacher.reply_parameters.message_id == rows[0]["group_message_id"]

    # следующий текст — уже отдельный вопрос
    await env.text("И ещё вопрос без фото")
    assert [r["kind"] for r in await env.db.questions_since(0)] == ["photo", "text"]


async def test_question_without_access_is_refused(env):
    await env.feed(message=_msg(42, "private", _student(), photo=_photo(), caption="вопрос"))
    assert env.session.of(SendPhoto) == [] and await env.db.questions_since(0) == []
    assert "от участников канала" in env.session.of(SendMessage)[-1].text


async def test_admin_commands_are_admin_only(env):
    admin = User(id=ADMIN, is_bot=False, first_name="Учитель")
    await env.text("/grant 42 30", user=_student(77))
    assert await env.db.get_user(42) is None

    await env.text("/grant 42 30", user=admin)
    assert (await env.db.get_user(42))["access_until"] > now_ts() + 30 * DAY - 60

    await env.text("/stats", user=admin)
    assert "С доступом сейчас: 1" in env.session.of(SendMessage)[-1].text

    await env.text("/revoke 42", user=admin)
    assert (await env.db.get_user(42))["access_until"] <= now_ts()
    # закрытие доступа сразу убирает из канала: исключить и тут же снять бан
    assert [c.user_id for c in env.session.of(BanChatMember)] == [42]
    unban = env.session.of(UnbanChatMember)[0]
    assert unban.user_id == 42 and unban.only_if_banned is True


async def test_group_chatter_is_ignored(env):
    """Сообщения в чате преподавателя не должны превращаться в вопросы."""
    await env.text("спасибо, посмотрю", user=User(id=ADMIN, is_bot=False, first_name="Учитель"),
                   chat_id=QUESTIONS_CHAT, chat_type="supergroup")
    assert env.session.calls == [] and await env.db.questions_since(0) == []
