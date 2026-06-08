from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import opencc
import os
from database import init_db, get_db
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

def categorize_url(url: str) -> str:
    if "familysearch.org" in url.lower():
        return "FamilySearch"
    elif "mychinaroots.com" in url.lower():
        return "MyChinaRoots"
    elif "ztzupu.com" in url.lower():
        return "ZtZupu"
    return "Unknown"

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
            SELECT p.id, p.ocr_text, p.image_path, s.url, s.category 
            FROM pages p
            JOIN sources s ON p.source_id = s.id
            WHERE p.ocr_text LIKE ? OR p.ocr_text LIKE ?
        """, (f"%{simplified_query}%", f"%{traditional_query}%"))
        results = await cursor.fetchall()
    finally:
        await db.close()
        
    return templates.TemplateResponse(request=request, name="partials/search_results.html", context={"request": request, "results": results, "query": query})
