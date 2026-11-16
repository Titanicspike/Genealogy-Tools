"""Run TongGuOCR over every page that is missing TongGuOCR text or layout.

Fills pages.tongguocr_text (searchable text, alongside HunyuanOCR's
pages.ocr_text) and pages.tongguocr_layout (column boxes for the viewer).
Pages that already have text but no layout get the quick layout-only run
first; pages with no text then get the full run. Safe to stop and re-run: it
only picks up pages that are still NULL, so it resumes where it left off and
also catches pages whose TongGuOCR call failed during normal ingestion.

Needs the TongGuOCR server running in WSL (TongGuOCR/start_server.sh).

    python reindex_tongguocr.py            # all remaining pages
    python reindex_tongguocr.py --limit 20 # just the next 20
"""

import argparse
import os
import sqlite3
import sys
import time

import ocr_engines
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

    # Starts the engine if the app's idle monitor stopped it.
    ocr_engines.ensure_running("tongguocr")

    db = sqlite3.connect(DB_PATH, timeout=30.0)
    # The app's database may not have been opened since the columns were added.
    columns = {row[1] for row in db.execute("PRAGMA table_info(pages)")}
    for column in ("tongguocr_text", "tongguocr_layout"):
        if column not in columns:
            db.execute(f"ALTER TABLE pages ADD COLUMN {column} TEXT")
    db.commit()

    # Layout-only pages first: they take a few seconds each and give every
    # page that is already searchable its positions in the viewer. The slow
    # full runs for pages without TongGuOCR text follow.
    pending = db.execute("""
        SELECT id, image_path, tongguocr_text IS NULL AS needs_text
        FROM pages
        WHERE tongguocr_text IS NULL OR tongguocr_layout IS NULL
        ORDER BY needs_text ASC, id
    """).fetchall()
    if args.limit is not None:
        pending = pending[: args.limit]
    total = len(pending)
    full_total = sum(1 for row in pending if row[2])
    print(f"{total} pages to process: {full_total} need text and layout, {total - full_total} need layout only")

    done = failed = 0
    # The two kinds of run take very different times, so time them separately.
    elapsed = {True: 0.0, False: 0.0}
    count = {True: 0, False: 0}
    for index, (page_id, image_path, needs_text) in enumerate(pending, start=1):
        needs_text = bool(needs_text)
        if not os.path.isfile(image_path):
            print(f"[{index}/{total}] page {page_id}: image missing, skipped: {image_path}")
            failed += 1
            continue
        page_started = time.time()
        try:
            text, layout = TongGuOCR.recognize(image_path, with_text=needs_text)
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
        if needs_text:
            db.execute(
                "UPDATE pages SET tongguocr_text = ?, tongguocr_layout = ? WHERE id = ?",
                (text, layout, page_id),
            )
        else:
            db.execute("UPDATE pages SET tongguocr_layout = ? WHERE id = ?", (layout, page_id))
        db.commit()
        done += 1

        elapsed[needs_text] += time.time() - page_started
        count[needs_text] += 1
        left_full = sum(1 for row in pending[index:] if row[2])
        left_layout = (total - index) - left_full
        average = {kind: elapsed[kind] / count[kind] if count[kind] else None for kind in (True, False)}
        # Before any layout-only page has run, assume it costs a quarter of a full one.
        layout_guess = average[False] or (average[True] or 0) / 4
        remaining = left_full * (average[True] or layout_guess * 4) + left_layout * layout_guess
        kind = "text+layout" if needs_text else "layout"
        print(f"[{index}/{total}] page {page_id}: {kind} | ETA {remaining / 3600:.1f} h")

    db.close()
    print(f"Finished: {done} indexed, {failed} failed or skipped (re-run to retry those).")


if __name__ == "__main__":
    main()
