import asyncio
import functools
import os
import sys
import traceback
import requests
import base64
import re
import fitz
from PIL import Image
from HunyuanOCR.HunyuanOCR import extractText
from TongGuOCR import TongGuOCR
from PaddleOCRVL import PaddleOCRVL
import ocr_engines
print(os.path.join(os.path.dirname(__file__), 'CamoufoxScraping'))
# Add CamoufoxScraping to the path so we can import the scrapers
sys.path.append(os.path.join(os.path.dirname(__file__), 'CamoufoxScraping'))

from Scraping import FamilySearch, MCR, ztzupu
from database import get_db

# How many sources from one website may scrape/OCR at once. Sources from
# different websites (FamilySearch, MyChinaRoots, ZtZupu, uploads) run side by
# side; more from the same site wait in submission order with status 'Pending'.
# One per site by default so each site sees a single browser or download burst.
MAX_CONCURRENT_SOURCES_PER_SITE = max(1, int(os.environ.get("MAX_CONCURRENT_SOURCES_PER_SITE", "1")))
_site_slots: dict[str, asyncio.Semaphore] = {}
# How many pages of one source are read at once. All three engines share one
# GPU, so this only helps until the GPU is saturated: on an RTX 5060 Ti, 4 pages
# in flight read ~25% faster than 1 (26 vs 35 s/page), while 2 was no faster.
# TongGuOCR reads one page at a time and queues the rest.
PAGE_OCR_CONCURRENCY = max(1, int(os.environ.get("PAGE_OCR_CONCURRENCY", "4")))


# Live progress of sources being worked on, by source id. Jobs only live in
# this process, so this is kept in memory rather than in the database:
#   downloaded / download_total  page images saved by the scraper, and how many
#                                the book has (None when the site doesn't say)
#   ocr_done / ocr_total         pages OCR'd and saved, and pages queued for OCR
# Entries are dropped when a source finishes or fails.
progress: dict[int, dict] = {}


def start_progress(source_id: int, download_total: int | None = None, ocr_total: int = 0):
    progress[source_id] = {
        "downloaded": 0,
        "download_total": download_total,
        "ocr_done": 0,
        "ocr_total": ocr_total,
    }


def update_progress(source_id: int, **changes):
    if source_id in progress:
        progress[source_id].update(changes)


def bump_progress(source_id: int, key: str, by: int = 1):
    if source_id in progress:
        progress[source_id][key] += by


def site_slots(category: str) -> asyncio.Semaphore:
    """The semaphore limiting concurrent sources for one website (category)."""
    if category not in _site_slots:
        _site_slots[category] = asyncio.Semaphore(MAX_CONCURRENT_SOURCES_PER_SITE)
    return _site_slots[category]


def format_error(exc: Exception) -> str:
    """Build a short, human-readable reason for a failed source.

    Kept concise since it surfaces in a hover tooltip on the source list. The
    exception type is included because some errors (e.g. TimeoutError) carry an
    empty message that would otherwise be useless on its own.
    """
    message = str(exc).strip()
    reason = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return reason[:500]


async def run_tongguocr(image_path: str) -> tuple[str | None, str | None]:
    """TongGuOCR (text, layout JSON) for a page, or (None, None) on failure.

    TongGuOCR is a second index alongside HunyuanOCR, so a failure here must not
    fail the whole source; reindex_tongguocr.py picks up any NULL pages later.
    """
    try:
        return await asyncio.to_thread(TongGuOCR.recognize, image_path)
    except Exception as exc:
        print(f"TongGuOCR failed for {image_path}: {format_error(exc)}")
        return None, None


async def run_paddleocr(image_path: str) -> str | None:
    """PaddleOCR-VL text for a page, or None on failure.

    Like TongGuOCR, a failure leaves the column NULL for reindex_paddleocr.py
    to fill in later rather than failing the source.
    """
    try:
        return await asyncio.to_thread(PaddleOCRVL.recognize, image_path)
    except Exception as exc:
        print(f"PaddleOCR-VL failed for {image_path}: {format_error(exc)}")
        return None


async def ocr_page(image_path: str) -> tuple:
    """Read one page with all three engines: (Hunyuan, TongGuOCR text, TongGuOCR layout, PaddleOCR-VL)."""
    # Engines stopped for being idle are started one at a time first; starting
    # them all at once from the parallel calls below can starve one of memory.
    await asyncio.to_thread(ocr_engines.ensure_all)
    # The three engines are separate servers, so run them side by side.
    ocr_text, (tongguocr_text, tongguocr_layout), paddleocr_text = await asyncio.gather(
        asyncio.to_thread(extractText, image_path),
        run_tongguocr(image_path),
        run_paddleocr(image_path),
    )
    return ocr_text, tongguocr_text, tongguocr_layout, paddleocr_text


