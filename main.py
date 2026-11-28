import sys

# The OCR clients print page text (Chinese) as they go. When the server's
# console isn't UTF-8 (e.g. output redirected on Windows, which falls back to
# cp1252), those prints raise UnicodeEncodeError and fail the whole upload.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import library
from library import OCR_ENGINES, chunked, highlight_segments, list_topics as fetch_topics, search_variants
from pydantic import BaseModel
import os
import json
import shutil
import uuid
import asyncio
from urllib.parse import urlsplit
from pathlib import Path
from typing import Literal
from PIL import Image, ImageOps
from database import init_db, get_db, clear_db, fail_interrupted_sources
import ocr_engines
from tasks import ocr_page, process_jiapu_source, process_uploaded_files, progress
import re
from contextlib import asynccontextmanager

UPLOAD_DIR = "uploads"
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".pdf"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB on startup
    await init_db()
    interrupted = await fail_interrupted_sources()
    if interrupted:
        print(f"Marked {interrupted} interrupted source(s) as Failed; retry them from the source list.")
    # Stop OCR engines that sit idle, so their GPU memory is free between jobs.
    idle_monitor = ocr_engines.start_idle_monitor()
    yield
    if idle_monitor:
        idle_monitor.set()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# Mount static file directories for serving images
if os.path.exists("Scraping"):
    app.mount("/images", StaticFiles(directory="Scraping"), name="scraping_images")
if os.path.exists("CamoufoxScraping"):
    app.mount("/camoufox", StaticFiles(directory="CamoufoxScraping"), name="camoufox_images")
if os.path.exists("HunyuanOCR"):
    app.mount("/hocr", StaticFiles(directory="HunyuanOCR"), name="hocr_images")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploaded_files")

def normalize_source_url(url: str) -> str:
    """Links copied from an address bar often lack the scheme
    ("www.familysearch.org/ark:/..."). Assume https so the link is still
    recognized and the scraper gets a URL it can open."""
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def categorize_url(url: str) -> str:
    url = normalize_source_url(url)
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "Unknown"
    if parts.scheme in ("http", "https"):
        for domain, category in (
            ("familysearch.org", "FamilySearch"), ("mychinaroots.com", "MyChinaRoots"),
            ("mychinarootslibrary.com", "MyChinaRoots"),
            ("mychinarootslibrary-com.rpa.sccl.org", "MyChinaRoots"), ("ztzupu.com", "ZtZupu"),
        ):
            if host == domain or host.endswith("." + domain):
                return category
    return "Unknown"


def get_image_url(image_path: str) -> str:
    if not image_path:
        return ""

    normalized = image_path.replace('\\', '/')
    if normalized.startswith(("http://", "https://", "/")):
        return normalized

    abs_path = os.path.abspath(image_path).replace('\\', '/')
    roots = {
        os.path.abspath("Scraping").replace('\\', '/'): "/images",
        os.path.abspath("CamoufoxScraping").replace('\\', '/'): "/camoufox",
        os.path.abspath("HunyuanOCR").replace('\\', '/'): "/hocr",
        os.path.abspath(UPLOAD_DIR).replace('\\', '/'): "/uploads",
    }

    for root, prefix in roots.items():
        if abs_path == root or abs_path.startswith(root + "/"):
            rel_path = os.path.relpath(abs_path, root).replace('\\', '/')
            return f"{prefix}/{rel_path}"

    return normalized

