from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import opencc
import os
import json
import shutil
import uuid
from functools import lru_cache
from database import init_db, get_db, clear_db
from tasks import process_jiapu_source, process_uploaded_files
from contextlib import asynccontextmanager
import itertools

# Initialize opencc converters
s2t = opencc.OpenCC('s2t')
t2s = opencc.OpenCC('t2s')

SIMILAR_CHARACTERS_PATH = "similarCharacters.json"
MAX_FUZZY_OPTIONS_PER_CHAR = 6
MAX_FUZZY_QUERIES = 500
UPLOAD_DIR = "uploads"
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".pdf"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB on startup
    await init_db()
    yield

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

def categorize_url(url: str) -> str:
    if "familysearch.org" in url.lower():
        return "FamilySearch"
    elif "mychinaroots.com" in url.lower() or "mychinarootslibrary-com" in url.lower():
        return "MyChinaRoots"
    elif "ztzupu.com" in url.lower():
        return "ZtZupu"
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

def chunked(iterable, size):
    it = iter(iterable)
    while chunk := list(itertools.islice(it, size)):
        yield chunk


@lru_cache(maxsize=1)
def load_confusion_map() -> dict:
    if not os.path.exists(SIMILAR_CHARACTERS_PATH):
        return {}

    try:
        with open(SIMILAR_CHARACTERS_PATH, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def get_fuzzy_options(char: str, confusion_map: dict) -> list[str]:
    options = [char]
    predictions = confusion_map.get(char, {})

    ranked_predictions = sorted(
        predictions.items(),
        key=lambda item: (
            item[1].get("count", 0),
            item[1].get("score_sum", 0) / (item[1].get("count") or 1),
        ),
        reverse=True,
    )

    for predicted_char, _stats in ranked_predictions:
        if predicted_char and predicted_char not in options:
            options.append(predicted_char)

        if len(options) >= MAX_FUZZY_OPTIONS_PER_CHAR:
            break

    return options


def expand_fuzzy_queries(query: str) -> list[str]:
    confusion_map = load_confusion_map()
    base_queries = [query, t2s.convert(query), s2t.convert(query)]
    expanded_queries = []
    seen = set()

    for base_query in base_queries:
        combinations = [""]

        for char in base_query:
            options = get_fuzzy_options(char, confusion_map)
            next_combinations = []

            for prefix in combinations:
                for option in options:
                    next_combinations.append(prefix + option)

            combinations = next_combinations[:MAX_FUZZY_QUERIES]

        for expanded_query in combinations:
            if expanded_query and expanded_query not in seen:
                seen.add(expanded_query)
                expanded_queries.append(expanded_query)

            if len(expanded_queries) >= MAX_FUZZY_QUERIES:
                return expanded_queries

    return expanded_queries

def parse_topic_id(topic_id: str | None) -> int | None:
    """Form selects submit "" for the All Topics option; treat that as unassigned."""
    if topic_id is None or not str(topic_id).strip():
        return None
    try:
        return int(topic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid topic.")


async def fetch_topics(db):
    """Topics with a source count, newest activity first."""
    cursor = await db.execute("""
        SELECT t.id, t.name, t.created_at, COUNT(s.id) AS source_count
        FROM topics t
        LEFT JOIN sources s ON s.topic_id = t.id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE ASC
    """)
    return await cursor.fetchall()


async def assert_topic_exists(db, topic_id: int | None):
    if topic_id is None:
        return
    cursor = await db.execute("SELECT id FROM topics WHERE id = ?", (topic_id,))
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Topic not found")


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
        context={"request": request, "topics": topics, "selected_topic_id": None},
    )


@app.post("/topics", response_class=HTMLResponse)
async def create_topic(request: Request, name: str = Form(...)):
    topic_name = name.strip()
    if not topic_name:
        raise HTTPException(status_code=400, detail="Topic name cannot be empty.")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM topics WHERE name = ? COLLATE NOCASE", (topic_name,))
        existing = await cursor.fetchone()
        if existing:
            topic_id = existing["id"]
        else:
            cursor = await db.execute("INSERT INTO topics (name) VALUES (?)", (topic_name,))
            await db.commit()
            topic_id = cursor.lastrowid

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
    new_topic_id = parse_topic_id(topic_id)
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

    return await get_sources(request, topic_id=filter_topic_id)


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
    db = await get_db()
    try:
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

    return await get_sources(request, topic_id=filter_topic_id)


@app.post("/add-source", response_class=HTMLResponse)
async def add_source(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    topic_id: str = Form(None),
):
    category = categorize_url(url)
    selected_topic_id = parse_topic_id(topic_id)

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

    # After adding, we just want to return the updated list of sources.
    # We can fetch them and render the partial.
    return await get_sources(request, topic_id=topic_id)


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
        query = """
            SELECT s.*, t.name AS topic_name
            FROM sources s
            LEFT JOIN topics t ON s.topic_id = t.id
        """
        params = []
        if filter_by_topic:
            query += " WHERE s.topic_id = ?"
            params.append(selected_topic_id)
        query += " ORDER BY s.created_at DESC"

        cursor = await db.execute(query, params)
        sources = await cursor.fetchall()
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

@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, query: str = "", fuzzy: bool = False, topic_id: str | None = None):
    selected_topic_id = parse_topic_id(topic_id)

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
            name="partials/search_results.html",
            context={"request": request, "results": [], "fuzzy": fuzzy, "topic_name": topic_name},
        )

    search_queries = expand_fuzzy_queries(query) if fuzzy else [query, t2s.convert(query), s2t.convert(query)]
    search_queries = list(dict.fromkeys(search_queries))

    # SQLite's default limit is 999 bound params; stay comfortably under that per statement.
    # The topic filter consumes one, so leave room for it.
    BATCH_SIZE = 900

    db = await get_db()
    try:
        rows_by_id = {}
        match_counts = {}

        for batch in chunked(search_queries, BATCH_SIZE):
            like_clauses = " OR ".join(["p.ocr_text LIKE ?"] * len(batch))
            params = [f"%{q}%" for q in batch]

            topic_clause = ""
            if selected_topic_id is not None:
                topic_clause = " AND s.topic_id = ?"
                params.append(selected_topic_id)

            cursor = await db.execute(f"""
                SELECT p.id, p.source_id, p.ocr_text, p.image_path, s.url, s.category,
                       s.title AS source_title, t.name AS topic_name
                FROM pages p
                JOIN sources s ON p.source_id = s.id
                LEFT JOIN topics t ON s.topic_id = t.id
                WHERE ({like_clauses}){topic_clause}
            """, params)
            batch_rows = await cursor.fetchall()

            for row in batch_rows:
                rows_by_id[row["id"]] = row
                match_counts[row["id"]] = match_counts.get(row["id"], 0) + sum(
                    1 for q in batch if q in row["ocr_text"]
                )
    finally:
        await db.close()

    def sort_key(row):
        exact_match = query in row["ocr_text"]
        return (not exact_match, -match_counts[row["id"]])

    results = sorted(rows_by_id.values(), key=sort_key)

    return templates.TemplateResponse(
        request=request,
        name="partials/search_results.html",
        context={
            "request": request,
            "results": results,
            "query": query,
            "fuzzy": fuzzy,
            "variant_count": len(search_queries),
            "topic_name": topic_name,
        },
    )

