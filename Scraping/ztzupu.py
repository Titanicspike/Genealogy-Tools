import aiohttp
import requests
import os
import sys
import asyncio
import random
from bs4 import BeautifulSoup
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.paths import ZTZUPU_DIR, book_dir

# aiohttp misbehaves on Windows under the default Proactor loop (sockets can
# raise "Event loop is closed" during teardown), so run the downloads on a
# Selector loop. Pass it to asyncio.run rather than setting a global policy:
# other scrapers (Camoufox) run at the same time in other threads and need
# Windows' default Proactor loop to spawn their browser.
# Elsewhere the default loop is already selector-based.
LOOP_FACTORY = asyncio.SelectorEventLoop if sys.platform == "win32" else None
# Be gentle with the site: a few downloads at a time, backing off on errors.
MAX_CONCURRENT_DOWNLOADS = 4
MAX_ATTEMPTS = 6
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0
# Statuses worth retrying; any other non-200 response is treated as permanent.
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)


class DownloadError(Exception):
    pass


def backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    """Exponential backoff with jitter, honouring a numeric Retry-After header."""
    if retry_after and retry_after.strip().isdigit():
        return min(float(retry_after), MAX_BACKOFF_SECONDS)
    delay = min(BASE_BACKOFF_SECONDS * 2 ** attempt, MAX_BACKOFF_SECONDS)
    return delay * random.uniform(0.5, 1.0)


async def getImage(session, slots, url, num, saveDir):
    """Download one page image, retrying transient failures with backoff.

    Raises DownloadError when the page can't be fetched, so the source fails
    instead of finishing with pages missing or error pages saved as images.
    """
    path = os.path.join(saveDir, f'{num}.{url.split(".")[-1]}')
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        retry_after = None
        try:
            # Hold a slot only while requesting, not while backing off, and
            # start the timeout only once the slot is ours.
            async with slots, session.get(url, timeout=REQUEST_TIMEOUT) as response:
                if response.status == 200:
                    # A 200 with an HTML body is the site's error/login page.
                    content_type = response.headers.get("Content-Type", "")
                    if content_type.startswith("text/html"):
                        raise DownloadError(f"{url} returned an HTML page, not an image")
                    body = await response.read()
                    with open(path, 'wb') as f:
                        f.write(body)
                    print("Saved image to: " + path)
                    return
                last_error = f"HTTP {response.status}"
                if response.status not in RETRYABLE_STATUSES:
                    raise DownloadError(f"{url} returned {last_error}")
                retry_after = response.headers.get("Retry-After")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = f"{type(e).__name__}: {e}"
        if attempt + 1 < MAX_ATTEMPTS:
            delay = backoff_seconds(attempt, retry_after)
            print(f"Attempt {attempt + 1} for {url} failed ({last_error}); retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
    raise DownloadError(f"{url} failed after {MAX_ATTEMPTS} attempts ({last_error})")

async def imageManager(imageUrls, saveDir):
    slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[getImage(session, slots, url, n, saveDir) for n, url in enumerate(imageUrls)],
            return_exceptions=True,
        )
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        raise DownloadError(
            f"{len(failures)} of {len(imageUrls)} pages failed to download; first: {failures[0]}"
        )

def main(zupuIds, savePath = ZTZUPU_DIR):
    """Scrape each book id in zupuIds; returns the directory saved for each."""
    saved_dirs = []
    for zupuId in zupuIds:
        url = f'http://www.ztzupu.com/ztzupu/zpzoom/id/{zupuId}.html'
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        mainPage = BeautifulSoup(response.text, 'html.parser')


        imageUrls = []
        for a in mainPage.select_one("#listagem-imagens > div:nth-child(1)").find_all('a'): # type: ignore
            imageUrls.append(f"https://www.ztzupu.com{a['href']}") # type: ignore
        name = SerializeFolderName(mainPage.find('title').text)

        fullSavePath = book_dir(savePath, name)
        asyncio.run(imageManager(imageUrls, fullSavePath), loop_factory=LOOP_FACTORY)
        saved_dirs.append(fullSavePath)
    return saved_dirs

if __name__ == '__main__':

    zupuIds = [29]
    main(zupuIds)