# Every OCR engine's page text, as (label shown in results, pages column).
# Search matches a page if a query appears in any of them.
def parse_topic_id(topic_id: str | None) -> int | None:
    """Form selects submit "" for the All Topics option; treat that as unassigned."""
    if topic_id is None or not str(topic_id).strip():
        return None
    try:
        return int(topic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid topic.")


async def assert_topic_exists(db, topic_id: int | None):
    if topic_id is None:
        return
    cursor = await db.execute("SELECT id FROM topics WHERE id = ?", (topic_id,))
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Topic not found")


def progress_view(source: dict) -> dict | None:
    """Short progress figures for a source that is being worked on, or None.

    Returns the labels shown under the source ("34/120" downloaded, "12/120"
    OCR'd) and the bar widths as percentages. A scrape whose site doesn't say
    how many pages the book has shows a bare download count until it ends.
    """
    live = progress.get(source["id"])
    if not live:
        return None
    downloaded, download_total = live["downloaded"], live["download_total"]
    ocr_done, ocr_total = live["ocr_done"], live["ocr_total"]
    is_upload = source["category"] == "Upload"

    # The best guess at the book's page count, used as the bar's full width.
    total = ocr_total if is_upload else (download_total or max(ocr_total, downloaded))

    def pct(n):
        return min(100, round(100 * n / total)) if total else 0

    return {
        "download": None if is_upload else (
            f"{downloaded}/{download_total}" if download_total else f"{downloaded}"
        ),
        "ocr": f"{ocr_done}/{total}" if total else f"{ocr_done}",
        "download_pct": 0 if is_upload else pct(downloaded),
        "ocr_pct": pct(ocr_done),
    }


async def fetch_sources(db, topic_id=None, status="", category="", text="", sort="newest"):
    sources = await library.list_sources(db, topic_id, status, category, text, sort)

    # The first page's image, for a thumbnail on the sources page.
    first_ids = [source["first_page_id"] for source in sources if source["first_page_id"]]
    first_images = {}
    for batch in chunked(first_ids, 900):
        cursor = await db.execute(
            f"SELECT id, image_path FROM pages WHERE id IN ({','.join('?' * len(batch))})", batch
        )
        first_images.update({row["id"]: row["image_path"] for row in await cursor.fetchall()})

    for source in sources:
        source["progress"] = progress_view(source)
        source["thumbnail_url"] = get_image_url(first_images.get(source["first_page_id"], ""))
    return sources


async def fetch_categories(db) -> list[str]:
    cursor = await db.execute("SELECT DISTINCT category FROM sources ORDER BY category")
    return [row["category"] for row in await cursor.fetchall()]


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    db = await get_db()
    try:
        topics = await fetch_topics(db)
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, "active_page": "home", "topics": topics, "selected_topic_id": None},
    )


class TopicInput(BaseModel):
    name: str


class TopicAssignment(BaseModel):
    topic_id: int | None


class SourceInput(BaseModel):
    url: str
    topic_id: int | None = None


async def save_topic(db, name):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Topic name cannot be empty.")
    await db.execute("INSERT INTO topics(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    rows = await db.execute_fetchall("SELECT id, name FROM topics WHERE name=? COLLATE NOCASE", (name,))
    await db.commit()
    return dict(rows[0])


@app.post("/api/topics")
async def api_create_topic(body: TopicInput):
    db = await get_db()
    try:
        return await save_topic(db, body.name)
    finally:
        await db.close()


@app.post("/topics", response_class=HTMLResponse)
async def create_topic(request: Request, name: str = Form(...)):
    db = await get_db()
    try:
        topic = await save_topic(db, name)
        topic_id = topic["id"]
        topics = await fetch_topics(db)
    finally:
        await db.close()

    # Swap the picker back in with the new topic already selected, so the next
    # source the user adds lands in the topic they just created.
    return templates.TemplateResponse(
        request=request,
        name="partials/topic_picker.html",
        context={"request": request, "topics": topics, "selected_topic_id": topic_id},
    )


@app.get("/topics", response_class=HTMLResponse)
async def get_topics(request: Request, topic_id: str | None = None):
    selected_topic_id = parse_topic_id(topic_id)
    db = await get_db()
    try:
        topics = await fetch_topics(db)
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="partials/topic_picker.html",
        context={"request": request, "topics": topics, "selected_topic_id": selected_topic_id},
    )


