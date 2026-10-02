"""Личный чат с учеником: старт, пробный период, оплата, статус."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import access
from ..access import days_word, fmt_dt, fmt_price, is_active, now_ts
from ..config import Settings
from ..db import Database
from ..yookassa import YooKassa, YooKassaError

log = logging.getLogger(__name__)
router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class PayStates(StatesGroup):
    email = State()


async def _menu(bot: Bot, db: Database, settings: Settings, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    now = now_ts()
    user = await db.get_user(user_id)
    active = is_active(user, now)
    link = await access.get_invite_link(bot, db, settings) if active else None
    if active:
        status = f"Доступ к каналу открыт до <b>{fmt_dt(user['access_until'], settings.tz)}</b> (МСК)."
    elif user["access_until"]:
        status = "Срок доступа закончился. Оплатите подписку, чтобы вернуться в канал."
    else:
        status = (
            f"Можно попробовать бесплатно: {days_word(settings.trial_days)} доступа, "
            f"дальше — {fmt_price(settings.price)} ₽ за {days_word(settings.sub_days)}."
        )
    return status, access.main_keyboard(settings, user, now, link)


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    status, keyboard = await _menu(bot, db, settings, message.from_user.id)
    await message.answer(
        "Привет! Через меня можно получить доступ к закрытому каналу и задать вопрос по задаче.\n\n"
        f"{status}\n\n"
        "Чтобы задать вопрос — просто пришлите сюда фото задачи и напишите, что непонятно.",
        reply_markup=keyboard,
    )


@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    status, keyboard = await _menu(bot, db, settings, message.from_user.id)
    await message.answer(status, reply_markup=keyboard)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил. Меню — /start")


@router.callback_query(F.data == "ask")
async def cb_ask(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "Пришлите фото задачи (можно несколько) и подпишите, что именно непонятно. "
        "Можно и просто текстом. Всё передам преподавателю."
    )


@router.callback_query(F.data == "trial")
async def cb_trial(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    until = await db.start_trial(callback.from_user.id, settings.trial_days, now_ts())
    await callback.answer()
    if until is None:
        await callback.message.answer("Пробный период уже был использован. Меню — /start")
        return
    link = await access.get_invite_link(bot, db, settings)
    await callback.message.answer(
        f"🎁 Пробный доступ открыт до <b>{fmt_dt(until, settings.tz)}</b> (МСК).\n\n"
        "Нажмите кнопку и подайте заявку — одобрю её автоматически. "
        "За день до конца напомню про оплату.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➡️ Войти в канал", url=link)]]
        ),
    )


async def _send_pay_link(
    message: Message, bot: Bot, db: Database, yk: YooKassa, settings: Settings, user_id: int
) -> None:
    user = await db.get_user(user_id)
    me = await bot.me()
    try:
        payment_id, url = await access.create_payment_link(me.username, db, yk, settings, user)
    except YooKassaError as exc:
        log.error("Не удалось создать платёж для %s: %s", user_id, exc)
        await message.answer(
            "Не получилось создать счёт на оплату — попробуйте ещё раз через пару минут."
            + (f" Если не поможет, напишите {settings.support_contact}." if settings.support_contact else "")
        )
        return
    text = (
        f"<b>{settings.product_name}</b>\n"
        f"{days_word(settings.sub_days)} — {fmt_price(settings.price)} ₽\n\n"
        "Оплата проходит на защищённой странице ЮKassa (карта, СБП, SberPay). "
        "Доступ откроется автоматически, обычно в течение минуты после оплаты."
    )
    if settings.offer_url:
        text += f'\n\nОплачивая, вы принимаете <a href="{settings.offer_url}">условия оферты</a>.'
    await message.answer(
        text,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Перейти к оплате", url=url)],
                [InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data=f"check:{payment_id}")],
            ]
        ),
    )


@router.callback_query(F.data == "pay")
async def cb_pay(
    callback: CallbackQuery, bot: Bot, db: Database, yk: YooKassa, settings: Settings, state: FSMContext
) -> None:
    await callback.answer()
    user = await db.get_user(callback.from_user.id)
    if settings.send_receipt and not user["email"]:
        await state.set_state(PayStates.email)
        await callback.message.answer(
            "Напишите e-mail — на него придёт кассовый чек об оплате. Отмена — /cancel"
        )
        return
    await _send_pay_link(callback.message, bot, db, yk, settings, callback.from_user.id)


@router.message(PayStates.email, F.text)
async def got_email(
    message: Message, bot: Bot, db: Database, yk: YooKassa, settings: Settings, state: FSMContext
) -> None:
    email = message.text.strip()
    if len(email) > 254 or not EMAIL_RE.match(email):
        await message.answer("Не похоже на e-mail. Пришлите адрес вида name@example.com или /cancel")
        return
    await db.set_email(message.from_user.id, email)
    await state.clear()
    await _send_pay_link(message, bot, db, yk, settings, message.from_user.id)


@router.callback_query(F.data.startswith("check:"))
async def cb_check(callback: CallbackQuery, bot: Bot, db: Database, yk: YooKassa, settings: Settings) -> None:
    payment = await db.get_payment(callback.data.split(":", 1)[1])
    if payment is None or payment["user_id"] != callback.from_user.id:
        await callback.answer("Счёт не найден. Создайте новый через /start", show_alert=True)
        return
    try:
        status = await access.sync_payment(bot, db, yk, settings, payment)
    except YooKassaError as exc:
        log.warning("Проверка платежа %s: %s", payment["id"], exc)
        await callback.answer("Не удалось связаться с ЮKassa, попробуйте через минуту.", show_alert=True)
        return
    if status == "succeeded":
        # Сообщение об успехе отправляет access.on_paid; здесь только гасим «часики».
        await callback.answer("Оплата получена ✅")
    elif status == "pending":
        await callback.answer(
            "Оплата пока не поступила. Если вы уже оплатили — подождите минуту, доступ откроется сам.",
            show_alert=True,
        )
    elif status == "mismatch":
        await callback.answer("С платежом что-то не так, преподаватель уже в курсе.", show_alert=True)
    else:
        await callback.answer("Этот счёт отменён или истёк. Создайте новый через /start", show_alert=True)
