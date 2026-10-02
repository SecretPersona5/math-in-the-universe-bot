"""Настройки бота. Всё читается из переменных окружения (файл .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана обязательная переменная {name}")
    return value


def _opt(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


def _int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"Не задана обязательная переменная {name}")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} должно быть целым числом, а там «{raw}»") from None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    channel_id: int
    questions_chat_id: int | None
    admin_ids: frozenset[int]

    yk_shop_id: str
    yk_secret: str

    price: Decimal
    sub_days: int
    trial_days: int
    product_name: str

    send_receipt: bool
    vat_code: int
    tax_system_code: int | None

    offer_url: str | None
    support_contact: str | None
    questions_subscribers_only: bool

    db_path: str
    proxy_url: str | None
    tz: str


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)

    try:
        price = Decimal(_req("PRICE_RUB").replace(",", "."))
    except InvalidOperation:
        raise ConfigError("PRICE_RUB должно быть числом, например 990 или 990.00") from None
    if price <= 0:
        raise ConfigError("PRICE_RUB должно быть больше нуля")

    admin_raw = _req("ADMIN_IDS")
    try:
        admin_ids = frozenset(int(x) for x in admin_raw.replace(";", ",").split(",") if x.strip())
    except ValueError:
        raise ConfigError("ADMIN_IDS: числовые id через запятую") from None

    questions_raw = _opt("QUESTIONS_CHAT_ID")
    tax_raw = _opt("TAX_SYSTEM_CODE")

    return Settings(
        bot_token=_req("BOT_TOKEN"),
        channel_id=_int("CHANNEL_ID"),
        questions_chat_id=int(questions_raw) if questions_raw else None,
        admin_ids=admin_ids,
        yk_shop_id=_req("YOOKASSA_SHOP_ID"),
        yk_secret=_req("YOOKASSA_SECRET_KEY"),
        price=price.quantize(Decimal("0.01")),
        sub_days=_int("SUB_DAYS", 30),
        trial_days=_int("TRIAL_DAYS", 3),
        product_name=os.getenv("PRODUCT_NAME", "").strip() or "Доступ к закрытому каналу",
        send_receipt=_bool("YOOKASSA_SEND_RECEIPT", False),
        vat_code=_int("VAT_CODE", 1),
        tax_system_code=int(tax_raw) if tax_raw else None,
        offer_url=_opt("OFFER_URL"),
        support_contact=_opt("SUPPORT_CONTACT"),
        questions_subscribers_only=_bool("QUESTIONS_SUBSCRIBERS_ONLY", True),
        db_path=os.getenv("DB_PATH", "").strip() or "data/bot.sqlite3",
        proxy_url=_opt("PROXY_URL"),
        tz=os.getenv("TZ_NAME", "").strip() or "Europe/Moscow",
    )
