"""Genealogy library tools for stdio MCP clients."""
import asyncio
import io
import json
import logging
import os
import re
from urllib.parse import urlencode

import requests
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PillowImage

from contextlib import asynccontextmanager
import library

APP_URL = os.getenv("GENEALOGY_APP_URL", "http://127.0.0.1:8000").rstrip("/")
mcp = MCPServer("genealogy-tools", instructions=(
    "Search Chinese genealogy books (族譜) transcribed into Traditional Chinese. "
    "Search matches Simplified and Traditional forms. fuzzy=true includes OCR confusions. "
    "Try names recorded as 名/字/號/諱 and generation characters (字輩) shared by relatives. "
    "Compare the three OCR readings and consult the scan when it matters. "
    "Always cite page_id and give the viewer_url for findings."
))


@asynccontextmanager
async def reader():
    try:
        async with library.reader() as db:
            yield db
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc


def viewer(source_id, page_id=None, query=None):
    params = {}
    if page_id is not None:
        params["focus_image_id"] = page_id
    if query:
        params["q"] = query
    return f"{APP_URL}/viewer/{source_id}" + ("?" + urlencode(params) if params else "")


@mcp.tool()
async def list_topics():
    """List topics with the number of sources filed under each."""
    async with reader() as db:
        return await library.list_topics(db)


@mcp.tool()
async def list_sources(topic_id: int | None = None, status: str = "", category: str = "",
                       text: str = "", limit: int = 50, offset: int = 0):
    """Browse books by topic, title/URL, category, or status (active, Completed, Failed)."""
    async with reader() as db:
        rows = await library.list_sources(db, topic_id, status, category, text,
                                          limit=min(max(limit, 0), 100), offset=max(offset, 0))
    return [dict(row, viewer_url=viewer(row["id"])) for row in rows]


@mcp.tool()
async def get_source(source_id: int):
    """Get one book's metadata, processing status, counts, and failure details."""
    async with reader() as db:
        row = await library.get_source(db, source_id)
    if row is None:
        raise ToolError(f"Unknown source_id {source_id}")
    return dict(row, viewer_url=viewer(source_id))


@mcp.tool()
def search_variants(query: str, fuzzy: bool = False):
    """Show all literal spellings searched, including optional OCR confusions."""
    if not query.strip():
        raise ToolError("Enter a nonempty search query.")
    return library.search_variants(query, fuzzy)


@mcp.tool()
async def search(query: str, fuzzy: bool = False, topic_id: int | None = None,
                 source_id: int | None = None, category: str = "", engine: str = "",
                 limit: int = 20, offset: int = 0):
    """Find ranked OCR hits with highlighted snippets and links to the original pages."""
    if not query.strip():
        raise ToolError("Enter a nonempty search query.")
    async with reader() as db:
        rows, variants = await library.search_pages(db, query, fuzzy, topic_id, category, engine, source_id)
        offset = max(0, offset)
        window = rows[offset:offset + min(100, max(0, limit))]
        positions = await library.page_positions(db, [row["source_id"] for row in window])
    pattern = re.compile("|".join(map(re.escape, sorted(variants, key=len, reverse=True))))
    results = []
    for row in window:
        matches = row["matches"]
        best = next((m for m in matches if query in m["text"]), matches[0])
        parts = library.highlight_segments(best["text"], pattern, snippet=True)
        number, total = positions[row["id"]]
        results.append(dict(page_id=row["id"], source_id=row["source_id"], source_title=row["source_title"],
                            topic_name=row["topic_name"], page_number=number, page_total=total,
                            engines_matched=[m["engine"] for m in matches],
                            snippet="".join(f"【{s}】" if hit else s for s, hit in parts),
                            viewer_url=viewer(row["source_id"], row["id"], query) + ("&fuzzy=true" if fuzzy else "")))
    return dict(query=query, variant_count=len(variants), total_matches=len(rows),
                source_count=len({r["source_id"] for r in rows}), results=results)


def format_page(page, engines):
    texts = page["texts"]
    labels = list(texts) if engines is None else engines
    for label in labels:
        if label not in texts:
            raise ToolError(f"Unknown OCR engine {label}")
    page["truncated"] = [label for label in labels if len(texts[label] or "") > 8000]
    page["texts"] = {label: texts[label][:8000] if texts[label] else texts[label] for label in labels}
    page["viewer_url"] = viewer(page["source_id"], page["id"])
    page["page_id"] = page.pop("id")
    return page


def scan(path):
    with PillowImage.open(path) as image:
        image.thumbnail((1568, 1568))
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg").to_image_content()


@mcp.tool()
async def get_page(page_id: int, engines: list[str] | None = None, include_image: bool = False):
    """Read selected OCR readings and neighboring page IDs. Optionally include the scan."""
    async with reader() as db:
        page = await library.get_page(db, page_id)
    if page is None:
        raise ToolError(f"Unknown page_id {page_id}")
    path = page.pop("image_path")
    format_page(page, engines)
    if include_image:
        if os.getenv("GENEALOGY_MCP_ALLOW_IMAGES") != "1":
            page["note"] = "Images are disabled. Set GENEALOGY_MCP_ALLOW_IMAGES=1 to enable them."
        elif image_path := library.resolve_image_path(path):
            return [json.dumps(page, ensure_ascii=False), await asyncio.to_thread(scan, image_path)]
        else:
            page["note"] = "Scan file is missing."
    return page


@mcp.tool()
async def get_source_pages(source_id: int, start_page: int = 1, count: int = 10, engine: str = "HunyuanOCR"):
    """Read up to 25 consecutive pages from one OCR engine, starting at page 1 or later."""
    async with reader() as db:
        if not await library.get_source(db, source_id):
            raise ToolError(f"Unknown source_id {source_id}")
        pages = await library.get_source_pages(db, source_id, max(1, start_page), min(25, max(0, count)))
    for page in pages:
        page.pop("image_path")
        format_page(page, [engine])
    return pages


async def api(method, path, body=None):
    try:
        response = await asyncio.to_thread(requests.request, method, APP_URL + path, json=body, timeout=30)
    except requests.ConnectionError as exc:
        raise ToolError(f"The Genealogy Tools web app isn't running at {APP_URL}") from exc
    if not response.ok:
        raise ToolError(f"Web app returned {response.status_code}: {response.text[:500]}")
    return response.json()


@mcp.tool()
async def add_source(url: str, topic_id: int | None = None):
    """Queue a book and return immediately. Scraping and OCR take minutes (about 4 extra
    minutes for cold OCR engines). Check back with source_status."""
    return await api("POST", "/api/sources", dict(url=url, topic_id=topic_id))


@mcp.tool()
async def source_status(source_id: int):
    """Check download and OCR progress from the running web app."""
    return await api("GET", f"/api/sources/{source_id}")


@mcp.tool()
async def retry_source(source_id: int):
    """Queue a failed source for processing again, replacing its partial pages."""
    return await api("POST", f"/api/sources/{source_id}/retry")


@mcp.tool()
async def create_topic(name: str):
    """Create a topic or retrieve the existing case-insensitive name match."""
    return await api("POST", "/api/topics", dict(name=name))


@mcp.tool()
async def set_source_topic(source_id: int, topic_id: int | None = None):
    """File a source under a topic, or omit topic_id to unassign it."""
    return await api("POST", f"/api/sources/{source_id}/topic", dict(topic_id=topic_id))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    mcp.run()
