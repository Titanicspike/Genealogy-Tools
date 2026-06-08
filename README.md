# Genealogy Tools

This document outlines the architecture and data flow of the Genealogy Tools application. It serves as a guide for future development and AI sessions.

## Architecture Overview

The application is built using **FastAPI** as the web framework, serving HTML pages via **Jinja2Templates**. It uses an **SQLite** database (accessed asynchronously via `aiosqlite`) to store genealogical sources and the OCR text extracted from their pages. Background tasks are used to handle the heavy lifting of web scraping and OCR processing without blocking the main web server.

### 1. Database Structure (`database.py`)
The database (`jiapu.db`) is initialized on application startup and consists of two main tables:
*   **`sources`**: Tracks the URLs submitted by the user. It records the `url`, the categorized source `category` (e.g., FamilySearch), and the current processing `status` (Pending, Scraping, Running OCR, Completed, or Failed).
*   **`pages`**: Represents individual pages scraped from a source. It references a `source_id` and contains the `image_path` of the page and the extracted `ocr_text`.

### 2. The Main Application Flow (`main.py` & `tasks.py`)

#### Adding a Source
1.  **Submission**: A user submits a new URL via a POST request to `/add-source`.
2.  **Categorization**: The URL is parsed to determine its origin (FamilySearch, MyChinaRoots, ZtZupu, or Unknown).
3.  **Database Entry**: A new record is created in the `sources` table with a status of "Pending".
4.  **Background Processing**: The endpoint immediately returns an updated HTML list of sources to the user and offloads the actual processing to a background task (`process_jiapu_source` from `tasks.py`).

#### The Background Processing Pipeline
The background task (`process_jiapu_source`) orchestrates the pipeline and updates the database status at each step:
1.  **Scraping Phase**: The status is set to "Scraping". The scraper scripts (located in the `CamoufoxScraping` directory) are synchronous, so the application runs them in a separate thread using `asyncio.to_thread` to prevent freezing the asynchronous FastAPI event loop.
2.  **OCR Phase**: Once scraping is complete, the status updates to "Running OCR". Currently, this calls a placeholder function (`perform_ocr_placeholder`) which simulates OCR processing by sleeping for 2 seconds and inserting dummy Chinese text into the `pages` table.
3.  **Completion**: Finally, the source's status is marked as "Completed" (or "Failed" if an exception was caught during the pipeline).

#### Searching the Records
1.  **Query Translation**: When a user searches via the `/search` endpoint, the query is passed through the `opencc` library to generate both **Simplified** (`t2s`) and **Traditional** (`s2t`) Chinese versions of the text.
2.  **Database Query**: The application executes a SQL `LIKE` query against the `ocr_text` column in the `pages` table, looking for matches for either the simplified or traditional versions of the query. 
3.  **Rendering**: The matching pages (along with their associated source URLs) are returned and rendered into the `search_results.html` partial template. 

### Current State & Next Steps
The foundational architecture is solid, utilizing a responsive frontend setup with partials, asynchronous database operations, and proper background task delegation. 

The primary next step for this codebase is replacing the `perform_ocr_placeholder` in `tasks.py` with actual OCR integration (e.g., passing the images scraped by the Camoufox scripts into an OCR engine like Tesseract or EasyOCR) to populate the database with real text.
