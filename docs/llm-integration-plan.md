# Plan: LLM integration via an MCP server

This is an implementation brief. Read `README.md` first for how the app works; this
document assumes it.

## Goal

Let an LLM client (Claude Code, Claude Desktop, or any other MCP client) work with the
genealogy library: list books, search the OCR text the way the web app does, read pages
(all three OCR engines' text, optionally the scan), and, in a later phase, submit new
sources to scrape.

## Architecture

```
LLM client ──stdio──> mcp_server.py ──read-only──> jiapu.db  (WAL, shared with the app)
                           │
                           └──HTTP (phase 3)──> FastAPI app on :8000  (owns all writes)
```

- **`mcp_server.py`** is a separate process launched by the MCP client over stdio. It is
  not part of the FastAPI app and does not import `main.py`, `tasks.py`, `ocr_engines.py`
  or anything under `Scraping/`.
- **Reads** go straight to `jiapu.db` through a read-only SQLite connection. The app runs
  SQLite in WAL mode, so a reader never blocks the OCR jobs that are writing.
- **Writes** (phase 3) go through new JSON endpoints on the running FastAPI app, never
  straight to the database. The app keeps sole control of background tasks, the
  per-site concurrency limit, status transitions and live progress.
- **`library.py`** is a new module of plain query functions (no FastAPI, no templates)
  that the MCP server calls. In phase 2 `main.py` switches over to it too, so search
  behaves identically in both places.

## Ground rules for the implementer

1. **Do not restart the uvicorn server while any source is processing.** Jobs live only
   in the server process; a restart marks every unfinished source `Failed`. Phases 2 and
   3 edit `main.py` and only take effect after a restart, so do them when nothing is
   `Pending`/`Scraping`/`Running OCR` (check with the phase 1 `list_sources` tool, status
   `active`). The launch config has no `--reload`, so editing files alone is harmless.
2. **Phase 1 adds files only.** Do not edit `main.py`, `tasks.py`, `database.py` or
   `ocr_engines.py` in phase 1.
3. **Never write to stdout in the MCP server.** stdio is the protocol channel. Log to
   stderr (the `logging` module, configured to stderr). This is also why the server must
   not import the OCR clients or scrapers: they `print` page text.
4. **Resolve paths from the project root, not the working directory.** MCP clients launch
   the server from arbitrary directories. Use
   `PROJECT_ROOT = Path(__file__).resolve().parent`; the DB is `PROJECT_ROOT / "jiapu.db"`
   and relative `pages.image_path` values resolve against `PROJECT_ROOT`. Stored paths may
   hold backslashes (written on Windows).
5. **Not exposed, in any phase:** `/clear-database`, source deletion, topic deletion,
   file upload.
6. Follow the existing code style: docstrings that explain *why*, small functions,
   batches of ≤900 bound parameters per SQL statement (`chunked()` in `main.py`).

---

## Phase 1: read-only MCP server (safe while sources are processing)

### 1.1 Dependency

Add the official Python MCP SDK (`mcp` on PyPI) to `requirements.txt`, pinned to the
current release at implementation time, under a new `# LLM integration (MCP server)`
heading. Use its `FastMCP` server class. Install it into `.venv`.

### 1.2 `library.py`

Pure data access, returning dicts/lists. Use the standard-library `sqlite3` module run via
`asyncio.to_thread`, or `aiosqlite` (already a dependency); either is fine, but open every
connection read-only and close it after each call:

```python
sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30)
```

with `row_factory = sqlite3.Row`. If the DB file does not exist, raise a clear error
("jiapu.db not found; start the web app once to create it").

Move copies of these from `main.py` (phase 2 deletes the originals):

- `OCR_ENGINES`, `chunked`, `load_confusion_map`, `get_fuzzy_options`,
  `expand_fuzzy_queries`, `search_variants`, plus the OpenCC converters and the
  `MAX_FUZZY_*` constants. `similarCharacters.json` must resolve from `PROJECT_ROOT`.
- `highlight_segments` / `SNIPPET_CONTEXT` (for snippets).

New functions (signatures are a guide, adjust as needed):

| Function | Returns |
|---|---|
| `list_topics()` | `[{id, name, source_count}]`, same query as `fetch_topics` |
| `list_sources(topic_id=None, status="", category="", text="", sort="newest", limit=50, offset=0)` | `[{id, title, url, category, status, error, topic_id, topic_name, page_count, hunyuan_count, tongguocr_count, paddleocr_count, created_at}]`, same SQL as `fetch_sources` minus thumbnails and live progress (that lives in the app process) |
| `get_source(source_id)` | one row as above, or `None` |
| `search_pages(query, fuzzy=False, topic_id=None, category="", engine="", source_id=None)` | the ranked page list exactly as the `/search` route builds it: lift the SQL, per-engine match tracking and `sort_key` ranking from `main.py:search` verbatim, plus a `source_id` filter |
| `page_positions(source_ids)` | `{page_id: (page_number, page_total)}`. Page numbers are 1-based by `pages.id` order within a source, as in `add_page_details` |
| `get_page(page_id)` | `{id, source_id, source_title, image_path, texts: {engine_label: text}, page_number, page_total, prev_page_id, next_page_id}` |
| `get_source_pages(source_id, start_page, count)` | the pages at those positions, each like `get_page` |
| `resolve_image_path(image_path)` | absolute `Path` on disk, or `None` if missing |

Keep `library.py` free of FastAPI imports so `main.py` can import it in phase 2.

### 1.3 `mcp_server.py`

A `FastMCP("genealogy-tools", instructions=...)` server run over stdio
(`if __name__ == "__main__": mcp.run()`).

**Server instructions** (passed to `instructions`, so every client sees them): a short
paragraph saying the library holds Chinese genealogy books (族譜) OCR'd into Traditional
Chinese; search automatically matches Simplified and Traditional forms; `fuzzy=true`
also matches characters OCR commonly confuses; names may appear as 名/字/號/諱 and
generation characters (字輩) are shared by a generation, so try variants; the three OCR
engines often disagree, so compare them and look at the scan when it matters; always
cite `page_id` and give the `viewer_url` when reporting a finding.

**Tools** (all read-only in phase 1; give each a clear docstring, since that is the
description the LLM sees):

| Tool | Parameters | Returns |
|---|---|---|
| `list_topics` | none | topics with source counts |
| `list_sources` | `topic_id?`, `status?` (`active`, `Completed`, `Failed`, or empty), `category?`, `text?`, `limit=50`, `offset=0` | sources as in `library.list_sources`, plus `viewer_url` |
| `get_source` | `source_id` | one source, including `error` for failed ones |
| `search` | `query`, `fuzzy=false`, `topic_id?`, `source_id?`, `category?`, `engine?`, `limit=20`, `offset=0` | `{query, variant_count, total_matches, source_count, results: [...]}`; each result is `{page_id, source_id, source_title, topic_name, page_number, page_total, engines_matched, snippet, viewer_url}` |
| `search_variants` | `query`, `fuzzy=false` | the variant strings a search would match (lets the LLM see what fuzzy expanded to) |
| `get_page` | `page_id`, `engines?` (list of labels, default all), `include_image=false` | page text from each engine, position, prev/next ids, `viewer_url`; with `include_image`, also the scan as MCP image content (see 1.4) |
| `get_source_pages` | `source_id`, `start_page=1`, `count=10` (max 25), `engine="HunyuanOCR"` | consecutive pages' text from one engine, for reading a stretch of a book |

Details:

- **Snippets are plain text.** Use `highlight_segments(..., snippet=True)` on the best
  matching engine's text and join the parts, wrapping hits in 【】.
- **Token budget.** Clamp `limit` (max 100) and `count` (max 25). Truncate any single
  engine text over 8,000 characters and say so in a `truncated` field. Always return
  `total_matches` so the LLM knows to page with `offset`.
- **`viewer_url`**: `{APP_URL}/viewer/{source_id}?focus_image_id={page_id}` (add
  `&q=...` for search hits), where `APP_URL` comes from env `GENEALOGY_APP_URL`, default
  `http://127.0.0.1:8000`. The user can click it to see the page in the app.
- **Errors**: unknown ids and an empty query return a clear tool error message, not a
  stack trace.

### 1.4 Images and privacy

The README promises that page images never leave the machine. A cloud-hosted LLM breaks
that promise the moment a scan (or even OCR text) is returned to it, so:

- Images are off unless env `GENEALOGY_MCP_ALLOW_IMAGES=1` is set. With it unset,
  `get_page(include_image=true)` returns the text plus a note that images are disabled.
- When enabled, load the file with Pillow, convert to RGB, downscale so the long side is
  at most 1568 px, encode as JPEG (quality ~85), and return it with the SDK's `Image`
  helper alongside the text.
- Document in the README section (1.6) that text is sent to whatever model the client
  uses, and that images are opt-in.

### 1.5 Client registration

Add `.mcp.json` at the project root so Claude Code picks the server up in this repo:

```json
{
  "mcpServers": {
    "genealogy-tools": {
      "command": ".venv/Scripts/python.exe",
      "args": ["mcp_server.py"]
    }
  }
}
```

(Relative paths in `.mcp.json` resolve against the project root; verify on this machine
and switch to absolute paths if they don't.) The README section should also show the
equivalent Claude Desktop config with absolute paths.

### 1.6 Tests and docs

- `tests/test_library.py` (pytest): build a temporary SQLite DB with the same schema
  (call `database.init_db` against a temp path, or copy its DDL), insert a few topics,
  sources and pages with Traditional text, and point `library` at it (make the DB path
  overridable, e.g. via env `GENEALOGY_DB_PATH` or a module variable). Cover: Simplified
  query matches Traditional text; fuzzy widens matches; topic/source/category/engine
  filters; ranking (exact match first); page numbering and prev/next; read-only
  connection rejects a write. Add `pytest` to requirements under a dev heading.
- Manual check with the MCP Inspector (`npx @modelcontextprotocol/inspector
  .venv/Scripts/python.exe mcp_server.py`) against the real `jiapu.db` while the app is
  running and a source is processing: tools respond, and the job keeps going.
- Add an "LLM integration (MCP)" section to `README.md`: what it is, setup, the tool
  list, the privacy note, and the env vars (`GENEALOGY_APP_URL`,
  `GENEALOGY_MCP_ALLOW_IMAGES`, `GENEALOGY_DB_PATH`).

**Phase 1 is done when:** the server starts from any working directory; every tool
works against the live DB without disturbing a running scrape; `search` returns the same
pages, in the same order, as the web app's `/search` for the same query and filters
(spot-check a few, including a fuzzy one); tests pass; `main.py`, `tasks.py`,
`database.py` are unchanged.

---

## Phase 2: one search implementation (needs a restart; do it when idle)

- In `main.py`, delete the functions copied into `library.py` and import them from
  there. Rewrite the `/search` route to call `library.search_pages` for the rows and keep
  only the template rendering and `add_page_details` in the route.
- Keep `library.py`'s read-only connection for the MCP server, but let `main.py` keep
  using its own `get_db()`; the simplest split is for `search_pages` and friends to take
  an open connection, with a read-only helper for the MCP side.
- Re-run the phase 1 tests, and check the web search page by hand (normal and fuzzy,
  with a topic filter) to confirm nothing changed.

**Done when:** there is one copy of the variant expansion and the search SQL, and the
web UI behaves exactly as before.

---

## Phase 3: write tools through the app (needs a restart; do it when idle)

### 3.1 JSON endpoints in `main.py`

The existing write routes return HTML fragments for HTMX, which an MCP tool can't use
(`/add-source` doesn't even return the new id). Add JSON siblings that reuse the same
logic (factor the shared part into helpers so the HTML and JSON routes can't drift):

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/api/sources` | `{url, topic_id?}` | `{id, category, status}`; rejects `Unknown` category with 400 |
| `GET` | `/api/sources/{id}` | | the source row plus live progress from `tasks.progress` (downloaded / total / OCR done) |
| `POST` | `/api/sources/{id}/retry` | | `{id, status}`; same rules as the HTML retry (only `Failed`) |
| `POST` | `/api/sources/{id}/topic` | `{topic_id \| null}` | `{id, topic_id}` |
| `POST` | `/api/topics` | `{name}` | `{id, name}`; returns the existing topic on a case-insensitive name match, as the HTML route does |

These endpoints have no authentication, same as the rest of the app. The app binds to
127.0.0.1 by default; keep it that way and say so in the README.

### 3.2 MCP write tools

Call the endpoints over HTTP at `GENEALOGY_APP_URL` (use `requests` in
`asyncio.to_thread`, already a dependency). If the app isn't reachable, return a clear
"the Genealogy Tools web app isn't running at …" error.

| Tool | Calls |
|---|---|
| `add_source(url, topic_id?)` | `POST /api/sources`. Docstring must say it returns immediately and that scraping plus OCR takes minutes (about 4 extra minutes when the OCR engines are cold), so check back with `source_status` |
| `source_status(source_id)` | `GET /api/sources/{id}` |
| `retry_source(source_id)` | `POST /api/sources/{id}/retry` |
| `create_topic(name)` | `POST /api/topics` |
| `set_source_topic(source_id, topic_id?)` | `POST /api/sources/{id}/topic` |

**Done when:** an LLM can add a FamilySearch/MyChinaRoots/ZtZupu link, watch it progress,
and file it under a topic, and the web UI shows the same source moving through the same
statuses as one added by hand. Tests for the JSON endpoints use FastAPI's `TestClient`
with background tasks stubbed out.

---

## Phase 4 (later, design only; do not build yet)

Ideas that build on the tools above. Each needs its own design pass:

- **OCR adjudication:** an LLM compares the three engines' text with the scan and writes a
  corrected reading into a new `pages.llm_text` column, added to `OCR_ENGINES` as a
  fourth engine so search and the viewer pick it up. Never overwrite engine output.
- **Structured extraction:** a `people` table (name, courtesy/art names, generation,
  father, spouses, children, dates, places, `page_id` evidence) filled by an LLM
  reading pages, which enables family-tree views and GEDCOM export.
- **Cross-book linking:** suggest when the same ancestor appears in two books.
- **In-app chat panel:** an "Ask the books" sidebar in the web UI driving the same
  `library.py` functions with an LLM tool-use loop.
- **Local model option:** the vLLM setup could host a local LLM so nothing leaves the
  machine, but it competes with the OCR engines (~13 GB of GPU 0). It would need to
  share `ocr_engines.py`'s start-on-demand/idle-stop scheme.
