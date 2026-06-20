from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import opencc
import os
from database import init_db, get_db, clear_db
from tasks import process_jiapu_source
from contextlib import asynccontextmanager

# Initialize opencc converters
s2t = opencc.OpenCC('s2t')
t2s = opencc.OpenCC('t2s')

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

def categorize_url(url: str) -> str:
    if "familysearch.org" in url.lower():
        return "FamilySearch"
    elif "mychinaroots.com" in url.lower():
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
    }

    for root, prefix in roots.items():
        if abs_path == root or abs_path.startswith(root + "/"):
            rel_path = os.path.relpath(abs_path, root).replace('\\', '/')
            return f"{prefix}/{rel_path}"

    return normalized

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

@app.post("/add-source", response_class=HTMLResponse)
async def add_source(request: Request, background_tasks: BackgroundTasks, url: str = Form(...)):
    category = categorize_url(url)
    
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO sources (url, category, status) VALUES (?, ?, ?)",
            (url, category, "Pending")
        )
        await db.commit()
        source_id = cursor.lastrowid
        
        # Dispatch background task
        background_tasks.add_task(process_jiapu_source, source_id, url, category)
        
    finally:
        await db.close()

    # After adding, we just want to return the updated list of sources.
    # We can fetch them and render the partial.
    return await get_sources(request)

@app.get("/sources", response_class=HTMLResponse)
async def get_sources(request: Request):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sources ORDER BY created_at DESC")
        sources = await cursor.fetchall()
    finally:
        await db.close()
        
    return templates.TemplateResponse(request=request, name="partials/source_list.html", context={"request": request, "sources": sources})

@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, query: str = ""):
    if not query.strip():
        return templates.TemplateResponse(request=request, name="partials/search_results.html", context={"request": request, "results": []})
        
    simplified_query = t2s.convert(query)
    traditional_query = s2t.convert(query)
    
    db = await get_db()
    try:
        # Search for both variants in the pages table using LIKE
        # Using DISTINCT to avoid duplicates if simplified == traditional
        cursor = await db.execute("""
            SELECT p.id, p.source_id, p.ocr_text, p.image_path, s.url, s.category 
            FROM pages p
            JOIN sources s ON p.source_id = s.id
            WHERE p.ocr_text LIKE ? OR p.ocr_text LIKE ?
        """, (f"%{simplified_query}%", f"%{traditional_query}%"))
        results = await cursor.fetchall()
    finally:
        await db.close()
        
    return templates.TemplateResponse(request=request, name="partials/search_results.html", context={"request": request, "results": results, "query": query})

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
    return templates.TemplateResponse(request=request, name="partials/source_list.html", context={"request": request, "sources": []})
