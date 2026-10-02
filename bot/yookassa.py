"""Минимальный клиент API ЮKassa (v3): создать платёж, узнать статус.

Документация: https://yookassa.ru/developers/payment-acceptance/getting-started/quick-start
Платёж создаётся со сценарием redirect: ученик платит на странице ЮKassa в браузере,
деньги идут напрямую на счёт ИП. Через Telegram деньги не проходят.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

BASE_URL = "https://api.yookassa.ru/v3"


class YooKassaError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class YkPayment:
    id: str
    status: str  # pending | waiting_for_capture | succeeded | canceled
    paid: bool
    amount: Decimal
    currency: str
    confirmation_url: str | None
    metadata: dict[str, str] = field(default_factory=dict)
    cancellation_reason: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "YkPayment":
        return cls(
            id=data["id"],
            status=data["status"],
            paid=bool(data.get("paid")),
            amount=Decimal(data["amount"]["value"]),
            currency=data["amount"]["currency"],
            confirmation_url=(data.get("confirmation") or {}).get("confirmation_url"),
            metadata=data.get("metadata") or {},
            cancellation_reason=(data.get("cancellation_details") or {}).get("reason"),
        )


def build_receipt(
    *,
    email: str,
    description: str,
    amount: Decimal,
    vat_code: int,
    tax_system_code: int | None = None,
) -> dict[str, Any]:
    """Данные для чека по 54-ФЗ («Чеки от ЮKassa»): одна услуга, полный расчёт."""
    receipt: dict[str, Any] = {
        "customer": {"email": email},
        "items": [
            {
                "description": description[:128],
                "quantity": 1,
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "vat_code": vat_code,
                "payment_mode": "full_payment",
                "payment_subject": "service",
            }
        ],
    }
    if tax_system_code is not None:
        receipt["tax_system_code"] = tax_system_code
    return receipt


class YooKassa:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        retries: int = 3,
    ) -> None:
        token = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = retries
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotence_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": self._auth_header}
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key
        session = await self._get_session()
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                async with session.request(
                    method, f"{self._base}{path}", json=json, headers=headers
                ) as resp:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        body = {}
                    if resp.status < 300:
                        return body
                    message = body.get("description") or f"HTTP {resp.status}"
                    error = YooKassaError(message, status=resp.status, code=body.get("code"))
                    # 5xx и 429 можно повторить: с тем же ключом идемпотентности
                    # ЮKassa не создаст второй платёж.
                    if resp.status >= 500 or resp.status == 429:
                        last_error = error
                    else:
                        raise error
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
            if attempt < self._retries:
                await asyncio.sleep(0.5 * attempt)
        if isinstance(last_error, YooKassaError):
            raise last_error
        raise YooKassaError(f"ЮKassa недоступна: {last_error!r}") from last_error

    async def create_payment(
        self,
        *,
        idempotence_key: str,
        amount: Decimal,
        description: str,
        return_url: str,
        metadata: dict[str, str],
        receipt: dict[str, Any] | None = None,
    ) -> YkPayment:
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": metadata,
        }
        if receipt is not None:
            payload["receipt"] = receipt
        data = await self._request("POST", "/payments", json=payload, idempotence_key=idempotence_key)
        return YkPayment.from_json(data)

    async def get_payment(self, yk_id: str) -> YkPayment:
        return YkPayment.from_json(await self._request("GET", f"/payments/{yk_id}"))

    async def me(self) -> dict[str, Any]:
        """Настройки магазина — удобно для проверки ключей."""
        return await self._request("GET", "/me")