@app.post("/topics/{topic_id}/delete", response_class=HTMLResponse)
async def delete_topic(request: Request, topic_id: int):
    """Remove a topic but keep its sources — they simply become unassigned."""
    db = await get_db()
    try:
        await assert_topic_exists(db, topic_id)
        await db.execute("UPDATE sources SET topic_id = NULL WHERE topic_id = ?", (topic_id,))
        await db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        await db.commit()
        topics = await fetch_topics(db)
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="partials/topic_picker.html",
        context={"request": request, "topics": topics, "selected_topic_id": None},
    )


@app.post("/sources/{source_id}/topic", response_class=HTMLResponse)
async def set_source_topic(
    request: Request,
    source_id: int,
    topic_id: str = Form(None),
    filter_topic_id: str | None = None,
):
    """Move an existing source into (or out of) a topic.

    filter_topic_id is the topic the list is currently filtered by, so the
    re-rendered list keeps that filter instead of falling back to all topics.
    """
    await assign_topic(source_id, parse_topic_id(topic_id))
    return await get_sources(request, topic_id=filter_topic_id)


@app.post("/api/sources/{source_id}/topic")
async def api_assign_topic(source_id: int, body: TopicAssignment):
    return await assign_topic(source_id, body.topic_id)


async def assign_topic(source_id, new_topic_id):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM sources WHERE id = ?", (source_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Source not found")

        await assert_topic_exists(db, new_topic_id)
        await db.execute("UPDATE sources SET topic_id = ? WHERE id = ?", (new_topic_id, source_id))
        await db.commit()
    finally:
        await db.close()

    return {"id": source_id, "topic_id": new_topic_id}


@app.post("/sources/{source_id}/delete", response_class=HTMLResponse)
async def delete_source(
    request: Request,
    source_id: int,
    filter_topic_id: str | None = None,
):
    """Delete one source along with its OCR'd pages.

    Only upload files are removed from disk: they live in uploads/source_<id>/,
    which this app created and owns. Scraped images sit in directories named
    after the book, which a second source for the same book would share, so
    those are left alone rather than risking someone else's data.
    """
    db = await get_db()
    try:
        cursor = await db.execute("SELECT category FROM sources WHERE id = ?", (source_id,))
        source = await cursor.fetchone()
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")

        await db.execute("DELETE FROM pages WHERE source_id = ?", (source_id,))
        await db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        await db.commit()
    finally:
        await db.close()

    if source["category"] == "Upload":
        shutil.rmtree(os.path.join(UPLOAD_DIR, f"source_{source_id}"), ignore_errors=True)
    shutil.rmtree(Path(UPLOAD_DIR, "rotations", str(source_id)), ignore_errors=True)

    return await get_sources(request, topic_id=filter_topic_id)


@app.post("/sources/{source_id}/retry", response_class=HTMLResponse)
async def retry_source(
    request: Request,
    background_tasks: BackgroundTasks,
    source_id: int,
    filter_topic_id: str | None = None,
):
    """Re-run a failed source from scratch.

    Any pages produced by the partial run are cleared first so a successful
    retry doesn't leave duplicate OCR text behind. Uploads are re-processed from
    the original files still sitting in uploads/source_<id>/; scrapes just re-run
    against the stored URL.
    """
    await queue_retry(source_id, background_tasks)
    return await get_sources(request, topic_id=filter_topic_id)


