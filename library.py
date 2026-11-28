"""Queries shared by web and MCP clients."""
import itertools
import json
import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import aiosqlite
import opencc

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / os.getenv("GENEALOGY_DB_PATH", "jiapu.db")


@asynccontextmanager
async def reader():
    path = DB_PATH.resolve()
    if not path.is_file():
        raise FileNotFoundError("jiapu.db not found; start the web app once to create it")
    async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        yield db


# Initialize opencc converters
s2t = opencc.OpenCC('s2t')
t2s = opencc.OpenCC('t2s')

SIMILAR_CHARACTERS_PATH = PROJECT_ROOT / "similarCharacters.json"
MAX_FUZZY_OPTIONS_PER_CHAR = 6
MAX_FUZZY_QUERIES = 500

OCR_ENGINES = (
    ("HunyuanOCR", "ocr_text"),
    ("TongGuOCR", "tongguocr_text"),
    ("PaddleOCR-VL", "paddleocr_text"),
)


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

def search_variants(query: str, fuzzy: bool) -> list[str]:
    """Every string a search for `query` matches: Simplified and Traditional
    forms, plus OCR look-alike variants when fuzzy. The viewer uses the same
    list to highlight a hit, so it finds exactly what the search found."""
    variants = expand_fuzzy_queries(query) if fuzzy else [query, t2s.convert(query), s2t.convert(query)]
    return list(dict.fromkeys(variants))


async def list_topics(db):
    """Topics with a source count, newest activity first."""
    cursor = await db.execute("""
        SELECT t.id, t.name, t.created_at, COUNT(s.id) AS source_count
        FROM topics t
        LEFT JOIN sources s ON s.topic_id = t.id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE ASC
    """)
    return [dict(row) for row in await cursor.fetchall()]


SOURCE_SORTS = {
    "newest": "s.created_at DESC",
    "oldest": "s.created_at ASC",
    "title": "COALESCE(s.title, s.url) COLLATE NOCASE ASC",
    "pages": "page_count DESC",
}


async def list_sources(
    db,
    topic_id: int | None = None,
    status: str = "",
    category: str = "",
    text: str = "",
    sort: str = "newest",
    source_id=None, limit=None, offset=0,
) -> list[dict]:
    """Sources with their topic, page counts per OCR engine for each book.

    status is "active" (anything still running), "Completed", "Failed" or ""
    for all; text matches the title or URL.
    """
    query = """
        SELECT s.*, t.name AS topic_name,
               COUNT(p.id) AS page_count,
               COUNT(p.ocr_text) AS hunyuan_count,
               COUNT(p.tongguocr_text) AS tongguocr_count,
               COUNT(p.paddleocr_text) AS paddleocr_count,
               MIN(p.id) AS first_page_id
        FROM sources s
        LEFT JOIN topics t ON s.topic_id = t.id
        LEFT JOIN pages p ON p.source_id = s.id
    """
    where, params = [], []
    if topic_id is not None:
        where.append("s.topic_id = ?")
        params.append(topic_id)
    if status == "active":
        where.append("s.status NOT IN ('Completed', 'Failed')")
    elif status:
        where.append("s.status = ?")
        params.append(status)
    if category:
        where.append("s.category = ?")
        params.append(category)
    if text.strip():
        where.append("(s.title LIKE ? OR s.url LIKE ?)")
        params += [f"%{text.strip()}%"] * 2
    if source_id is not None:
        where.append("s.id = ?")
        params.append(source_id)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " GROUP BY s.id ORDER BY " + SOURCE_SORTS.get(sort, SOURCE_SORTS["newest"])

    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend((limit, offset))
    cursor = await db.execute(query, params)
    sources = [dict(row) for row in await cursor.fetchall()]

    return sources


SNIPPET_CONTEXT = 70


def highlight_segments(text: str, pattern: "re.Pattern | None", snippet: bool) -> list[tuple[str, bool]]:
    """Split text into (piece, is_hit) parts for the template to mark up.

    With snippet, only the text around the first hit is kept, with ellipses.
    Returning parts rather than HTML leaves escaping to the template.
    """
    text = text or ""
    matches = list(pattern.finditer(text)) if pattern else []
    start, end = 0, len(text)
    if snippet and matches:
        start = max(0, matches[0].start() - SNIPPET_CONTEXT)
        end = min(len(text), matches[0].end() + SNIPPET_CONTEXT * 2)
    elif snippet:
        end = min(len(text), SNIPPET_CONTEXT * 3)

    parts, position = [], start
    if start > 0:
        parts.append(("…", False))
    for match in matches:
        if match.end() <= start or match.start() >= end:
            continue
        hit_start, hit_end = max(match.start(), start), min(match.end(), end)
        if hit_start > position:
            parts.append((text[position:hit_start], False))
        parts.append((text[hit_start:hit_end], True))
        position = hit_end
    if position < end:
        parts.append((text[position:end], False))
    if end < len(text):
        parts.append(("…", False))
    return parts


