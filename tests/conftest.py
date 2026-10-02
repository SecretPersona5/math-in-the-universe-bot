from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio

from bot.config import Settings
from bot.db import Database
from bot.yookassa import YkPayment

CHANNEL = -1001
ADMIN = 999


def make_settings(**overrides) -> Settings:
    base = dict(
        bot_token="t",
        channel_id=CHANNEL,
        questions_chat_id=-2002,
        admin_ids=frozenset({ADMIN}),
        yk_shop_id="shop",
        yk_secret="secret",
        price=Decimal("990.00"),
        sub_days=30,
        trial_days=3,
        product_name="Доступ к закрытому каналу",
        send_receipt=False,
        vat_code=1,
        tax_system_code=None,
        offer_url=None,
        support_contact=None,
        questions_subscribers_only=True,
        db_path=":memory:",
        proxy_url=None,
        tz="Europe/Moscow",
    )
    base.update(overrides)
    return Settings(**base)


class FakeBot:
    """Записывает вызовы вместо обращения к Telegram."""

    def __init__(self) -> None:
        self.members: dict[int, str] = {}
        self.sent: list[tuple[int, str]] = []
        self.banned: list[int] = []
        self.unbanned: list[int] = []
        self.links_created = 0

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=self.members.get(user_id, "left"))

    async def ban_chat_member(self, chat_id, user_id):
        self.banned.append(user_id)
        self.members[user_id] = "kicked"

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=None):
        self.unbanned.append(user_id)
        self.members[user_id] = "left"

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    async def create_chat_invite_link(self, chat_id, **kwargs):
        self.links_created += 1
        assert kwargs.get("creates_join_request") is True
        return SimpleNamespace(invite_link="https://t.me/+invite")


class FakeYooKassa:
    def __init__(self) -> None:
        self.remote: dict[str, YkPayment] = {}
        self.created: list[dict] = []

    async def create_payment(self, **kwargs):
        self.created.append(kwargs)
        yk_id = f"yk-{len(self.created)}"
        payment = YkPayment(
            id=yk_id,
            status="pending",
            paid=False,
            amount=kwargs["amount"],
            currency="RUB",
            confirmation_url=f"https://yoomoney.ru/pay/{yk_id}",
            metadata=kwargs["metadata"],
        )
        self.remote[yk_id] = payment
        return payment

    async def get_payment(self, yk_id):
        return self.remote[yk_id]

    def set_status(self, yk_id, status, *, paid=None, amount=None, metadata=None):
        old = self.remote[yk_id]
        self.remote[yk_id] = YkPayment(
            id=yk_id,
            status=status,
            paid=(status == "succeeded") if paid is None else paid,
            amount=old.amount if amount is None else amount,
            currency="RUB",
            confirmation_url=old.confirmation_url,
            metadata=old.metadata if metadata is None else metadata,
        )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.sqlite3"))
    await database.connect()
    yield database
    await database.close()