@app.post("/api/sources/{source_id}/retry")
async def queue_retry(source_id: int, background_tasks: BackgroundTasks):
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT url, category, status FROM sources WHERE id = ?", (source_id,)
        )
        source = await cursor.fetchone()
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")
        if source["status"] != "Failed":
            raise HTTPException(status_code=409, detail="Only failed sources can be retried.")

        if source["category"] == "Upload":
            source_dir = os.path.join(UPLOAD_DIR, f"source_{source_id}")
            saved_paths = [
                os.path.join(source_dir, name)
                for name in sorted(os.listdir(source_dir))
                if os.path.isfile(os.path.join(source_dir, name))
            ] if os.path.isdir(source_dir) else []
            if not saved_paths:
                raise HTTPException(
                    status_code=410,
                    detail="The uploaded files for this source are no longer available.",
                )

        await db.execute("DELETE FROM pages WHERE source_id = ?", (source_id,))
        await db.execute(
            "UPDATE sources SET status = 'Pending', error = NULL WHERE id = ?", (source_id,)
        )
        await db.commit()
    finally:
        await db.close()

    if source["category"] == "Upload":
        background_tasks.add_task(process_uploaded_files, source_id, saved_paths)
    else:
        background_tasks.add_task(
            process_jiapu_source, source_id, source["url"], source["category"]
        )

    return {"id": source_id, "status": "Pending"}


@app.get("/api/sources/{source_id}")
async def api_source_status(source_id: int):
    db = await get_db()
    try:
        source = await library.get_source(db, source_id)
        if source is None:
            raise HTTPException(404, "Source not found")
        return dict(source, progress=progress.get(source_id))
    finally:
        await db.close()


@app.post("/api/sources")
async def api_add_source(body: SourceInput, background_tasks: BackgroundTasks):
    url = normalize_source_url(body.url)
    category = categorize_url(url)
    if category == "Unknown":
        raise HTTPException(400, "Unsupported source website.")
    return await queue_source(url, category, body.topic_id, background_tasks)


@app.post("/add-source", response_class=HTMLResponse)
async def add_source(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    topic_id: str = Form(None),
):
    url = normalize_source_url(url)
    await queue_source(url, categorize_url(url), parse_topic_id(topic_id), background_tasks)
    return await get_sources(request, topic_id=topic_id)


async def queue_source(url, category, selected_topic_id, background_tasks):
    db = await get_db()
    try:
        await assert_topic_exists(db, selected_topic_id)
        cursor = await db.execute(
            "INSERT INTO sources (url, category, status, topic_id) VALUES (?, ?, ?, ?)",
            (url, category, "Pending", selected_topic_id)
        )
        await db.commit()
        source_id = cursor.lastrowid

        # Dispatch background task
        background_tasks.add_task(process_jiapu_source, source_id, url, category)

    finally:
        await db.close()

    return {"id": source_id, "category": category, "status": "Pending"}


@app.post("/upload-ocr", response_class=HTMLResponse)
async def upload_for_ocr(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    topic_id: str = Form(None),
):
    """Store uploaded photos/PDFs and queue them for the existing OCR worker."""
    selected_topic_id = parse_topic_id(topic_id)
    valid_files = [upload for upload in files if upload.filename]
    if not valid_files:
        raise HTTPException(status_code=400, detail="Choose at least one photo or PDF.")

    invalid_files = [
        upload.filename for upload in valid_files
        if os.path.splitext(upload.filename)[1].lower() not in ALLOWED_UPLOAD_EXTENSIONS
    ]
    if invalid_files:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, PNG, WebP, TIFF, and PDF files are supported.",
        )

    db = await get_db()
    try:
        await assert_topic_exists(db, selected_topic_id)
        cursor = await db.execute(
            "INSERT INTO sources (url, category, status, topic_id) VALUES (?, ?, ?, ?)",
            (f"upload://{valid_files[0].filename}", "Upload", "Saving upload", selected_topic_id),
        )
        await db.commit()
        source_id = cursor.lastrowid
    finally:
        await db.close()

    source_dir = os.path.join(UPLOAD_DIR, f"source_{source_id}")
    os.makedirs(source_dir, exist_ok=True)
    saved_paths = []
    try:
        for upload in valid_files:
            suffix = os.path.splitext(upload.filename)[1].lower()
            stored_name = f"{uuid.uuid4().hex}{suffix}"
            destination = os.path.join(source_dir, stored_name)
            total_size = 0
            with open(destination, "wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total_size += len(chunk)
                    if total_size > MAX_UPLOAD_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"{upload.filename} exceeds the 50 MB per-file limit.",
                        )
                    output.write(chunk)
            saved_paths.append(destination)
    except Exception:
        shutil.rmtree(source_dir, ignore_errors=True)
        db = await get_db()
        try:
            await db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            await db.commit()
        finally:
            await db.close()
        raise
    finally:
        for upload in valid_files:
            await upload.close()

    background_tasks.add_task(process_uploaded_files, source_id, saved_paths)
    return await get_sources(request, topic_id=topic_id)

