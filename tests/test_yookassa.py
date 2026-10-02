"""Клиент ЮKassa проверяется на локальном HTTP-сервере, который ведёт себя как их API."""
from __future__ import annotations

import base64
from decimal import Decimal

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.yookassa import YooKassa, YooKassaError


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail_first = 0
        self.by_key: dict[str, dict] = {}

    async def create(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append({"headers": dict(request.headers), "json": body})
        if self.fail_first > 0:
            self.fail_first -= 1
            return web.json_response({"type": "error", "code": "internal_server_error"}, status=500)
        if body["amount"]["value"] == "0.00":
            return web.json_response(
                {"type": "error", "code": "invalid_request", "description": "Amount is too small"}, status=400
            )
        key = request.headers["Idempotence-Key"]
        payment = self.by_key.setdefault(
            key,
            {
                "id": f"yk-{len(self.by_key) + 1}",
                "status": "pending",
                "paid": False,
                "amount": body["amount"],
                "confirmation": {"type": "redirect", "confirmation_url": "https://yoomoney.ru/pay/1"},
                "metadata": body.get("metadata", {}),
            },
        )
        return web.json_response(payment)

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "id": request.match_info["id"],
                "status": "canceled",
                "paid": False,
                "amount": {"value": "990.00", "currency": "RUB"},
                "cancellation_details": {"party": "yoo_money", "reason": "expired_on_confirmation"},
            }
        )


@pytest_asyncio.fixture
async def api():
    fake = FakeApi()
    app = web.Application()
    app.router.add_post("/v3/payments", fake.create)
    app.router.add_get("/v3/payments/{id}", fake.get)
    server = TestServer(app)
    await server.start_server()
    client = YooKassa("shop123", "secret456", base_url=str(server.make_url("/v3")), retries=3)
    yield fake, client
    await client.close()
    await server.close()


async def _create(client: YooKassa, amount="990.00", key="key-1"):
    return await client.create_payment(
        idempotence_key=key,
        amount=Decimal(amount),
        description="Доступ к каналу",
        return_url="https://t.me/mybot",
        metadata={"user_id": "42"},
    )


async def test_create_payment_request_shape(api):
    fake, client = api
    payment = await _create(client)

    sent = fake.requests[0]
    assert sent["headers"]["Idempotence-Key"] == "key-1"
    expected_auth = "Basic " + base64.b64encode(b"shop123:secret456").decode()
    assert sent["headers"]["Authorization"] == expected_auth
    assert sent["json"] == {
        "amount": {"value": "990.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": "https://t.me/mybot"},
        "description": "Доступ к каналу",
        "metadata": {"user_id": "42"},
    }
    assert payment.id == "yk-1" and payment.status == "pending" and not payment.paid
    assert payment.amount == Decimal("990.00")
    assert payment.confirmation_url == "https://yoomoney.ru/pay/1"


async def test_server_error_is_retried_with_same_key(api):
    fake, client = api
    fake.fail_first = 2
    payment = await _create(client)
    assert payment.id == "yk-1"
    assert [r["headers"]["Idempotence-Key"] for r in fake.requests] == ["key-1"] * 3
    assert len(fake.by_key) == 1  # повтор не создал второй платёж


async def test_client_error_is_not_retried(api):
    fake, client = api
    with pytest.raises(YooKassaError) as info:
        await _create(client, amount="0.00")
    assert info.value.status == 400 and info.value.code == "invalid_request"
    assert len(fake.requests) == 1


async def test_gives_up_after_retries(api):
    fake, client = api
    fake.fail_first = 10
    with pytest.raises(YooKassaError):
        await _create(client)
    assert len(fake.requests) == 3


async def test_get_payment_parses_cancellation(api):
    _, client = api
    payment = await client.get_payment("yk-9")
    assert payment.status == "canceled" and payment.cancellation_reason == "expired_on_confirmation"
    assert payment.confirmation_url is None and payment.metadata == {}


async def test_unreachable_host_raises_yookassa_error():
    client = YooKassa("s", "k", base_url="http://127.0.0.1:9", retries=1, timeout=2)
    try:
        with pytest.raises(YooKassaError):
            await client.get_payment("x")
    finally:
        await client.close()
