from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from bot import access
from bot.db import DAY

from .conftest import ADMIN, FakeBot, FakeYooKassa, make_settings

NOW = 1_800_000_000
U = 42


async def _user(db, user_id=U):
    await db.upsert_user(user_id, "student", "Ученик <Тест>", NOW)
    return await db.get_user(user_id)


# ---------- пробный период ----------

async def test_trial_is_given_once(db):
    await _user(db)
    assert await db.start_trial(U, 3, NOW) == NOW + 3 * DAY
    assert await db.start_trial(U, 3, NOW + 10) is None
    assert access.is_active(await db.get_user(U), NOW + 3 * DAY - 1)
    assert not access.is_active(await db.get_user(U), NOW + 3 * DAY)


async def test_no_trial_after_paid_access(db):
    """Кто уже платил (или получил доступ вручную), пробный период взять не может."""
    await _user(db)
    await db.grant_days(U, 30, NOW)
    assert await db.start_trial(U, 3, NOW + 40 * DAY) is None


# ---------- оплата ----------

async def test_payment_extends_from_end_of_trial(db):
    bot, yk, settings = FakeBot(), FakeYooKassa(), make_settings()
    user = await _user(db)
    await db.start_trial(U, 3, NOW)

    pid, url = await access.create_payment_link("mybot", db, yk, settings, user, NOW + DAY)
    assert url.startswith("https://yoomoney.ru/")
    sent = yk.created[0]
    assert sent["idempotence_key"] == pid
    assert sent["amount"] == Decimal("990.00")
    assert sent["metadata"] == {"user_id": str(U), "payment": pid}
    assert sent["return_url"] == "https://t.me/mybot"
    assert sent["receipt"] is None

    yk.set_status("yk-1", "succeeded")
    status = await access.sync_payment(bot, db, yk, settings, await db.get_payment(pid), NOW + DAY)
    assert status == "succeeded"
    # 3 дня триала + 30 оплаченных: оставшиеся дни не сгорают
    assert (await db.get_user(U))["access_until"] == NOW + 33 * DAY
    assert any(chat == U and "Оплата получена" in text for chat, text in bot.sent)
    assert any(chat == ADMIN and "990.00" in text for chat, text in bot.sent)


async def test_payment_after_expiry_counts_from_now(db):
    bot, yk, settings = FakeBot(), FakeYooKassa(), make_settings()
    user = await _user(db)
    await db.start_trial(U, 3, NOW)
    later = NOW + 10 * DAY
    pid, _ = await access.create_payment_link("mybot", db, yk, settings, user, later)
    yk.set_status("yk-1", "succeeded")
    await access.sync_payment(bot, db, yk, settings, await db.get_payment(pid), later)
    assert (await db.get_user(U))["access_until"] == later + 30 * DAY


async def test_payment_is_credited_exactly_once(db):
    """Кнопка «Я оплатил» и фоновая проверка приходят одновременно — зачисление одно."""
    bot, yk, settings = FakeBot(), FakeYooKassa(), make_settings()
    user = await _user(db)
    pid, _ = await access.create_payment_link("mybot", db, yk, settings, user, NOW)
    yk.set_status("yk-1", "succeeded")
    payment = await db.get_payment(pid)

    results = await asyncio.gather(
        *(access.sync_payment(bot, db, yk, settings, payment, NOW) for _ in range(5))
    )
    assert set(results) == {"succeeded"}
    assert (await db.get_user(U))["access_until"] == NOW + 30 * DAY
    assert sum(1 for chat, text in bot.sent if chat == U and "Оплата получена" in text) == 1
    # повторная проверка уже зачисленного платежа ничего не меняет
    assert await access.sync_payment(bot, db, yk, settings, await db.get_payment(pid), NOW) == "succeeded"
    assert (await db.get_user(U))["access_until"] == NOW + 30 * DAY


async def test_pending_payment_gives_nothing(db):
    bot, yk, settings = FakeBot(), FakeYooKassa(), make_settings()
    user = await _user(db)
    pid, _ = await access.create_payment_link("mybot", db, yk, settings, user, NOW)
    assert await access.sync_payment(bot, db, yk, settings, await db.get_payment(pid), NOW) == "pending"
    assert (await db.get_user(U))["access_until"] is None


async def test_canceled_payment_is_closed(db):
    bot, yk, settings = FakeBot(), FakeYooKassa(), make_settings()
    user = await _user(db)
    pid, _ = await access.create_payment_link("mybot", db, yk, settings, user, NOW)
    yk.set_status("yk-1", "canceled")
    assert await access.sync_payment(bot, db, yk, settings, await db.get_payment(pid), NOW) == "canceled"
    assert (await db.get_payment(pid))["status"] == "canceled"
    assert await db.pending_payments() == []