@app.get("/sources", response_class=HTMLResponse)
async def get_sources(request: Request, topic_id: str | None = None):
    selected_topic_id = parse_topic_id(topic_id)
    # An explicit topic_id of "" means "All topics"; a real id narrows the list.
    filter_by_topic = selected_topic_id is not None

    db = await get_db()
    try:
        sources = await fetch_sources(db, selected_topic_id if filter_by_topic else None)
        topics = await fetch_topics(db)
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="partials/source_list.html",
        context={
            "request": request,
            "sources": sources,
            "topics": topics,
            "selected_topic_id": selected_topic_id,
        },
    )


@app.get("/browse", response_class=HTMLResponse)
async def browse_page(request: Request, topic_id: str | None = None):
    """Full-page sources table with filters, page counts and per-engine coverage."""
    db = await get_db()
    try:
        topics = await fetch_topics(db)
        categories = await fetch_categories(db)
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="browse.html",
        context={
            "request": request,
            "active_page": "browse",
            "topics": topics,
            "categories": categories,
            "selected_topic_id": parse_topic_id(topic_id),
        },
    )


@app.get("/sources/table", response_class=HTMLResponse)
async def get_sources_table(
    request: Request,
    topic_id: str | None = None,
    status: str = "",
    category: str = "",
    text: str = "",
    sort: str = "newest",
):
    selected_topic_id = parse_topic_id(topic_id)
    db = await get_db()
    try:
        sources = await fetch_sources(db, selected_topic_id, status, category, text, sort)
        topics = await fetch_topics(db)
        # Library-wide totals for the summary strip, ignoring the filters.
        cursor = await db.execute("""
            SELECT
                (SELECT COUNT(*) FROM sources) AS sources,
                (SELECT COUNT(*) FROM sources WHERE status NOT IN ('Completed', 'Failed')) AS active,
                (SELECT COUNT(*) FROM sources WHERE status = 'Failed') AS failed,
                (SELECT COUNT(*) FROM pages) AS pages
        """)
        totals = dict(await cursor.fetchone())
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="partials/source_table.html",
        context={
            "request": request,
            "sources": sources,
            "topics": topics,
            "totals": totals,
            "filtered": bool(selected_topic_id or status or category or text.strip()),
        },
    )


