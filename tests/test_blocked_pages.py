import asyncio

import pytest
from PIL import Image

import tasks
from Scraping import ztzupu

# What ZtZupu sends for a blocked page: an HTML 404 page, served as image/jpeg.
BLOCKED_PAGE = b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN">\r\n<html>404</html>'


def write_image(path):
    Image.new("RGB", (4, 4)).save(path)
    return str(path)


def test_is_image_tells_blocked_pages_apart(tmp_path):
    real = write_image(tmp_path / "0.jpg")
    with open(real, "rb") as f:
        assert ztzupu.is_image(f.read())
    assert not ztzupu.is_image(BLOCKED_PAGE)

    blocked = tmp_path / "1.jpg"
    blocked.write_bytes(BLOCKED_PAGE)
    assert tasks.is_image_file(real)
    assert not tasks.is_image_file(str(blocked))


def test_image_manager_skips_blocked_pages(monkeypatch):
    async def fake_get_image(session, slots, url, num, save_dir, on_saved=None):
        return url != "blocked"

    monkeypatch.setattr(ztzupu, "getImage", fake_get_image)
    totals = []
    blocked = asyncio.run(ztzupu.imageManager(["a", "blocked", "b", "blocked"], "dir", on_total=totals.append))
    assert blocked == 2
    assert totals == [3, 2]

    with pytest.raises(ztzupu.DownloadError, match="All 2 pages are blocked"):
        asyncio.run(ztzupu.imageManager(["blocked", "blocked"], "dir"))


def test_ocr_skips_blocked_pages(tmp_path, monkeypatch):
    good = [write_image(tmp_path / f"{n}.jpg") for n in (0, 2)]
    blocked = tmp_path / "1.jpg"
    blocked.write_bytes(BLOCKED_PAGE)

    read, saved = [], []

    async def fake_ocr_page(path):
        read.append(path)
        return ("text", None, None, None)

    async def fake_save_page(source_id, path, results):
        saved.append(path)

    monkeypatch.setattr(tasks, "ocr_page", fake_ocr_page)
    monkeypatch.setattr(tasks, "save_page", fake_save_page)
    tasks.start_progress(99)
    try:
        asyncio.run(tasks.ocr_pages(99, tasks.image_files(str(tmp_path))))
        assert saved == read == good
        assert tasks.progress[99]["ocr_total"] == tasks.progress[99]["ocr_done"] == 2
    finally:
        tasks.progress.pop(99, None)
