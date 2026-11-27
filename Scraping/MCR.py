"""Download a MyChinaRoots book's page images.

MyChinaRoots' viewer asks its API for each page: clan-pedigree/<book id>/
gives the book's page count and source ID, and sources/<source id>/image/<n>/
gives a signed link to page n's full image. So instead of clicking through the
viewer, this signs in through the library login, opens the book's page once
(to pick up the API headers the site's own code sends), then asks the API for
each page and downloads it, a few pages at a time.
"""

from camoufox.async_api import AsyncCamoufox
import os
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.credentials import require
from Scraping.paths import MCR_DIR, book_dir
import asyncio
from urllib.parse import unquote

SITE = "https://mychinarootslibrary-com.rpa.sccl.org"
API = f"{SITE}/api/v1"
LOGIN_URL = "https://login.rpa.sccl.org/login?qurl=https://mychinarootslibrary.com%2f"
# The site's own code adds these to its API requests (an API key and the
# member's session); without them the API treats the account as a preview-only
# visitor and won't hand out page images.
API_HEADERS = ("authorization", "x-mcr-session-id", "x-referrer", "x-analytics-session-id")
# Pages downloaded at once. Pages are still reported in book order.
PAGE_CONCURRENCY = 3
# Page images can be several MB.
REQUEST_TIMEOUT_MS = 120000
# Waits before retrying a page that failed with an error or timeout.
RETRY_WAITS_S = (5, 15, 30, 60)


async def sign_in(page):
    await page.goto(LOGIN_URL)
    await page.fill("body > form:nth-child(3) > input:nth-child(4)", require("MCR_USERNAME"))
    await page.fill("body > form:nth-child(3) > input:nth-child(7)", require("MCR_PASSWORD"))
    await page.click("body > form:nth-child(3) > p:nth-child(9) > input:nth-child(1)")
    await page.wait_for_load_state()


async def open_book(page, book_id):
    """Open the book's page; return its title and the headers its API requests carry."""
    async with page.expect_request(lambda r: f"/api/v1/clan-pedigree/{book_id}" in r.url) as info:
        await page.goto(f"{SITE}/zupus/{book_id}")
    request_headers = await (await info.value).all_headers()
    title = await page.locator(".zupu-info > div:nth-child(1) > h1:nth-child(1)").inner_text()
    return title, {k: v for k, v in request_headers.items() if k in API_HEADERS}


async def download_page(api, headers, source_id, number, save_dir):
    """Download page `number` of the source and return where it was saved."""
    last_error = None
    for wait in (*RETRY_WAITS_S, None):
        try:
            response = await api.get(f"{API}/sources/{source_id}/image/{number}/",
                                     headers=headers, timeout=REQUEST_TIMEOUT_MS)
            if response.ok:
                image_url = (await response.json())["url"]
                image = await api.get(image_url, timeout=REQUEST_TIMEOUT_MS)
                if image.ok:
                    # Named as before: the file name in the image's URL.
                    path = os.path.join(save_dir, unquote(image_url.split("?")[0].split("/")[-1]))
                    with open(path, "wb") as f:
                        f.write(await image.body())
                    return path
                last_error = f"image HTTP {image.status}"
            else:
                last_error = f"HTTP {response.status} {(await response.text())[:100]}"
        except Exception as e:  # a timeout or dropped connection
            last_error = e
        if wait is not None:
            print(f"Page {number} failed ({last_error}); retrying in {wait}s")
            await asyncio.sleep(wait)
    raise RuntimeError(f"Could not download page {number}: {last_error}")


async def download_book(api, headers, source_id, count, save_dir, on_image=None):
    """Download pages 1..count, PAGE_CONCURRENCY at a time, reporting them in order."""
    slots = asyncio.Semaphore(PAGE_CONCURRENCY)

    async def fetch(number):
        async with slots:
            return await download_page(api, headers, source_id, number, save_dir)

    tasks = [asyncio.create_task(fetch(number)) for number in range(1, count + 1)]
    try:
        for task in tasks:
            path = await task
            if on_image:
                on_image(path)
    finally:
        # On a failure (or on_image stopping the scrape), drop the rest.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def mainAsync(ids, pathName, on_image=None, on_total=None):
    saved_dirs = []
    async with AsyncCamoufox() as browser:
        page = await browser.new_page()
        await sign_in(page)
        api = page.context.request
        for id in ids:
            title, headers = await open_book(page, id)
            response = await api.get(f"{API}/clan-pedigree/{id}/", headers=headers, timeout=REQUEST_TIMEOUT_MS)
            if not response.ok:
                raise RuntimeError(f"MyChinaRoots couldn't find book {id} (HTTP {response.status})")
            book = await response.json()
            if not book.get("user_has_full_access"):
                raise RuntimeError(
                    "This MyChinaRoots account only has the free preview of this book "
                    f"({book.get('free_preview_images_count')} pages), so it can't be downloaded"
                )
            count = book["images_count"]
            folder_name = SerializeFolderName(title)
            print(f"Scraping {folder_name}: {count} pages")
            fullSavePath = book_dir(pathName, folder_name)
            if on_total:
                on_total(count)
            await download_book(api, headers, book["source_object_id"], count, fullSavePath, on_image)
            print(f"Finished scraping {folder_name}")
            saved_dirs.append(fullSavePath)
    return saved_dirs

def main(ids, pathName = MCR_DIR, on_image=None, on_total=None):
    """Scrape each book id in ids; returns the directory saved for each.

    on_image(path), if given, is called as each page image is saved, in page
    order, and on_total(n) with a book's page count before its download starts.
    """
    return asyncio.run(mainAsync(ids, pathName, on_image, on_total))

if __name__ == '__main__':
    main(["3ff8274e-d4b7-4400-9fec-b742cabee362"])