@app.get("/sources/{source_id}/text", response_class=PlainTextResponse)
async def get_source_text(source_id: int, download: bool = False):
    """Return all OCR text for a source, ordered by page."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM sources WHERE id = ?", (source_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Source not found")

        cursor = await db.execute(
            "SELECT ocr_text FROM pages WHERE source_id = ? ORDER BY id ASC",
            (source_id,),
        )
        pages = await cursor.fetchall()
    finally:
        await db.close()

    text = "\n\n".join(page["ocr_text"] or "" for page in pages).strip()
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="source-{source_id}-ocr.txt"'

    return PlainTextResponse(text, headers=headers)

async def add_page_details(results: list[dict], variants: list[str]):
    """Add what the search page shows beyond the home page's results: each
    hit's page number within its source, the source's page count, an image
    URL, and highlighted snippet and full text for each matching engine."""
    pattern = None
    alternatives = sorted({v for v in variants if v}, key=len, reverse=True)
    if alternatives:
        # Longest first, so a longer variant wins over a shorter one it contains.
        pattern = re.compile("|".join(re.escape(v) for v in alternatives))

    db = await get_db()
    try:
        positions = await library.page_positions(db, (row["source_id"] for row in results))
    finally:
        await db.close()

    for result in results:
        result["page_number"], result["page_total"] = positions.get(result["id"], (None, None))
        result["image_url"] = get_image_url(result["image_path"])
        for match in result["matches"]:
            match["snippet"] = highlight_segments(match["text"], pattern, snippet=True)
            match["full"] = highlight_segments(match["text"], pattern, snippet=False)


@app.get("/find", response_class=HTMLResponse)
async def search_page(request: Request, query: str = "", fuzzy: bool = False, topic_id: str | None = None):
    """Full-page search with filters and larger results."""
    db = await get_db()
    try:
        topics = await fetch_topics(db)
        categories = await fetch_categories(db)
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "request": request,
            "active_page": "search",
            "topics": topics,
            "categories": categories,
            "engines": [name for name, _ in OCR_ENGINES],
            "selected_topic_id": parse_topic_id(topic_id),
            "query": query,
            "fuzzy": fuzzy,
        },
    )


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    query: str = "",
    fuzzy: bool = False,
    topic_id: str | None = None,
    category: str = "",
    engine: str = "",
    layout: str = "",
):
    """Search page text. layout=full renders the larger results on the search
    page (page numbers, highlighted snippets, thumbnails); category and engine
    narrow results to one website or one OCR engine's text."""
    selected_topic_id = parse_topic_id(topic_id)
    full = layout == "full"
    results_template = "partials/search_results_full.html" if full else "partials/search_results.html"
    topic_name = None
    if selected_topic_id is not None:
        db = await get_db()
        try:
            cursor = await db.execute("SELECT name FROM topics WHERE id = ?", (selected_topic_id,))
            topic_row = await cursor.fetchone()
        finally:
            await db.close()
        if not topic_row:
            raise HTTPException(status_code=404, detail="Topic not found")
        topic_name = topic_row["name"]

    if not query.strip():
        return templates.TemplateResponse(
            request=request,
            name=results_template,
            context={"request": request, "results": [], "fuzzy": fuzzy, "topic_name": topic_name},
        )

    db = await get_db()
    try:
        results, search_queries = await library.search_pages(db, query, fuzzy, selected_topic_id, category, engine)
    finally:
        await db.close()

    if full:
        await add_page_details(results, search_queries)

    return templates.TemplateResponse(
        request=request,
        name=results_template,
        context={
            "request": request,
            "results": results,
            "source_count": len({r["source_id"] for r in results}),
            "query": query,
            "fuzzy": fuzzy,
            "variant_count": len(search_queries),
            "topic_name": topic_name,
        },
    )

@app.get("/viewer/{source_id}", response_class=HTMLResponse)
async def image_viewer(
    request: Request,
    source_id: int,
    focus_image_id: int | None = None,
    q: str = "",
    fuzzy: bool = False,
):
    """Page-by-page reader for one source.

    The page list is embedded up front; each page's text and layout are fetched
    from /api/pages/{id} as the reader moves. `q` (and `fuzzy`) carry a search
    over from the results list so the reader opens on that hit, highlighted.
    """
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
        source = await cursor.fetchone()
        if not source:
            return templates.TemplateResponse(
                request=request,
                name="viewer.html",
                context={"request": request, "source": None, "error": "Source not found"},
                status_code=404,
            )
        cursor = await db.execute(
            "SELECT id, image_path FROM pages WHERE source_id = ? ORDER BY id ASC", (source_id,)
        )
        pages = [{"id": row["id"], "image_url": get_image_url(row["image_path"])} for row in await cursor.fetchall()]
    finally:
        await db.close()

    query = q.strip()
    return templates.TemplateResponse(
        request=request,
        name="viewer.html",
        context={
            "request": request,
            "source": dict(source),
            "viewer_data": {
                "pages": pages,
                "focusPageId": focus_image_id,
                "query": query,
                "fuzzy": fuzzy,
                "variants": search_variants(query, fuzzy) if query else [],
            },
        },
    )