@app.get("/viewer/{source_id}", response_class=HTMLResponse)
async def image_viewer(request: Request, source_id: int, focus_image_id: int = None):
    """Serve the image viewer page for a specific source"""
    db = await get_db()
    initial_page = 0
    try:
        # Get source info
        cursor = await db.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
        source = await cursor.fetchone()
        
        if not source:
            return templates.TemplateResponse(
                request=request, 
                name="viewer.html", 
                context={"request": request, "source": None, "error": "Source not found"}
            )
        
        # Get image count for this source
        cursor = await db.execute("SELECT COUNT(*) as count FROM pages WHERE source_id = ?", (source_id,))
        count_row = await cursor.fetchone()
        image_count = count_row['count'] if count_row else 0

        if focus_image_id is not None:
            cursor = await db.execute(
                "SELECT COUNT(*) as rank FROM pages WHERE source_id = ? AND id <= ?",
                (source_id, focus_image_id)
            )
            rank_row = await cursor.fetchone()
            if rank_row and rank_row['rank']:
                initial_page = max(0, (rank_row['rank'] - 1) // 12)
    finally:
        await db.close()
    
    return templates.TemplateResponse(
        request=request, 
        name="viewer.html", 
        context={
            "request": request, 
            "source": dict(source),
            "image_count": image_count,
            "focus_image_id": focus_image_id,
            "initial_page": initial_page
        }
    )

@app.get("/api/images/{source_id}", response_class=HTMLResponse)
async def get_images(request: Request, source_id: int, page: int = 0):
    """API endpoint to fetch images for a specific source with pagination"""
    page = max(0, page)
    page_size = 12
    offset = page * page_size
    
    db = await get_db()
    try:
        # Get paginated images
        cursor = await db.execute("""
            SELECT id, image_path, ocr_text 
            FROM pages 
            WHERE source_id = ?
            ORDER BY id ASC
            LIMIT ? OFFSET ?
        """, (source_id, page_size, offset))
        raw_images = await cursor.fetchall()
        
        # Get total count
        cursor = await db.execute("SELECT COUNT(*) as count FROM pages WHERE source_id = ?", (source_id,))
        count_row = await cursor.fetchone()
        total_count = count_row['count'] if count_row else 0
        
    finally:
        await db.close()

    images = []
    for img in raw_images:
        row = dict(img)
        row["image_url"] = get_image_url(row["image_path"])
        images.append(row)
    
    return templates.TemplateResponse(
        request=request,
        name="partials/image_grid.html",
        context={
            "request": request,
            "images": images,
            "source_id": source_id,
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "has_more": (page + 1) * page_size < total_count
        }
    )

@app.post("/clear-database")
async def clear_database(request: Request):
    await clear_db()
    # Return empty source list and refresh the page
    return templates.TemplateResponse(
        request=request,
        name="partials/source_list.html",
        context={"request": request, "sources": [], "topics": [], "selected_topic_id": None},
    )
