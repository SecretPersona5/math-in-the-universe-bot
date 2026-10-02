from __future__ import annotations

import io
import zipfile

from bot.export import build_export

NOW = 1_800_000_000


class DownloadBot:
    async def download(self, file_id, destination):
        destination.write(f"bytes-of-{file_id}".encode())


async def test_export_groups_albums_and_escapes_html(db):
    await db.upsert_user(1, "anya", "Аня <script>", NOW)
    await db.add_question(1, "photo", "f1", "Почему <b> тут минус?", None, NOW)
    await db.add_question(1, "photo", "f2", "Две задачи", "album", NOW + 10)
    await db.add_question(1, "photo", "f3", None, "album", NOW + 10)
    await db.add_question(1, "text", None, "Вопрос без фото", None, NOW + 20)
    await db.add_question(1, "photo", "old", "старый", None, NOW - 100)

    data, count, truncated = await build_export(DownloadBot(), db, NOW, "Europe/Moscow")
    assert (count, truncated) == (3, False)  # альбом — один вопрос, старый не попал

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = sorted(archive.namelist())
        page = archive.read("index.html").decode()
        assert archive.read(names[0]) == b"bytes-of-f1"
    assert len([n for n in names if n.startswith("img/")]) == 3
    assert "Почему &lt;b&gt; тут минус?" in page and "<script>" not in page
    assert "@anya" in page and "Вопрос без фото" in page
    assert page.count("<article>") == 3
