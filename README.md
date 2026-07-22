# Genealogy Tools

A local web app for collecting Chinese genealogy books (族譜 / *jiapu*), running OCR
over their pages, and searching the extracted text across every book you have gathered.

Sources come from two places: scraped from FamilySearch, MyChinaRoots, and ZtZupu by
submitting a link, or uploaded directly as photos and PDFs. Either way the pages land in
the same pipeline, get transcribed by a local vision OCR model, and become searchable.
Search understands that Simplified and Traditional forms of a character are the same
query, and can optionally widen further to cover characters the OCR model tends to
confuse.

Everything runs on your own machine. The OCR model is served locally; no page images
leave the host.

---

## Architecture

**Stack:** FastAPI + Jinja2 templates, with [HTMX](https://htmx.org/) driving the
frontend. Most endpoints return HTML fragments rather than JSON, which the page swaps in
place — there is no separate frontend build. Tailwind and HTMX load from CDNs, so the UI
needs a network connection even though the data does not.

**Storage:** SQLite (`jiapu.db`) accessed asynchronously through `aiosqlite`, in WAL mode.
Connections are opened per operation and closed immediately, which keeps the long-running
OCR background tasks from holding write locks.

**Concurrency:** Scraping and OCR are slow and synchronous, so they run as FastAPI
background tasks and push their blocking work onto threads via `asyncio.to_thread`. The
web server stays responsive, and the UI polls `/sources` every 5 seconds to show progress.

### Data model (`database.py`)

| Table | Purpose |
|---|---|
| `topics` | Optional grouping. A name, unique case-insensitively. |
| `sources` | One book or upload batch: `url`, `category`, processing `status`, a nullable `topic_id`, and a `title`. |
| `pages` | One image per row: `source_id`, `image_path`, and the extracted `ocr_text`. |

`init_db()` runs on startup and is idempotent. It also carries small migrations that add
`sources.topic_id` and `sources.title` to databases created before those columns existed,
so an older `jiapu.db` keeps working without manual intervention.

**Titles.** The scrapers save each book into a directory named after the book, so
`process_jiapu_source` records that directory name as the source's `title` — which is what
the UI shows, with the URL demoted to secondary text. A one-time backfill in `init_db()`
derives titles for older scraped sources from the parent directory of their page images.
Sources with no pages (a failed scrape) have no title and fall back to displaying the URL.
Uploads are excluded from the backfill, since their parent directory is just `source_<id>`;
they continue to show the uploaded filename.

A source's `status` walks through `Pending` → `Scraping` → `Running OCR` → `Completed`,
or lands on `Failed`. Uploads use `Saving upload` → `Preparing files` → `Running OCR`.
The status column is how the UI reports progress, since the actual work happens out of band.

### Pipelines (`tasks.py`)

**Scraped sources.** `POST /add-source` categorizes the URL by domain, inserts a `Pending`
row, and returns immediately. In the background, `process_jiapu_source` dispatches to the
matching scraper in `Scraping/` — FamilySearch and MyChinaRoots drive a real browser via
[Camoufox](https://github.com/daijro/camoufox), ZtZupu is plain HTTP — and each returns a
directory of downloaded page images. Those are then OCR'd one at a time.

**Uploads.** `POST /upload-ocr` accepts JPG, PNG, WebP, TIFF, and PDF, capped at 50 MB per
file, and streams them to `uploads/source_<id>/`. PDFs are rasterized page by page at 2×
scale with PyMuPDF; images are used as-is. The resulting pages go through the same OCR
call. If saving fails partway, the directory and the `sources` row are both rolled back.

### OCR (`HunyuanOCR/`)

`docker-compose.yml` runs [vLLM](https://github.com/vllm-project/vllm) serving
`tencent/HunyuanOCR`, exposing an OpenAI-compatible API on port **9000**.
`HunyuanOCR.py` is the client: it base64-encodes a page and asks the model, in Chinese, to
transcribe vertical, horizontal, printed, and handwritten text in reading order as
Traditional Chinese, preserving line breaks and marking uncertain characters with `[ ]`.

Genealogy scans are frequently too large for the model's encoder cache. Rather than
failing, the client parses the token-overflow error, computes the scale factor that would
fit, shrinks the image, and retries — up to 5 attempts.

### Search (`main.py`)

Every query runs in at least three forms: as typed, Simplified (`t2s`), and Traditional
(`s2t`), courtesy of OpenCC. Matching is SQL `LIKE` against `pages.ocr_text`.

Ticking **fuzzy** expands much further. `similarCharacters.json` maps each character to
the characters OCR most often mistakes it for, ranked by observed frequency. Each character
in the query expands to up to 6 candidates, and the cartesian product becomes a variant
list capped at 500 queries. Because SQLite allows only 999 bound parameters per statement,
variants are executed in batches of 900 and the results merged.

Results are ranked by exact-match-first, then by how many variants hit the page. Selecting
a topic scopes the entire search to that topic's sources.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Main page |
| `POST` | `/add-source` | Submit a URL to scrape (optional `topic_id`) |
| `POST` | `/upload-ocr` | Upload images/PDFs (optional `topic_id`) |
| `GET` | `/sources` | Source list fragment, filterable by `topic_id` |
| `GET` | `/sources/{id}/text` | All OCR text for a source; `?download=true` to save |
| `POST` | `/sources/{id}/topic` | Move a source into or out of a topic |
| `GET`/`POST` | `/topics` | List or create topics |
| `POST` | `/topics/{id}/delete` | Delete a topic; its sources survive, unassigned |
| `GET` | `/search` | Search, with optional `fuzzy` and `topic_id` |
| `GET` | `/viewer/{id}` | Page-image viewer; `?focus_image_id=` jumps to a page |
| `GET` | `/api/images/{id}` | Paginated image grid fragment (12 per page) |
| `POST` | `/clear-database` | Wipe all pages, sources, and topics |

Scraped and uploaded images are served through static mounts (`/images`, `/camoufox`,
`/hocr`, `/uploads`); `get_image_url()` maps an on-disk path back to whichever mount covers it.

---

## Setup

### Prerequisites

- **Python 3.12** (developed on 3.12.10).
- **NVIDIA GPU + Docker** with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
  for the OCR server. The compose file requests all GPUs and 85% of VRAM. You can run the
  web app, scrapers, and search without this, but OCR of new pages will fail.

**Platform support.** The app, scrapers, and search run on Windows, Linux, and macOS.
The OCR server is the one piece that does not travel: it needs an NVIDIA GPU, so it runs
on Windows and Linux but not on Apple Silicon. On a Mac you can still scrape, upload,
browse, and search — or point `base_url` in `HunyuanOCR/HunyuanOCR.py` at a machine that
does have a GPU.

Run commands from the project root. `main.py` mounts its static image directories by
relative path, so the working directory matters.

### 1. Install Python dependencies

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate            # Windows (PowerShell / cmd)
source .venv/bin/activate         # macOS / Linux
```

```bash
pip install -r requirements.txt
```

### 2. Set up credentials

The FamilySearch and MyChinaRoots scrapers sign in, so they need accounts. Copy the
template and fill in your own values:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

| Key | Used by | Notes |
|---|---|---|
| `FAMILYSEARCH_USERNAME` / `FAMILYSEARCH_PASSWORD` | `Scraping/FamilySearch.py` | Your FamilySearch account |
| `MCR_USERNAME` / `MCR_PASSWORD` | `Scraping/MCR.py` | SCCL library card number and PIN |

`.env` is gitignored. A real environment variable takes precedence over the file, so you
can override a single value from the shell without editing it.

These are only read when a scraper actually runs. Uploading files, searching, and ZtZupu
scraping all work with no `.env` at all — a missing credential raises a clear error at
scrape time rather than blocking startup.

### 3. Download the Camoufox browser

The FamilySearch and MyChinaRoots scrapers drive a real browser, which is a separate
download from the pip package:

```bash
python -m camoufox fetch
```

Skip this if you only plan to upload files or scrape ZtZupu.

### 4. Start the OCR server

```bash
cd HunyuanOCR
docker compose up -d
```

The first run pulls the vLLM image and downloads model weights into
`~/.cache/huggingface`, which takes a while. Wait until it answers before submitting
anything for OCR:

```bash
curl http://127.0.0.1:9000/v1/models
```

The client in `HunyuanOCR.py` targets `http://127.0.0.1:9000/v1` — edit `base_url` there
if you serve the model elsewhere.

### 5. (Optional) Add the fuzzy-search confusion map

Fuzzy search reads `similarCharacters.json` from the project root, a ~13 MB map of
OCR character confusions. It is not in the repository. Without it the app runs fine and
the fuzzy checkbox simply falls back to Simplified/Traditional matching only —
`load_confusion_map()` returns empty rather than raising.

### 6. Run

```bash
python -m uvicorn main:app --reload
```

Then open <http://127.0.0.1:8000>. The database and `uploads/` directory are created
automatically on first start.
