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
Only one source per website runs at a time (`MAX_CONCURRENT_SOURCES_PER_SITE`, default 1),
so a FamilySearch book and a MyChinaRoots book run side by side, while a second FamilySearch
book waits as `Pending`. Uploads count as their own site. These tasks live only in the
server process, so on startup any source left unfinished by a previous run is marked
`Failed` and can be retried.

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
directory of downloaded page images. Those are then OCR'd one page at a time, with the
three OCR engines reading each page in parallel.

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

### OCR (`PaddleOCRVL/`)

A third engine, stored in `pages.paddleocr_text`. `docker-compose.yml` runs the official
two-container PaddleOCR-VL-1.6 deployment, using the images built for Blackwell (sm120)
GPUs: a vLLM server for the 0.9B PaddleOCR-VL recognizer, and the document pipeline, which
finds each text block with PP-DocLayoutV3, reads it with the recognizer, and returns the
blocks in reading order. The pipeline serves `POST /layout-parsing` on port **9200**.
`PaddleOCRVL.py` is the client; it joins the text blocks into the page text.

Every new page is sent to all three engines at once. A TongGuOCR or PaddleOCR-VL failure
leaves that engine's column NULL instead of failing the source; `reindex_tongguocr.py` and
`reindex_paddleocr.py` fill in any NULL pages later and can be stopped and re-run.

### Freeing the GPU when idle (`ocr_engines.py`)

Running, the three engines hold about 13 GB of GPU 0 whether or not they're reading
anything. So the app starts each engine only when a page needs it (`docker compose up -d`
for the two container sets, `TongGuOCR/start_server.sh` through WSL for TongGuOCR) and
stops it again after `OCR_IDLE_MINUTES` without use (default 10; `0` never stops them).
Stopped, they hold nothing.

The catch is the cold start: about 4 minutes before the first page after an idle period
(HunyuanOCR ~2.5 min, PaddleOCR-VL and TongGuOCR ~50 s each, started one after another
so each can size its memory). Pages after that run at full speed. The reindex scripts
start the engine they need too; while the app is running, its idle monitor sees their
use and stops the engine once they finish.

### Search (`main.py`)

Every query runs in at least three forms: as typed, Simplified (`t2s`), and Traditional
(`s2t`), courtesy of OpenCC. Matching is SQL `LIKE` against every engine's text (`OCR_ENGINES` in `main.py`), and
result cards say which engine(s) matched.

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
| `GET` | `/viewer/{id}` | Page reader; `?focus_image_id=` opens a page, `?q=` (and `fuzzy`) highlights a search on it |
| `GET` | `/api/pages/{id}` | One page as JSON: image URL, every engine's OCR text, TongGuOCR column layout |
| `GET` | `/api/variants` | The variant strings a search for `?q=` matches (used by the reader's find box) |
| `POST` | `/clear-database` | Wipe all pages, sources, and topics |

Scraped and uploaded images are served through static mounts (`/images`, `/camoufox`,
`/hocr`, `/uploads`); `get_image_url()` maps an on-disk path back to whichever mount covers it.

**Finding a match on the page.** Neither engine's page text says where each character
sits. TongGuOCR's layout detector does find every text column, so for each page the
TongGuOCR server also reads each detected column on its own and stores the boxes with
their text in `pages.tongguocr_layout`. The reader looks for a search in those
per-column texts and places it within its column by where it falls in that column's
text. If no column has an exact match, it falls back to the closest one (drawn dashed).
Where one box spans several printed columns, the match can only be pinned to the whole
box. This per-column reading is just for locating text; search uses the
normal page text.

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

Start PaddleOCR-VL the same way. The images bundle their models, so there is nothing
else to download:

```bash
cd PaddleOCRVL
docker compose up -d
```

```bash
curl http://127.0.0.1:9200/health
```

The client targets `http://127.0.0.1:9200`; set `PADDLEOCR_URL` to point it elsewhere.

You only need to bring each engine up once by hand, to download and check it. After that
you can stop them (`docker compose stop`); the app starts and stops all three on its own
(see *Freeing the GPU when idle*). Docker Desktop must be running, and TongGuOCR must be
installed in the WSL distribution named by `TONGGUOCR_WSL_DISTRO` (default `Ubuntu`).

### 5. (Optional) Add the fuzzy-search confusion map

Fuzzy search reads `similarCharacters.json` from the project root, a ~13 MB map of
OCR character confusions. It is not in the repository. Without it the app runs fine and
the fuzzy checkbox simply falls back to Simplified/Traditional matching only —
`load_confusion_map()` returns empty rather than raising.

### 6. Run

```bash
python -m uvicorn main:app --port 9999 --reload
```

Then open <http://127.0.0.1:9999>. The database and `uploads/` directory are created
automatically on first start.
