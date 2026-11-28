"""Run PaddleOCR-VL-1.6 over every page that has no PaddleOCR-VL text yet.

Fills pages.paddleocr_text, the third searchable index alongside HunyuanOCR's
pages.ocr_text and TongGuOCR's pages.tongguocr_text. Safe to stop and re-run:
it only picks up pages that are still NULL, so it resumes where it left off and
also catches pages whose PaddleOCR-VL call failed during normal ingestion.

Pages are sent a few at a time: the pipeline server batches the text blocks of
concurrent requests into one pass of the model, which is much faster than one
page at a time.

Needs the PaddleOCR-VL containers running (cd PaddleOCRVL && docker compose up -d).

    python reindex_paddleocr.py            # all remaining pages
    python reindex_paddleocr.py --limit 20 # just the next 20
"""

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import ocr_engines
from PaddleOCRVL import PaddleOCRVL

# Same database the app uses (database.DB_PATH), resolved from this file so the
# script works from any directory without importing the app's async stack.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jiapu.db")

SERVER_WAIT_SECONDS = 300


def wait_for_server() -> bool:
    """True once the PaddleOCR-VL server answers, False if it stays down."""
    deadline = time.time() + SERVER_WAIT_SECONDS
    while not PaddleOCRVL.is_available():
        if time.time() > deadline:
            return False
        print("Waiting for the PaddleOCR-VL server...")
        time.sleep(10)
    return True


class ServerDown(Exception):
    pass


def ocr_page(row):
    page_id, image_path = row
    if not os.path.isfile(image_path):
        return page_id, None, f"image missing: {image_path}"
    # The workers pull pages from a queue filled up front, so if the server
    # goes away (e.g. a container restart) they must wait here and retry,
    # rather than race through every remaining page as a failure.
    for attempt in range(3):
        try:
            return page_id, PaddleOCRVL.recognize(image_path), None
        except requests.ConnectionError as exc:
            error = f"{type(exc).__name__}: {exc}"
            if not wait_for_server():
                raise ServerDown(error)
        except Exception as exc:
            return page_id, None, f"{type(exc).__name__}: {exc}"
    return page_id, None, error


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="Process at most this many pages.")
    parser.add_argument("--workers", type=int, default=4, help="Pages in flight at once (default 4).")
    args = parser.parse_args()

    # Page text is Chinese; don't let a non-UTF-8 console crash the run.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # The client prints every page's full text; that's noise for a bulk run.
    PaddleOCRVL.print = lambda *a, **k: None

    # Starts the engine if the app's idle monitor stopped it.
    ocr_engines.ensure_running("paddleocr")

    db = sqlite3.connect(DB_PATH, timeout=30.0)
    # The app's database may not have been opened since the column was added.
    columns = {row[1] for row in db.execute("PRAGMA table_info(pages)")}
    if "paddleocr_text" not in columns:
        db.execute("ALTER TABLE pages ADD COLUMN paddleocr_text TEXT")
        db.commit()

    pending = db.execute("SELECT id, image_path FROM pages WHERE paddleocr_text IS NULL ORDER BY id").fetchall()
    if args.limit is not None:
        pending = pending[: args.limit]
    total = len(pending)
    print(f"{total} pages to process")

    done = failed = 0
    started = time.time()
    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        for index, (page_id, text, error) in enumerate(pool.map(ocr_page, pending), start=1):
            if error is None:
                # Commit each page so the app sees progress and a stop loses nothing.
                image_path = pending[index - 1][1]
                db.execute("UPDATE pages SET paddleocr_text = ? WHERE id = ? AND image_path = ?", (text, page_id, image_path))
                db.commit()
                done += 1
                status = f"{len(text)} chars"
            else:
                failed += 1
                status = f"failed: {error}"
            rate = (time.time() - started) / index
            print(f"[{index}/{total}] page {page_id}: {status} | ETA {(total - index) * rate / 3600:.1f} h")
    except ServerDown:
        # A re-run picks up where this left off.
        print("PaddleOCR-VL server is down; stopping. Re-run once it is back up to resume.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    db.close()
    print(f"Finished: {done} indexed, {failed} failed or skipped (re-run to retry those).")


if __name__ == "__main__":
    main()
