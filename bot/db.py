"""Хранилище на SQLite.

Все времена — целые секунды Unix (UTC). Доступ пользователя описывается одним
полем users.access_until: триал и оплата просто двигают его вперёд.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import aiosqlite

DAY = 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          INTEGER PRIMARY KEY,
    username         TEXT,
    full_name        TEXT,
    email            TEXT,
    created_at       INTEGER NOT NULL,
    trial_started_at INTEGER,
    access_until     INTEGER,
    removed_at       INTEGER,   -- когда бот убрал человека из канала за неуплату
    reminded_for     INTEGER    -- для какого access_until уже отправлено напоминание
);

CREATE TABLE IF NOT EXISTS payments (
    id               TEXT PRIMARY KEY,   -- наш uuid, он же ключ идемпотентности для ЮKassa
    user_id          INTEGER NOT NULL REFERENCES users(user_id),
    yk_id            TEXT UNIQUE,
    amount           TEXT NOT NULL,
    days             INTEGER NOT NULL,
    status           TEXT NOT NULL,      -- pending | succeeded | canceled | expired | mismatch
    confirmation_url TEXT,
    created_at       INTEGER NOT NULL,
    paid_at          INTEGER
);
CREATE INDEX IF NOT EXISTS payments_status ON payments(status);

CREATE TABLE IF NOT EXISTS questions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(user_id),
    media_group_id   TEXT,
    kind             TEXT NOT NULL,      -- photo | document | text
    file_id          TEXT,
    caption          TEXT,
    created_at       INTEGER NOT NULL,
    group_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS questions_created ON questions(created_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # Одно соединение на весь процесс; замок не даёт чужим запросам
        # вклиниться в середину транзакции.
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        folder = os.path.dirname(self._path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() не вызван"
        return self._conn

    async def _one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cur = await self._c.execute(sql, params)
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self._c.execute(sql, params)
            return list(await cur.fetchall())

    async def _exec(self, sql: str, params: tuple = ()) -> int:
        async with self._lock:
            cur = await self._c.execute(sql, params)
            return cur.rowcount

    # ---------- пользователи ----------

    async def upsert_user(self, user_id: int, username: str | None, full_name: str | None, now: int) -> None:
        await self._exec(
            """INSERT INTO users(user_id, username, full_name, created_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
            (user_id, username, full_name, now),
        )

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def set_email(self, user_id: int, email: str) -> None:
        await self._exec("UPDATE users SET email=? WHERE user_id=?", (email, user_id))

    async def start_trial(self, user_id: int, days: int, now: int) -> int | None:
        """Включает пробный период. Только один раз и только тем, у кого доступа ещё не было."""
        until = now + days * DAY
        changed = await self._exec(
            """UPDATE users SET trial_started_at=?, access_until=?, removed_at=NULL
               WHERE user_id=? AND trial_started_at IS NULL AND access_until IS NULL""",
            (now, until, user_id),
        )
        return until if changed else None

    async def _extend_locked(self, user_id: int, days: int, now: int) -> int:
        cur = await self._c.execute("SELECT access_until FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        current = (row["access_until"] if row else None) or 0
        # Оплата раньше срока не сжигает оставшиеся дни: продлеваем от конца текущего доступа.
        until = max(now, current) + days * DAY
        await self._c.execute(
            "UPDATE users SET access_until=?, removed_at=NULL WHERE user_id=?", (until, user_id)
        )
        return until

    async def grant_days(self, user_id: int, days: int, now: int) -> int:
        """Ручная выдача доступа администратором."""
        async with self._lock:
            await self._c.execute("BEGIN IMMEDIATE")
            try:
                await self._c.execute(
                    "INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?,?)", (user_id, now)
                )
                until = await self._extend_locked(user_id, days, now)
                await self._c.execute("COMMIT")
                return until
            except BaseException:
                await self._c.execute("ROLLBACK")
                raise

    async def revoke(self, user_id: int, now: int) -> bool:
        return bool(
            await self._exec(
                "UPDATE users SET access_until=? WHERE user_id=? AND access_until > ?", (now, user_id, now)
            )
        )

    async def expired_users(self, now: int) -> list[aiosqlite.Row]:
        return await self._all(
            """SELECT * FROM users
               WHERE access_until IS NOT NULL AND access_until <= ? AND removed_at IS NULL""",
            (now,),
        )

    async def mark_removed(self, user_id: int, now: int) -> None:
        # Условие по access_until защищает от гонки: если человек успел оплатить,
        # пока мы его убирали, пометку не ставим.
        await self._exec(
            "UPDATE users SET removed_at=? WHERE user_id=? AND access_until <= ?", (now, user_id, now)
        )

    async def users_to_remind(self, now: int, horizon: int) -> list[aiosqlite.Row]:
        return await self._all(
            """SELECT * FROM users
               WHERE access_until > ? AND access_until <= ?
                 AND (reminded_for IS NULL OR reminded_for != access_until)""",
            (now, now + horizon),
        )

    async def mark_reminded(self, user_id: int, access_until: int) -> None:
        await self._exec("UPDATE users SET reminded_for=? WHERE user_id=?", (access_until, user_id))

    async def active_users(self, now: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM users WHERE access_until > ? ORDER BY access_until", (now,)
        )

    # ---------- платежи ----------

    async def create_payment(self, user_id: int, amount: Decimal, days: int, now: int) -> str:
        payment_id = str(uuid.uuid4())
        await self._exec(
            "INSERT INTO payments(id, user_id, amount, days, status, created_at) VALUES(?,?,?,?, 'pending', ?)",
            (payment_id, user_id, f"{amount:.2f}", days, now),
        )
        return payment_id

    async def set_payment_remote(self, payment_id: str, yk_id: str, confirmation_url: str | None) -> None:
        await self._exec(
            "UPDATE payments SET yk_id=?, confirmation_url=? WHERE id=?",
            (yk_id, confirmation_url, payment_id),
        )

    async def get_payment(self, payment_id: str) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM payments WHERE id=?", (payment_id,))

    async def reusable_payment(self, user_id: int, amount: Decimal, since: int) -> aiosqlite.Row | None:
        """Свежий неоплаченный счёт — чтобы повторное нажатие «Оплатить» не плодило платежи."""
        return await self._one(
            """SELECT * FROM payments
               WHERE user_id=? AND status='pending' AND amount=? AND created_at >= ?
                 AND confirmation_url IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, f"{amount:.2f}", since),
        )

    async def pending_payments(self) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM payments WHERE status='pending' AND yk_id IS NOT NULL ORDER BY created_at"
        )

    async def close_payment(self, payment_id: str, status: str) -> bool:
        """Закрывает pending-платёж без зачисления."""
        assert status in {"canceled", "expired", "mismatch"}
        return bool(
            await self._exec(
                "UPDATE payments SET status=? WHERE id=? AND status='pending'", (status, payment_id)
            )
        )

    async def credit_payment(self, payment_id: str, now: int) -> int | None:
        """Зачисляет оплату ровно один раз.

        Возвращает новый access_until или None, если платёж уже был зачислен
        (кнопка «Я оплатил» и фоновая проверка могут прийти одновременно).
        """
        async with self._lock:
            await self._c.execute("BEGIN IMMEDIATE")
            try:
                cur = await self._c.execute(
                    "UPDATE payments SET status='succeeded', paid_at=? WHERE id=? AND status='pending'",
                    (now, payment_id),
                )
                if cur.rowcount == 0:
                    await self._c.execute("ROLLBACK")
                    return None
                cur = await self._c.execute("SELECT user_id, days FROM payments WHERE id=?", (payment_id,))
                pay = await cur.fetchone()
                until = await self._extend_locked(pay["user_id"], pay["days"], now)
                await self._c.execute("COMMIT")
                return until
            except BaseException:
                await self._c.execute("ROLLBACK")
                raise

    # ---------- вопросы учеников ----------

    async def add_question(
        self,
        user_id: int,
        kind: str,
        file_id: str | None,
        caption: str | None,
        media_group_id: str | None,
        now: int,
    ) -> int:
        async with self._lock:
            cur = await self._c.execute(
                """INSERT INTO questions(user_id, kind, file_id, caption, media_group_id, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (user_id, kind, file_id, caption, media_group_id, now),
            )
            return cur.lastrowid

    async def set_question_group_message(self, question_id: int, message_id: int) -> None:
        await self._exec("UPDATE questions SET group_message_id=? WHERE id=?", (message_id, question_id))

    async def last_question(self, user_id: int) -> aiosqlite.Row | None:
        return await self._one(
            "SELECT * FROM questions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        )

    async def set_question_caption(self, question_id: int, caption: str) -> None:
        await self._exec("UPDATE questions SET caption=? WHERE id=?", (caption, question_id))

    async def questions_since(self, since: int) -> list[aiosqlite.Row]:
        return await self._all(
            """SELECT q.*, u.username, u.full_name FROM questions q
               JOIN users u ON u.user_id = q.user_id
               WHERE q.created_at >= ? ORDER BY q.id""",
            (since,),
        )

    # ---------- прочее ----------

    async def kv_get(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def stats(self, now: int) -> dict[str, object]:
        row = await self._one(
            """SELECT
                 (SELECT COUNT(*) FROM users) AS users,
                 (SELECT COUNT(*) FROM users WHERE access_until > ?) AS active,
                 (SELECT COUNT(*) FROM users u WHERE access_until > ?
                    AND EXISTS(SELECT 1 FROM payments p WHERE p.user_id=u.user_id AND p.status='succeeded')) AS paying,
                 (SELECT COUNT(*) FROM payments WHERE status='succeeded') AS payments,
                 (SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM payments WHERE status='succeeded') AS revenue,
                 (SELECT COUNT(*) FROM questions WHERE created_at >= ?) AS questions_week
            """,
            (now, now, now - 7 * DAY),
        )
        return dict(row)
