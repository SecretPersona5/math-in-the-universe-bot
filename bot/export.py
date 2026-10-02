"""Выгрузка всех вопросов одним архивом: картинки + страница index.html с подписями."""
from __future__ import annotations

import html
import io
import logging
import zipfile

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .access import fmt_dt
from .db import Database

log = logging.getLogger(__name__)

# Бот может отправить файл до 50 МБ — оставляем запас.
MAX_ARCHIVE_BYTES = 45 * 1024 * 1024

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вопросы учеников</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:900px;margin:0 auto;padding:16px;background:#f6f6f4;color:#1c1c1c}}
 article{{background:#fff;border-radius:12px;padding:16px;margin:16px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 .meta{{color:#666;font-size:14px;margin-bottom:8px}}
 .text{{white-space:pre-wrap;font-size:17px;margin:8px 0}}
 img{{max-width:100%;border-radius:8px;margin-top:8px;display:block}}
 .missing{{color:#a33;font-size:14px}}
</style></head><body>
<h1>Вопросы учеников</h1>
<p class="meta">{summary}</p>
{items}
</body></html>
"""


def _group(rows: list) -> list[list]:
    """Склеивает фото одного альбома в один вопрос."""
    groups: list[list] = []
    by_album: dict[str, list] = {}
    for row in rows:
        album = row["media_group_id"]
        if album and album in by_album:
            by_album[album].append(row)
            continue
        group = [row]
        groups.append(group)
        if album:
            by_album[album] = group
    return groups


async def build_export(bot: Bot, db: Database, since: int, tz: str) -> tuple[bytes, int, bool]:
    """Возвращает (zip-архив, число вопросов, обрезан ли архив по размеру)."""
    groups = _group(await db.questions_since(since))
    buffer = io.BytesIO()
    items: list[str] = []
    total = 0
    truncated = False

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for group in groups:
            first = group[0]
            who = html.escape(first["full_name"] or "без имени")
            if first["username"]:
                who += f" (@{html.escape(first['username'])})"
            parts = [f'<div class="meta">{fmt_dt(first["created_at"], tz)} · {who}</div>']
            caption = next((r["caption"] for r in group if r["caption"]), None)
            if caption:
                parts.append(f'<div class="text">{html.escape(caption)}</div>')
            for row in group:
                if not row["file_id"]:
                    continue
                if truncated:
                    parts.append('<div class="missing">Картинка не поместилась в архив</div>')
                    continue
                data = io.BytesIO()
                try:
                    await bot.download(row["file_id"], destination=data)
                except TelegramAPIError:
                    log.warning("Не удалось скачать файл вопроса %s", row["id"])
                    parts.append('<div class="missing">Не удалось скачать картинку</div>')
                    continue
                payload = data.getvalue()
                if total + len(payload) > MAX_ARCHIVE_BYTES:
                    truncated = True
                    parts.append('<div class="missing">Картинка не поместилась в архив</div>')
                    continue
                total += len(payload)
                name = f"img/{row['id']}.jpg"
                archive.writestr(name, payload)
                parts.append(f'<a href="{name}"><img src="{name}" loading="lazy" alt=""></a>')
            items.append("<article>" + "".join(parts) + "</article>")

        summary = f"Всего вопросов: {len(groups)}"
        if truncated:
            summary += ". Архив обрезан по размеру — выгрузите период покороче."
        archive.writestr("index.html", PAGE.format(summary=summary, items="\n".join(items)))

    return buffer.getvalue(), len(groups), truncated
