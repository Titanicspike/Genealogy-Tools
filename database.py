import aiosqlite
import os

DB_PATH = "jiapu.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
        # Enable WAL mode for better concurrency
        await db.execute("PRAGMA journal_mode=WAL")
        # Set synchronous mode to NORMAL for faster writes
        await db.execute("PRAGMA synchronous=NORMAL")
        # Increase the cache size for better performance
        await db.execute("PRAGMA cache_size=10000")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                title TEXT,
                error TEXT,
                topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrate pre-existing databases created before these columns existed
        cursor = await db.execute("PRAGMA table_info(sources)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "topic_id" not in columns:
            await db.execute(
                "ALTER TABLE sources ADD COLUMN topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL"
            )
        if "title" not in columns:
            await db.execute("ALTER TABLE sources ADD COLUMN title TEXT")
        if "error" not in columns:
            await db.execute("ALTER TABLE sources ADD COLUMN error TEXT")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sources_topic_id ON sources(topic_id)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                image_path TEXT NOT NULL,
                ocr_text TEXT,
                FOREIGN KEY(source_id) REFERENCES sources(id)
            )
        """)
        await backfill_source_titles(db)
        await db.commit()


def title_from_image_path(image_path: str) -> str:
    """The scrapers save each book into a directory named after the book, so the
    parent directory of any page image is that book's title."""
    if not image_path:
        return ""

    # Normalize first: rows written on Windows hold backslashes, which
    # os.path.dirname would not treat as separators on Linux or macOS.
    normalized = image_path.replace("\\", "/")
    return os.path.basename(os.path.dirname(normalized))


async def backfill_source_titles(db):
    """Fill in titles for scraped sources that predate the title column.

    Uploads are skipped: their images live in uploads/source_<id>/, so the
    parent directory is an id rather than anything readable. The source list
    already shows the uploaded filename for those.
    """
    cursor = await db.execute("""
        SELECT s.id, MIN(p.image_path)
        FROM sources s
        JOIN pages p ON p.source_id = s.id
        WHERE s.title IS NULL AND s.category != 'Upload'
        GROUP BY s.id
    """)

    for source_id, sample_path in await cursor.fetchall():
        title = title_from_image_path(sample_path)
        if title:
            await db.execute(
                "UPDATE sources SET title = ? WHERE id = ?", (title, source_id)
            )

async def get_db():
    db = await aiosqlite.connect(DB_PATH, timeout=30.0)
    db.row_factory = aiosqlite.Row
    return db

async def clear_db():
    """Clear all data from the database tables"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pages")
        await db.execute("DELETE FROM sources")
        await db.execute("DELETE FROM topics")
        await db.commit()