def is_image_file(path: str) -> bool:
    """Whether path holds a real image; Pillow only reads its header.

    Blocked ZtZupu pages are HTML error pages under a .jpg name (scrapes from
    before the scraper skipped them left these on disk), and the OCR engines
    fail the whole source on them.
    """
    try:
        with Image.open(path):
            return True
    except Exception:
        return False


async def save_page(source_id: int, image_path: str, ocr_results: tuple):
    # Open a fresh connection for each image to avoid long locks
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO pages (source_id, image_path, ocr_text, tongguocr_text, tongguocr_layout, paddleocr_text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, image_path, *ocr_results),
        )
        await db.commit()
    finally:
        await db.close()


async def ocr_pages(source_id: int, image_paths: "list[str] | asyncio.Queue"):
    """OCR a source's pages PAGE_OCR_CONCURRENCY at a time, saving them in page order.

    image_paths is either a list, or a queue that a scraper feeds as each image
    is downloaded and ends with None, so OCR can start on the first page while
    the rest of the book is still downloading. A path seen twice is read once.

    The viewer and text export order pages by row id, so a page that finishes
    early waits for the pages before it to be saved first. If a page fails, the
    pages still being read are abandoned and the error fails the source. A file
    that isn't a readable image (such as a blocked ZtZupu page) is skipped.
    """
    if isinstance(image_paths, asyncio.Queue):
        incoming = image_paths
    else:
        incoming = asyncio.Queue()
        for path in image_paths:
            incoming.put_nowait(path)
        incoming.put_nowait(None)

    slots = asyncio.Semaphore(PAGE_OCR_CONCURRENCY)
    in_order: asyncio.Queue = asyncio.Queue()  # (path, read task) in arrival order, then None
    reads: list[asyncio.Task] = []
    seen: set[str] = set()

    async def read(image_path: str) -> tuple:
        async with slots:
            print(f"Processing {image_path}...")
            return await ocr_page(image_path)

    async def save_in_order():
        while (item := await in_order.get()) is not None:
            image_path, page_read = item
            await save_page(source_id, image_path, await page_read)
            bump_progress(source_id, "ocr_done")

    saver = asyncio.create_task(save_in_order())
    try:
        while True:
            # Wait for the next image, but stop as soon as a page fails rather
            # than only noticing when the next image turns up.
            next_path = asyncio.create_task(incoming.get())
            await asyncio.wait({next_path, saver}, return_when=asyncio.FIRST_COMPLETED)
            if not next_path.done():
                next_path.cancel()
                saver.result()  # the saver only stops early by raising a page's error
            image_path = next_path.result()
            if image_path is None:
                break
            key = os.path.normcase(os.path.abspath(image_path))
            if key in seen:
                continue
            seen.add(key)
            if not is_image_file(image_path):
                print(f"Skipping {image_path}: not a readable image (blocked page?)")
                continue
            page_read = asyncio.create_task(read(image_path))
            reads.append(page_read)
            update_progress(source_id, ocr_total=len(reads))
            in_order.put_nowait((image_path, page_read))
        in_order.put_nowait(None)
        await saver
    finally:
        for task in (*reads, saver):
            task.cancel()
        # Collect the cancelled tasks so their errors aren't reported as unhandled.
        await asyncio.gather(*reads, saver, return_exceptions=True)


def page_sort_key(filename: str) -> list:
    """Sort 2.jpg before 10.jpg, so numbered pages keep the book's order."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", filename)]


def image_files(image_dir: str) -> list[str]:
    """The page images in a scraped book's directory, in page order."""
    return [
        os.path.join(image_dir, filename)
        for filename in sorted(os.listdir(image_dir), key=page_sort_key)
        if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
    ]


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
    async with site_slots("Upload"):
        start_progress(source_id)
        try:
            await _process_uploaded_files(source_id, file_paths)
        finally:
            progress.pop(source_id, None)


async def _process_uploaded_files(source_id: int, file_paths: list[str]):
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

        await ocr_pages(source_id, image_paths)

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

