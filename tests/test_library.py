import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

import database
import library
import main
import mcp_server as server


@pytest.fixture
def books(tmp_path, monkeypatch):
    path = tmp_path / "books.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(library, "DB_PATH", path)
    asyncio.run(database.init_db())
    with sqlite3.connect(path) as db:
        db.executescript("""
            INSERT INTO topics(name) VALUES ('Chen');
            INSERT INTO sources(url, title, category, topic_id) VALUES ('url','Book','Upload',1);
            INSERT INTO sources(url, category) VALUES ('url2','FamilySearch');
            INSERT INTO pages(source_id,image_path,ocr_text) VALUES (1,'a','陳');
            INSERT INTO pages(source_id,image_path,paddleocr_text) VALUES (2,'b','陈');
            INSERT INTO pages(source_id,image_path,tongguocr_text) VALUES (1,'c','東');
        """)
    monkeypatch.setattr(library, "load_confusion_map", lambda: {"陳": {"東": {"count": 2}}})
    return path


def test_search_and_navigation(books):
    def ids(**filters):
        result = asyncio.run(server.search("陈", **filters))
        return [row["page_id"] for row in result["results"]]
    assert ids() == [2, 1]
    assert ids(fuzzy=True) == [2, 1, 3]
    assert ids(topic_id=1) == ids(source_id=1) == ids(category="Upload") == [1]
    assert ids(engine="PaddleOCR-VL") == [2]
    page = asyncio.run(server.get_page(3))
    assert (page["page_number"], page["page_total"], page["prev_page_id"], page["next_page_id"]) == (2, 2, 1, None)
    assert asyncio.run(server.get_source_pages(1, 2))[0]["page_id"] == 3
    client = TestClient(main.app)
    for fuzzy, expected in [(False, [1]), (True, [1, 3])]:
        response = client.get("/search", params=dict(query="陈", fuzzy=fuzzy, topic_id=1, layout="full"))
        assert [r["id"] for r in response.context["results"]] == expected


def test_read_only(books):
    async def attempt():
        async with library.reader() as db:
            await db.execute("DELETE FROM pages")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        asyncio.run(attempt())


def test_api(books, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "process_jiapu_source", lambda *args: calls.append(args))
    client = TestClient(main.app)
    assert client.post("/api/topics", json={"name": "chen"}).json() == {"id": 1, "name": "Chen"}
    topic = client.post("/api/topics", json={"name": "Other"}).json()
    assert client.post("/api/sources", json={"url": "https://example.com/familysearch.org"}).status_code == 400
    url = "https://www.familysearch.org/ark:/example"
    source = client.post("/api/sources", json={"url": url, "topic_id": 1}).json()
    path = f"/api/sources/{source['id']}"
    assert calls == [(source["id"], url, "FamilySearch")]
    assert client.get(path).json()["status"] == "Pending"
    assert client.post(path + "/retry").status_code == 409
    for selection in [topic["id"], None]:
        assert client.post(path + "/topic", json={"topic_id": selection}).json()["topic_id"] == selection
    with sqlite3.connect(books) as db:
        db.execute("UPDATE sources SET status='Failed' WHERE id=?", (source["id"],))
    assert client.post(path + "/retry").json()["status"] == "Pending"
    assert len(calls) == 2


def test_scan_privacy_and_text_limit(books, monkeypatch):
    from PIL import Image
    import base64
    import io
    import json

    scan_path = books.parent / "scan.png"
    Image.new("L", (2000, 1000)).save(scan_path)
    with sqlite3.connect(books) as db:
        db.execute("UPDATE pages SET ocr_text=?, image_path=? WHERE id=1", ("陳" * 8100, str(scan_path)))
    monkeypatch.delenv("GENEALOGY_MCP_ALLOW_IMAGES", raising=False)
    args = dict(page_id=1, include_image=True, engines=["HunyuanOCR"])
    result = asyncio.run(server.mcp.call_tool("get_page", args))
    page = json.loads(result.content[0].text)
    assert len(result.content) == 1 and "disabled" in page["note"]
    assert page["truncated"] == ["HunyuanOCR"] and len(page["texts"]["HunyuanOCR"]) == 8000
    monkeypatch.setenv("GENEALOGY_MCP_ALLOW_IMAGES", "1")
    result = asyncio.run(server.mcp.call_tool("get_page", args))
    assert [part.type for part in result.content] == ["text", "image"]
    with Image.open(io.BytesIO(base64.b64decode(result.content[1].data))) as image:
        assert (image.format, image.mode, image.size) == ("JPEG", "RGB", (1568, 784))


@pytest.mark.parametrize("url, category", [
    ("https://www.familysearch.org/ark:/61903/3:1:X", "FamilySearch"),
    ("www.familysearch.org/ark:/61903/3:1:X", "FamilySearch"),
    ("  familysearch.org/ark:/x  ", "FamilySearch"),
    ("mychinarootslibrary-com.rpa.sccl.org/book/1", "MyChinaRoots"),
    ("www.ztzupu.com/book/1", "ZtZupu"),
    ("example.com/familysearch.org", "Unknown"),
    ("ftp://www.familysearch.org/x", "Unknown"),
])
def test_categorize_url(url, category):
    assert main.categorize_url(url) == category


def test_scheme_less_link_is_stored_with_https(books, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "process_jiapu_source", lambda *args: calls.append(args))
    client = TestClient(main.app)
    source = client.post("/api/sources", json={"url": "www.ztzupu.com/book/1"}).json()
    assert source["category"] == "ZtZupu"
    client.post("/add-source", data={"url": "www.familysearch.org/ark:/x"})
    assert [args[1:] for args in calls] == [
        ("https://www.ztzupu.com/book/1", "ZtZupu"),
        ("https://www.familysearch.org/ark:/x", "FamilySearch"),
    ]