async def search_pages(db, query, fuzzy=False, topic_id=None, category="", engine="", source_id=None):
    engines = [e for e in OCR_ENGINES if e[0] == engine] or list(OCR_ENGINES)

    search_queries = search_variants(query, fuzzy)

    # SQLite's default limit is 999 bound params; stay comfortably under that per statement.
    # Each variant binds once per OCR engine; the topic and site filters take two more.
    BATCH_SIZE = (900 - 3) // len(engines)

    rows_by_id = {}
    match_counts = {}

    for batch in chunked(search_queries, BATCH_SIZE):
        # A page matches if any variant appears in any engine's text.
        one_variant = "(" + " OR ".join(f"p.{column} LIKE ?" for _, column in engines) + ")"
        like_clauses = " OR ".join([one_variant] * len(batch))
        params = [f"%{q}%" for q in batch for _ in engines]

        topic_clause = ""
        if topic_id is not None:
            topic_clause = " AND s.topic_id = ?"
            params.append(topic_id)
        if category:
            topic_clause += " AND s.category = ?"
            params.append(category)

        if source_id is not None:
            topic_clause += " AND s.id = ?"
            params.append(source_id)

        cursor = await db.execute(f"""
            SELECT p.id, p.source_id, p.ocr_text, p.tongguocr_text, p.paddleocr_text, p.image_path,
                   s.url, s.category, s.title AS source_title, t.name AS topic_name
            FROM pages p
            JOIN sources s ON p.source_id = s.id
            LEFT JOIN topics t ON s.topic_id = t.id
            WHERE ({like_clauses}){topic_clause}
        """, params)
        batch_rows = await cursor.fetchall()

        for row in batch_rows:
            page = rows_by_id.setdefault(row["id"], {**dict(row), "engines": set()})
            for q in batch:
                hit = False
                for engine_name, column in engines:
                    if q in (page[column] or ""):
                        page["engines"].add(engine_name)
                        hit = True
                if hit:
                    match_counts[page["id"]] = match_counts.get(page["id"], 0) + 1

    for page in rows_by_id.values():
        # Show the text from each engine that matched, in OCR_ENGINES order.
        page["matches"] = [
            {"engine": engine_name, "text": page[column]}
            for engine_name, column in engines
            if engine_name in page["engines"]
        ] or [
            # LIKE is case-insensitive for Latin letters but `in` is not, so a
            # page can match in SQL with no engine recorded; show it anyway.
            {"engine": engine_name, "text": page[column]}
            for engine_name, column in engines
            if page[column]
        ][:1]

    def sort_key(row):
        exact_match = any(query in (row[column] or "") for _, column in engines)
        return (not exact_match, -match_counts.get(row["id"], 0))

    results = sorted(rows_by_id.values(), key=sort_key)

    return results, search_queries

async def get_source(db, source_id):
    rows = await list_sources(db, source_id=source_id)
    return rows[0] if rows else None


async def page_positions(db, source_ids):
    positions = {}
    for batch in chunked(set(source_ids), 900):
        rows = await db.execute_fetchall(f"""
            SELECT id, row_number() OVER w, count(*) OVER (PARTITION BY source_id)
            FROM pages WHERE source_id IN ({','.join('?' * len(batch))})
            WINDOW w AS (PARTITION BY source_id ORDER BY id)
        """, batch)
        positions.update({page: (number, total) for page, number, total in rows})
    return positions


async def get_source_pages(db, source_id, start_page=1, count=10, *, page_id=None):
    # Window over IDs only; fetch potentially large transcriptions after pagination.
    condition = "n.id = ?" if page_id is not None else "n.page_number >= ?"
    rows = await db.execute_fetchall(f"""
        WITH numbered AS (
            SELECT id, row_number() OVER w AS page_number, count(*) OVER () AS page_total,
                   lag(id) OVER w AS prev_page_id, lead(id) OVER w AS next_page_id
            FROM pages WHERE source_id = ? WINDOW w AS (ORDER BY id)
        )
        SELECT n.*, p.source_id, p.image_path, s.title AS source_title,
               p.ocr_text, p.tongguocr_text, p.paddleocr_text
        FROM numbered n JOIN pages p ON p.id=n.id JOIN sources s ON s.id=p.source_id
        WHERE {condition} ORDER BY n.id LIMIT ?
    """, (source_id, page_id if page_id is not None else start_page, count))
    pages = [dict(row) for row in rows]
    for page in pages:
        page["texts"] = {label: page.pop(column) for label, column in OCR_ENGINES}
    return pages


async def get_page(db, page_id):
    rows = await db.execute_fetchall("SELECT source_id FROM pages WHERE id=?", (page_id,))
    if rows:
        pages = await get_source_pages(db, rows[0][0], page_id=page_id)
        return next(iter(pages), None)
    return None


def resolve_image_path(image_path):
    if image_path:
        path = PROJECT_ROOT / image_path.replace("\\", "/")
        if path.is_file():
            return path.resolve()
    return None
