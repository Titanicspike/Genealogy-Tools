import asyncio
import os
import sys
import traceback
import requests
import base64
import re
import fitz
from HunyuanOCR.HunyuanOCR import extractText
print(os.path.join(os.path.dirname(__file__), 'CamoufoxScraping'))
# Add CamoufoxScraping to the path so we can import the scrapers
sys.path.append(os.path.join(os.path.dirname(__file__), 'CamoufoxScraping'))

from Scraping import FamilySearch, MCR, ztzupu
from database import get_db


def format_error(exc: Exception) -> str:
    """Build a short, human-readable reason for a failed source.

    Kept concise since it surfaces in a hover tooltip on the source list. The
    exception type is included because some errors (e.g. TimeoutError) carry an
    empty message that would otherwise be useless on its own.
    """
    message = str(exc).strip()
    reason = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return reason[:500]


async def perform_ocr(source_id: int, image_dir: str):
    if not image_dir or not os.path.isdir(image_dir):
        print(f"Directory not found or invalid: {image_dir}")
        return

    print(f"Running OCR on images in {image_dir}")
    
    for filename in os.listdir(image_dir):
        if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            image_path = os.path.join(image_dir, filename)
            print(f"Processing {image_path}...")
            ocr_text = await asyncio.to_thread(extractText, image_path)
            
            # Open a fresh connection for each image to avoid long locks
            db = await get_db()
            try:
                await db.execute(
                    "INSERT INTO pages (source_id, image_path, ocr_text) VALUES (?, ?, ?)",
                    (source_id, image_path, ocr_text)
                )
                await db.commit()
            finally:
                await db.close()


def render_pdf_pages(pdf_path: str, output_dir: str) -> list[str]:
    """Render each PDF page at a readable resolution for the vision OCR model."""
    os.makedirs(output_dir, exist_ok=True)
    rendered_pages = []
    with fitz.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            output_path = os.path.join(output_dir, f"page_{page_number:04d}.png")
            pixmap.save(output_path)
            rendered_pages.append(output_path)
    return rendered_pages


async def process_uploaded_files(source_id: int, file_paths: list[str]):
    """OCR uploaded image files and rasterize PDFs into OCR-ready page images."""
    try:
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sources SET status = 'Preparing files', error = NULL WHERE id = ?",
                (source_id,),
            )
            await db.commit()
        finally:
            await db.close()

        image_paths = []
        for file_path in file_paths:
            if file_path.lower().endswith(".pdf"):
                rendered_dir = os.path.join(os.path.dirname(file_path), "rendered")
                image_paths.extend(await asyncio.to_thread(render_pdf_pages, file_path, rendered_dir))
            else:
                image_paths.append(file_path)

        db = await get_db()
        try:
            await db.execute("UPDATE sources SET status = 'Running OCR' WHERE id = ?", (source_id,))
            await db.commit()
        finally:
            await db.close()

        for image_path in image_paths:
            ocr_text = await asyncio.to_thread(extractText, image_path)
            db = await get_db()
            try:
                await db.execute(
                    "INSERT INTO pages (source_id, image_path, ocr_text) VALUES (?, ?, ?)",
                    (source_id, image_path, ocr_text),
                )
                await db.commit()
            finally:
                await db.close()

        db = await get_db()
        try:
            await db.execute("UPDATE sources SET status = 'Completed' WHERE id = ?", (source_id,))
            await db.commit()
        finally:
            await db.close()
    except Exception as exc:
        print(f"Failed to OCR uploaded source {source_id}: {exc}")
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sources SET status = 'Failed', error = ? WHERE id = ?",
                (format_error(exc), source_id),
            )
            await db.commit()
        finally:
            await db.close()

def run_scraper_sync(category: str, url: str):
    print(f"Running scraper for {category} with URL: {url}")
    # Camoufox drives a browser subprocess, which on Windows needs the Proactor
    # loop; the Selector loop there cannot spawn subprocesses at all. Importing
    # ztzupu sets a global Selector policy, so reassert Proactor before every
    # scrape. On other platforms the default loop already handles subprocesses,
    # and these policy classes do not exist.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        if category == "FamilySearch":
            print([url])
            return FamilySearch.main([url])
        elif category == "MyChinaRoots":
            return MCR.main([url.split("/")[-1]])
        elif category == "ZtZupu":
            return ztzupu.main([re.sub(r'[^\d]', '', url)])
        else:
            print(f"Unknown category {category}")
            return None
    except Exception as e:
        print(f"Error during scraping: {e}")
        print(f"Error type: {type(e)}")
        print(f"Error: {e}")
        print(f"Traceback: {traceback.format_exc()}")  # full traceback
        raise e

async def process_jiapu_source(source_id: int, url: str, category: str):
    try:
        print("1")
        # Update status to Scraping
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sources SET status = 'Scraping', error = NULL WHERE id = ?",
                (source_id,),
            )
            await db.commit()
        finally:
            await db.close()
        
        print("2")
        # Run the synchronous scraper in a separate thread so it doesn't block the async event loop
        image_dir = await asyncio.to_thread(run_scraper_sync, category, url)
        
        print("3")
        # Update status to OCR, and record the book's title. The scraper returns
        # the directory it saved into, which is named after the book itself.
        # normpath first so a trailing separator does not yield an empty name.
        title = os.path.basename(os.path.normpath(image_dir)) if image_dir else None
        db = await get_db()
        try:
            if title:
                await db.execute(
                    "UPDATE sources SET status = 'Running OCR', title = ? WHERE id = ?",
                    (title, source_id),
                )
            else:
                await db.execute("UPDATE sources SET status = 'Running OCR' WHERE id = ?", (source_id,))
            await db.commit()
        finally:
            await db.close()
        
        print("4")
        # Run the OCR
        if image_dir:
            await perform_ocr(source_id, image_dir)
        else:
            print("No image directory returned from scraper.")
        
        print("5")
        # Update status to Completed
        db = await get_db()
        try:
            await db.execute("UPDATE sources SET status = 'Completed' WHERE id = ?", (source_id,))
            await db.commit()
        finally:
            await db.close()
        
        print("6")
    except Exception as e:
        print(f"Failed to process source {source_id}: {e}")
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sources SET status = 'Failed', error = ? WHERE id = ?",
                (format_error(e), source_id),
            )
            await db.commit()
        finally:
            await db.close()
