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
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                image_path TEXT NOT NULL,
                ocr_text TEXT,
                FOREIGN KEY(source_id) REFERENCES sources(id)
            )
        """)
        await db.commit()

async def get_db():
    db = await aiosqlite.connect(DB_PATH, timeout=30.0)
    db.row_factory = aiosqlite.Row
    return db

async def clear_db():
    """Clear all data from the database tables"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pages")
        await db.execute("DELETE FROM sources")
        await db.commit()