def run_scraper_sync(category: str, url: str, on_image=None, on_total=None, on_saved=None) -> str | None:
    """Scrape one book and return the directory its page images were saved in.

    FamilySearch and MyChinaRoots download pages one after another and call
    on_image(path) as each is saved, so OCR can start right away. ZtZupu
    downloads pages in parallel, out of order, so its pages are only picked up
    from the returned directory once the whole book is downloaded.

    on_total(n) is called once the scraper knows how many pages the book has,
    and on_saved() as ZtZupu saves each page.
    """
    print(f"Running scraper for {category} with URL: {url}")
    try:
        if category == "FamilySearch":
            saved_dirs = FamilySearch.main([url], on_image=on_image, on_total=on_total)
        elif category == "MyChinaRoots":
            saved_dirs = MCR.main([url.split("/")[-1]], on_image=on_image, on_total=on_total)
        elif category == "ZtZupu":
            saved_dirs = ztzupu.main([re.sub(r'[^\d]', '', url)], on_total=on_total, on_saved=on_saved)
        else:
            print(f"Unknown category {category}")
            return None
        return saved_dirs[0] if saved_dirs else None
    except Exception as e:
        print(f"Error during scraping: {e}")
        print(f"Error type: {type(e)}")
        print(f"Error: {e}")
        print(f"Traceback: {traceback.format_exc()}")  # full traceback
        raise e

async def process_jiapu_source(source_id: int, url: str, category: str):
    """Scrape a book, then OCR its pages, once its website has a free slot."""
    async with site_slots(category):
        start_progress(source_id)
        try:
            await _process_jiapu_source(source_id, url, category)
        finally:
            progress.pop(source_id, None)


class OCRStopped(Exception):
    """Raised in the scraper thread to abandon a scrape once its OCR has failed."""


async def set_source_status(source_id: int, status: str, title: str | None = None):
    db = await get_db()
    try:
        if title:
            await db.execute(
                "UPDATE sources SET status = ?, title = ? WHERE id = ?", (status, title, source_id)
            )
        else:
            await db.execute("UPDATE sources SET status = ? WHERE id = ?", (status, source_id))
        await db.commit()
    finally:
        await db.close()


async def _process_jiapu_source(source_id: int, url: str, category: str):
    loop = asyncio.get_running_loop()
    incoming: asyncio.Queue = asyncio.Queue()
    ocr_task: asyncio.Task | None = None
    first_image = True

    async def accept_image(image_path: str):
        nonlocal first_image
        if ocr_task.done():
            raise OCRStopped("OCR stopped, so the scrape was abandoned.")
        if first_image:
            first_image = False
            # The scrapers save each book into a directory named after it.
            title = os.path.basename(os.path.dirname(image_path))
            await set_source_status(source_id, "Scraping + OCR", title)
        bump_progress(source_id, "downloaded")
        incoming.put_nowait(image_path)

    def on_total(total: int):
        """Called from the scraper's thread once it knows the book's page count."""
        # call_soon_threadsafe takes no keyword arguments, so bind them first.
        loop.call_soon_threadsafe(functools.partial(update_progress, source_id, download_total=total))

    def on_saved():
        """Called from ZtZupu's thread for each page saved (it has no on_image)."""
        loop.call_soon_threadsafe(bump_progress, source_id, "downloaded")

    def on_image(image_path: str):
        """Called from the scraper's thread each time it saves a page image."""
        # Wait for accept_image so images are queued in the order they arrive,
        # and so an OCR failure stops the scraper at its next image.
        asyncio.run_coroutine_threadsafe(accept_image(image_path), loop).result()

    try:
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sources SET status = 'Scraping', error = NULL WHERE id = ?", (source_id,)
            )
            await db.commit()
        finally:
            await db.close()

        ocr_task = asyncio.create_task(ocr_pages(source_id, incoming))

        # Run the synchronous scraper in a separate thread so it doesn't block the event loop
        image_dir = await asyncio.to_thread(run_scraper_sync, category, url, on_image, on_total, on_saved)

        # Record the book's title from the directory the scraper returns.
        # normpath first so a trailing separator does not yield an empty name.
        title = os.path.basename(os.path.normpath(image_dir)) if image_dir else None
        await set_source_status(source_id, "Running OCR", title)
        # The scrape is over, so whatever it saved is the book's full page count.
        if source_id in progress and progress[source_id]["download_total"] is None:
            update_progress(source_id, download_total=progress[source_id]["downloaded"])

        # Queue any page the scraper didn't report as it went (all of ZtZupu's);
        # pages OCR already has are skipped.
        if image_dir and os.path.isdir(image_dir):
            for image_path in image_files(image_dir):
                incoming.put_nowait(image_path)
        else:
            print(f"No image directory returned from scraper: {image_dir}")
        incoming.put_nowait(None)
        await ocr_task

        await set_source_status(source_id, "Completed")
    except Exception as e:
        # When OCR failed first, the scrape was abandoned because of it, so
        # OCR's error is the real reason.
        if ocr_task and ocr_task.done() and not ocr_task.cancelled() and ocr_task.exception():
            e = ocr_task.exception()
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
    finally:
        if ocr_task and not ocr_task.done():
            ocr_task.cancel()
            await asyncio.gather(ocr_task, return_exceptions=True)