@app.get("/api/variants")
async def get_variants(q: str = "", fuzzy: bool = False):
    """Search variants for a query typed into the viewer's find box."""
    query = q.strip()
    return {"variants": search_variants(query, fuzzy) if query else []}


@app.get("/api/pages/{page_id}")
async def get_page(page_id: int):
    """One page's image, every engine's OCR text, and TongGuOCR column layout."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, source_id, image_path, ocr_text, tongguocr_text, paddleocr_text, tongguocr_layout FROM pages WHERE id = ?",
            (page_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row:
        raise HTTPException(status_code=404, detail="Page not found")

    layout = None
    if row["tongguocr_layout"]:
        try:
            layout = json.loads(row["tongguocr_layout"])
        except ValueError:
            layout = None
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "image_url": get_image_url(row["image_path"]),
        "image_path": row["image_path"],
        "hunyuan_text": row["ocr_text"],
        "tongguocr_text": row["tongguocr_text"],
        "paddleocr_text": row["paddleocr_text"],
        "layout": layout,
    }

def save_rotated_image(original: Path, target: Path, direction: str):
    transpose = Image.Transpose.ROTATE_90 if direction == "left" else Image.Transpose.ROTATE_270
    with Image.open(original) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise HTTPException(422, "Rotation is available for single-frame pages.")
        rotated = ImageOps.exif_transpose(image).transpose(transpose)
        if rotated.mode == "CMYK":
            rotated = rotated.convert("RGB")
        target.parent.mkdir(parents=True, exist_ok=True)
        rotated.save(target, format="PNG")


@app.post("/api/pages/{page_id}/rotate")
async def rotate_page(page_id: int, direction: Literal["left", "right"], image_url: str = Form(...)):
    page = await get_page(page_id)
    if page["image_url"] != image_url:
        raise HTTPException(409, "This page has changed. Reload it before rotating.")
    original = Path(page["image_path"])
    if not original.is_file():
        raise HTTPException(404, "Page image is unavailable.")
    directory = Path(UPLOAD_DIR, "rotations", str(page["source_id"]))
    target = directory / f"{page_id}-{uuid.uuid4().hex}.png"
    db = await get_db()
    try:
        try:
            await asyncio.to_thread(save_rotated_image, original, target, direction)
            cursor = await db.execute(
                "UPDATE pages SET image_path=?, ocr_text=NULL, tongguocr_text=NULL, "
                "tongguocr_layout=NULL, paddleocr_text=NULL WHERE id=? AND image_path=?",
                (str(target), page_id, page["image_path"]),
            )
            if not cursor.rowcount:
                raise HTTPException(409, "This page has changed. Reload it before rotating.")
            await db.commit()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if original.parent == directory:
            original.unlink(missing_ok=True)
        try:
            results = await ocr_page(str(target))
        except Exception as exc:
            print(f"OCR failed for rotated page {page_id}: {exc}")
            return {"indexed": False}
        await db.execute(
            "UPDATE pages SET ocr_text=?, tongguocr_text=?, tongguocr_layout=?, paddleocr_text=? "
            "WHERE id=? AND image_path=?", (*results, page_id, str(target)),
        )
        await db.commit()
        return {"indexed": all(value is not None for value in results)}
    finally:
        await db.close()


@app.post("/clear-database")
async def clear_database(request: Request):
    await clear_db()
    # Return empty source list and refresh the page
    return templates.TemplateResponse(
        request=request,
        name="partials/source_list.html",
        context={"request": request, "sources": [], "topics": [], "selected_topic_id": None},
    )
