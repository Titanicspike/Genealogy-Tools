"""Run TongGuOCR over every page that doesn't have TongGuOCR text yet.

Fills pages.tongguocr_text alongside the existing HunyuanOCR text in
pages.ocr_text. Safe to stop and re-run: it only picks up pages that are still
NULL, so it resumes where it left off and also catches pages whose TongGuOCR
call failed during normal ingestion.

Needs the TongGuOCR server running in WSL (TongGuOCR/start_server.sh).

    python reindex_tongguocr.py            # all remaining pages
    python reindex_tongguocr.py --limit 20 # just the next 20
"""

import argparse
import os
import sqlite3
import sys
import time

from TongGuOCR import TongGuOCR

# Same database the app uses (database.DB_PATH), resolved from this file so the
# script works from any directory without importing the app's async stack.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jiapu.db")


SERVER_WAIT_SECONDS = 300


def wait_for_server() -> bool:
    """True once the TongGuOCR server answers, False if it stays down."""
    deadline = time.time() + SERVER_WAIT_SECONDS
    while not TongGuOCR.is_available():
        if time.time() > deadline:
            return False
        print("Waiting for the TongGuOCR server...")
        time.sleep(10)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="Process at most this many pages.")
    args = parser.parse_args()

    # Page text is Chinese; don't let a non-UTF-8 console crash the run.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not wait_for_server():
        raise SystemExit(f"TongGuOCR server is not reachable at {TongGuOCR.TONGGUOCR_URL}. Start TongGuOCR/start_server.sh in WSL first.")

    db = sqlite3.connect(DB_PATH, timeout=30.0)
    # The app's database may not have been opened since the column was added.
    columns = {row[1] for row in db.execute("PRAGMA table_info(pages)")}
    if "tongguocr_text" not in columns:
        db.execute("ALTER TABLE pages ADD COLUMN tongguocr_text TEXT")
        db.commit()

    pending = db.execute(
        "SELECT id, image_path FROM pages WHERE tongguocr_text IS NULL ORDER BY id"
    ).fetchall()
    if args.limit is not None:
        pending = pending[: args.limit]
    total = len(pending)
    print(f"{total} pages to process")

    done = failed = 0
    started = time.time()
    for index, (page_id, image_path) in enumerate(pending, start=1):
        if not os.path.isfile(image_path):
            print(f"[{index}/{total}] page {page_id}: image missing, skipped: {image_path}")
            failed += 1
            continue
        try:
            text = TongGuOCR.extractText(image_path)
        except Exception as exc:
            print(f"[{index}/{total}] page {page_id}: failed: {type(exc).__name__}: {exc}")
            failed += 1
            # If the server itself went away, don't burn through every remaining
            # page as a failure: wait for it to come back, or stop.
            if not wait_for_server():
                print("TongGuOCR server is down; stopping. Re-run once it is back up to resume.")
                break
            continue

        # Commit each page so the app sees progress and a stop loses nothing.
        db.execute("UPDATE pages SET tongguocr_text = ? WHERE id = ?", (text, page_id))
        db.commit()
        done += 1

        elapsed = time.time() - started
        remaining = (total - index) * elapsed / index
        print(f"[{index}/{total}] page {page_id}: {len(text)} chars | ETA {remaining / 3600:.1f} h")

    db.close()
    print(f"Finished: {done} indexed, {failed} failed or skipped (re-run to retry those).")


if __name__ == "__main__":
    main()
