"""Логика доступа: вход в канал, истечение, напоминания, зачисление платежей.

Telegram ничего не знает о «подписке до такого-то числа» — срок живёт в нашей базе,
а бот как администратор канала одобряет заявки и убирает тех, у кого срок вышел.
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Settings
from .db import DAY, Database
from .yookassa import YkPayment, YooKassa, YooKassaError, build_receipt

log = logging.getLogger(__name__)

REMIND_BEFORE = DAY            # за сколько до конца напоминать
PAYMENT_REUSE_WINDOW = 30 * 60  # сколько секунд переиспользуем уже выданную ссылку на оплату
PAYMENT_GIVE_UP = 2 * DAY       # после этого перестаём опрашивать зависший платёж
INVITE_KEY = "invite_link"


def now_ts() -> int:
    return int(time.time())


def fmt_dt(ts: int, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")


def fmt_price(price: Decimal) -> str:
    return f"{price:.0f}" if price == price.to_integral_value() else f"{price:.2f}"


def days_word(n: int) -> str:
    """1 день, 3 дня, 30 дней."""
    if n % 10 == 1 and n % 100 != 11:
        word = "день"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


def is_active(user, now: int) -> bool:
    return bool(user and user["access_until"] and user["access_until"] > now)


def user_label(user_id: int, full_name: str | None, username: str | None) -> str:
    name = html.escape(full_name or "без имени")
    label = f'<a href="tg://user?id={user_id}">{name}</a>'
    if username:
        label += f" (@{html.escape(username)})"
    return label


# ---------- клавиатуры ----------

def pay_button(settings: Settings, *, renew: bool = False) -> InlineKeyboardButton:
    verb = "Продлить" if renew else "Оплатить"
    return InlineKeyboardButton(
        text=f"💳 {verb}: {days_word(settings.sub_days)} — {fmt_price(settings.price)} ₽", callback_data="pay"
    )


def main_keyboard(settings: Settings, user, now: int, invite_link: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    active = is_active(user, now)
    if active and invite_link:
        rows.append([InlineKeyboardButton(text="➡️ Войти в канал", url=invite_link)])
    if not active and user and user["trial_started_at"] is None and user["access_until"] is None:
        rows.append(
            [InlineKeyboardButton(text=f"🎁 {days_word(settings.trial_days)} бесплатно", callback_data="trial")]
        )
    rows.append([pay_button(settings, renew=active)])
    rows.append([InlineKeyboardButton(text="❓ Вопрос по задаче", callback_data="ask")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- канал ----------

async def get_invite_link(bot: Bot, db: Database, settings: Settings) -> str:
    """Одна общая ссылка «по заявке». Утечка не страшна: заявку одобряет бот, сверяясь с базой."""
    # Ключ привязан к id канала: если бота перенесут на другой канал, ссылка создастся заново.
    key = f"{INVITE_KEY}:{settings.channel_id}"
    link = await db.kv_get(key)
    if link:
        return link
    created = await bot.create_chat_invite_link(
        settings.channel_id, name="Вход через бота", creates_join_request=True
    )
    await db.kv_set(key, created.invite_link)
    return created.invite_link


async def remove_from_channel(bot: Bot, settings: Settings, user_id: int) -> bool:
    """Убирает человека из канала. Администраторов и владельца не трогает."""
    if user_id in settings.admin_ids:
        return False
    member = await bot.get_chat_member(settings.channel_id, user_id)
    if member.status in {"creator", "administrator"}:
        return False
    if member.status in {"left", "kicked"}:
        return True
    # ban + unban = «исключить без чёрного списка»: после оплаты человек сможет вернуться.
    await bot.ban_chat_member(settings.channel_id, user_id)
    await bot.unban_chat_member(settings.channel_id, user_id, only_if_banned=True)
    return True


async def _safe_send(bot: Bot, user_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except TelegramForbiddenError:
        return False  # человек заблокировал бота — это нормально
    except TelegramAPIError:
        log.exception("Не удалось написать пользователю %s", user_id)
        return False


async def notify_admins(bot: Bot, settings: Settings, text: str) -> None:
    for admin_id in settings.admin_ids:
        await _safe_send(bot, admin_id, text)


async def expire_users(bot: Bot, db: Database, settings: Settings, now: int | None = None) -> int:
    now = now_ts() if now is None else now
    removed = 0
    for user in await db.expired_users(now):
        user_id = user["user_id"]
        try:
            await remove_from_channel(bot, settings, user_id)
        except TelegramAPIError:
            # Не помечаем — попробуем ещё раз на следующем проходе.
            log.exception("Не удалось убрать %s из канала", user_id)
            continue
        await db.mark_removed(user_id, now)
        removed += 1
        await _safe_send(
            bot,
            user_id,
            "Срок доступа к каналу закончился. Чтобы вернуться, оплатите подписку — "
            "сразу после оплаты пришлю ссылку для входа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[pay_button(settings)]]),
        )
    return removed


async def send_reminders(bot: Bot, db: Database, settings: Settings, now: int | None = None) -> int:
    now = now_ts() if now is None else now
    sent = 0
    for user in await db.users_to_remind(now, REMIND_BEFORE):
        await db.mark_reminded(user["user_id"], user["access_until"])
        ok = await _safe_send(
            bot,
            user["user_id"],
            f"Доступ к каналу действует до {fmt_dt(user['access_until'], settings.tz)} (МСК). "
            "Чтобы не потерять его, продлите подписку — оставшиеся дни не сгорят.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[pay_button(settings, renew=True)]]),
        )
        sent += int(ok)
    return sent


# ---------- оплата ----------

async def create_payment_link(
    bot_username: str, db: Database, yk: YooKassa, settings: Settings, user, now: int | None = None
) -> tuple[str, str]:
    """Возвращает (id платежа в нашей базе, ссылка на страницу оплаты ЮKassa)."""
    now = now_ts() if now is None else now
    existing = await db.reusable_payment(user["user_id"], settings.price, now - PAYMENT_REUSE_WINDOW)
    if existing:
        return existing["id"], existing["confirmation_url"]

    payment_id = await db.create_payment(user["user_id"], settings.price, settings.sub_days, now)
    description = f"{settings.product_name}, {days_word(settings.sub_days)}"
    receipt = None
    if settings.send_receipt:
        receipt = build_receipt(
            email=user["email"],
            description=description,
            amount=settings.price,
            vat_code=settings.vat_code,
            tax_system_code=settings.tax_system_code,
        )
    try:
        remote = await yk.create_payment(
            idempotence_key=payment_id,
            amount=settings.price,
            description=description,
            return_url=f"https://t.me/{bot_username}",
            metadata={"user_id": str(user["user_id"]), "payment": payment_id},
            receipt=receipt,
        )
    except YooKassaError:
        await db.close_payment(payment_id, "canceled")
        raise
    if not remote.confirmation_url:
        await db.close_payment(payment_id, "canceled")
        raise YooKassaError("ЮKassa не вернула ссылку на оплату")
    await db.set_payment_remote(payment_id, remote.id, remote.confirmation_url)
    return payment_id, remote.confirmation_url


def _payment_matches(payment, remote: YkPayment) -> bool:
    """Сверяем сумму, валюту и владельца, прежде чем открывать доступ."""
    return (
        remote.paid
        and remote.currency == "RUB"
        and remote.amount == Decimal(payment["amount"])
        and remote.metadata.get("user_id") == str(payment["user_id"])
    )


async def sync_payment(
    bot: Bot, db: Database, yk: YooKassa, settings: Settings, payment, now: int | None = None
) -> str:
    """Спрашивает у ЮKassa статус платежа и применяет его. Возвращает итоговый статус."""
    now = now_ts() if now is None else now
    if payment["status"] != "pending":
        return payment["status"]
    remote = await yk.get_payment(payment["yk_id"])

    if remote.status == "succeeded":
        if not _payment_matches(payment, remote):
            # Закрываем платёж отдельным статусом, чтобы не дёргать админа на каждом проходе.
            if await db.close_payment(payment["id"], "mismatch"):
                log.error("Платёж %s не совпал с ожидаемым: %s", payment["id"], remote)
                await notify_admins(
                    bot,
                    settings,
                    f"⚠️ Платёж {payment['yk_id']} оплачен, но сумма или плательщик не совпали с ожидаемыми. "
                    "Доступ не выдан — проверьте платёж в кабинете ЮKassa и при необходимости выдайте доступ через /grant.",
                )
            return "mismatch"
        until = await db.credit_payment(payment["id"], now)
        if until is not None:  # None — другой обработчик уже зачислил этот платёж
            await on_paid(bot, db, settings, payment["user_id"], until, payment["amount"])
        return "succeeded"

    if remote.status == "canceled":
        await db.close_payment(payment["id"], "canceled")
        return "canceled"

    if now - payment["created_at"] > PAYMENT_GIVE_UP:
        await db.close_payment(payment["id"], "expired")
        return "expired"
    return "pending"


async def on_paid(bot: Bot, db: Database, settings: Settings, user_id: int, until: int, amount: str) -> None:
    link = await get_invite_link(bot, db, settings)
    await _safe_send(
        bot,
        user_id,
        f"✅ Оплата получена. Доступ к каналу открыт до {fmt_dt(until, settings.tz)} (МСК).\n\n"
        "Если вы ещё не в канале — нажмите кнопку ниже, заявку одобрю автоматически.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➡️ Войти в канал", url=link)]]
        ),
    )
    user = await db.get_user(user_id)
    await notify_admins(
        bot,
        settings,
        f"💰 Оплата {amount} ₽ от {user_label(user_id, user['full_name'], user['username'])}, "
        f"доступ до {fmt_dt(until, settings.tz)}",
    )


async def sync_pending_payments(bot: Bot, db: Database, yk: YooKassa, settings: Settings) -> None:
    for payment in await db.pending_payments():
        try:
            await sync_payment(bot, db, yk, settings, payment)
        except YooKassaError as exc:
            log.warning("Не удалось проверить платёж %s: %s", payment["id"], exc)