@pytest.mark.parametrize(
    "tamper",
    [
        dict(amount=Decimal("1.00")),
        dict(metadata={"user_id": "777"}),
        dict(paid=False),
    ],
)
async def test_mismatched_payment_gives_no_access(db, tamper):
    bot, yk, settings = FakeBot(), FakeYooKassa(), make_settings()
    user = await _user(db)
    pid, _ = await access.create_payment_link("mybot", db, yk, settings, user, NOW)
    yk.set_status("yk-1", "succeeded", **tamper)
    for _ in range(3):
        status = await access.sync_payment(bot, db, yk, settings, await db.get_payment(pid), NOW)
    assert status == "mismatch"
    assert (await db.get_user(U))["access_until"] is None
    # админа предупреждаем один раз, а не на каждом проходе
    assert sum(1 for chat, _ in bot.sent if chat == ADMIN) == 1


async def test_pay_link_is_reused_within_window(db):
    yk, settings = FakeYooKassa(), make_settings()
    user = await _user(db)
    first = await access.create_payment_link("mybot", db, yk, settings, user, NOW)
    again = await access.create_payment_link("mybot", db, yk, settings, user, NOW + 60)
    assert first == again and len(yk.created) == 1
    fresh = await access.create_payment_link("mybot", db, yk, settings, user, NOW + 3600)
    assert fresh[0] != first[0] and len(yk.created) == 2


async def test_receipt_is_sent_when_enabled(db):
    yk, settings = FakeYooKassa(), make_settings(send_receipt=True)
    await _user(db)
    await db.set_email(U, "parent@example.com")
    await access.create_payment_link("mybot", db, yk, settings, await db.get_user(U), NOW)
    receipt = yk.created[0]["receipt"]
    assert receipt["customer"] == {"email": "parent@example.com"}
    item = receipt["items"][0]
    assert item["amount"] == {"value": "990.00", "currency": "RUB"}
    assert (item["vat_code"], item["payment_mode"], item["payment_subject"]) == (1, "full_payment", "service")


async def test_stale_pending_payment_expires(db):
    bot, yk, settings = FakeBot(), FakeYooKassa(), make_settings()
    user = await _user(db)
    pid, _ = await access.create_payment_link("mybot", db, yk, settings, user, NOW)
    payment = await db.get_payment(pid)
    assert await access.sync_payment(bot, db, yk, settings, payment, NOW + 3 * DAY) == "expired"


# ---------- истечение доступа ----------

async def test_expired_user_is_removed_and_can_return(db):
    bot, settings = FakeBot(), make_settings()
    await _user(db)
    await db.start_trial(U, 3, NOW)
    bot.members[U] = "member"

    assert await access.expire_users(bot, db, settings, NOW + 3 * DAY - 1) == 0
    assert bot.banned == []

    assert await access.expire_users(bot, db, settings, NOW + 3 * DAY) == 1
    assert bot.banned == [U] and bot.unbanned == [U]  # исключён, но не в чёрном списке
    assert any(chat == U and "закончился" in text for chat, text in bot.sent)

    # второй проход никого не трогает и не спамит
    sent_before = len(bot.sent)
    assert await access.expire_users(bot, db, settings, NOW + 4 * DAY) == 0
    assert len(bot.sent) == sent_before

    # после оплаты пометка «убран» снимается
    await db.grant_days(U, 30, NOW + 5 * DAY)
    user = await db.get_user(U)
    assert user["removed_at"] is None and access.is_active(user, NOW + 6 * DAY)


async def test_admins_are_never_removed(db):
    bot, settings = FakeBot(), make_settings()
    for user_id, status in ((ADMIN, "member"), (50, "administrator"), (51, "creator")):
        await _user(db, user_id)
        await db.start_trial(user_id, 3, NOW)
        bot.members[user_id] = status
    await access.expire_users(bot, db, settings, NOW + 4 * DAY)
    assert bot.banned == []


async def test_reminder_is_sent_once_per_period(db):
    bot, settings = FakeBot(), make_settings()
    await _user(db)
    await db.start_trial(U, 3, NOW)

    assert await access.send_reminders(bot, db, settings, NOW + DAY) == 0  # ещё рано
    assert await access.send_reminders(bot, db, settings, NOW + 2 * DAY + 60) == 1
    assert await access.send_reminders(bot, db, settings, NOW + 2 * DAY + 120) == 0

    # после продления — новый срок и новое напоминание перед его концом
    until = await db.grant_days(U, 30, NOW + 2 * DAY + 200)
    assert await access.send_reminders(bot, db, settings, until - DAY + 60) == 1


async def test_invite_link_is_created_once(db):
    bot, settings = FakeBot(), make_settings()
    assert await access.get_invite_link(bot, db, settings) == "https://t.me/+invite"
    assert await access.get_invite_link(bot, db, settings) == "https://t.me/+invite"
    assert bot.links_created == 1


def test_days_word():
    assert [access.days_word(n) for n in (1, 3, 5, 11, 21, 30)] == [
        "1 день", "3 дня", "5 дней", "11 дней", "21 день", "30 дней",
    ]
