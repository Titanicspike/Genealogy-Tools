from camoufox.async_api import AsyncCamoufox
import os
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.credentials import require
from Scraping.paths import MCR_DIR, book_dir
import asyncio
from urllib.parse import unquote

NEXT_PAGE = "[title = 'Next Page [PAGE DOWN]']"
# How long to wait for the browser to finish loading a page's image before
# fetching it directly instead.
IMAGE_LOAD_TIMEOUT = 120


class ImageResponses:
    """Hold on to the image responses the browser receives, keyed by URL.

    The viewer loads each page image itself, and may load the next one before
    the scraper asks for it, so responses are recorded as they arrive and handed
    out by URL. Saving the browser's copy avoids downloading every page twice
    and uses the browser's own session.
    """

    def __init__(self, page):
        self._waiting: dict[str, asyncio.Future] = {}
        page.on("response", self._on_response)

    def _future(self, url: str) -> asyncio.Future:
        if url not in self._waiting:
            self._waiting[url] = asyncio.get_running_loop().create_future()
        return self._waiting[url]

    def _on_response(self, response):
        if response.request.resource_type != "image":
            return
        # A redirect is reported as a response of its own; wait for the final one.
        if 300 <= response.status < 400:
            return
        # Record it under every URL in a redirect chain, so it is found by the
        # URL the <img> asked for as well as the one that finally answered.
        request = response.request
        while request is not None:
            future = self._future(request.url)
            if not future.done():
                future.set_result(response)
            request = request.redirected_from

    async def get(self, url: str, timeout: float):
        """The browser's response for url, or None if it doesn't load in time."""
        try:
            return await asyncio.wait_for(asyncio.shield(self._future(url)), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            # Drop it once handled, so a long book doesn't keep every page in memory.
            if self._waiting.get(url) is not None and self._waiting[url].done():
                del self._waiting[url]


async def save_current_image(page, responses: ImageResponses, save_dir: str) -> str:
    """Save the image the viewer is showing and return where it was saved.

    Uses the copy the browser already loaded. If none arrives (for instance the
    browser served it from cache without a network response), fetch it once
    through the browser's session instead.
    """
    # currentSrc/src give the absolute URL the browser actually requested.
    image_src = await page.locator(".image").evaluate("img => img.currentSrc || img.src")
    print(image_src)
    response = await responses.get(image_src, IMAGE_LOAD_TIMEOUT)
    if response is None:
        print(f"No browser response for {image_src}; fetching it directly")
        response = await page.context.request.get(image_src, timeout=IMAGE_LOAD_TIMEOUT * 1000)
    if not response.ok:
        raise RuntimeError(f"Image {image_src} returned HTTP {response.status}")
    body = await response.body()
    path = os.path.join(save_dir, unquote(image_src.split('?')[0].split('/')[-1]))
    with open(path, "wb") as f:
        f.write(body)
    return path


async def scrape_book_pages(page, responses: ImageResponses, save_dir: str, on_image=None):
    """Save every page of the open viewer, from the current page to the last."""
    while True:
        image_path = await save_current_image(page, responses, save_dir)
        if on_image:
            on_image(image_path)
        if not await page.is_visible(NEXT_PAGE):
            break
        await page.click(NEXT_PAGE)


async def mainAsync(ids, pathName, on_image=None):
    username = require("MCR_USERNAME")
    password = require("MCR_PASSWORD")

    saved_dirs = []
    async with AsyncCamoufox() as browser:
        page = await browser.new_page()
        responses = ImageResponses(page)
        await page.goto("https://login.rpa.sccl.org/login?qurl=https://mychinarootslibrary.com%2f")
        await page.fill("body > form:nth-child(3) > input:nth-child(4)", username)
        await page.fill("body > form:nth-child(3) > input:nth-child(7)", password)
        await page.click("body > form:nth-child(3) > p:nth-child(9) > input:nth-child(1)")
        for id in ids:
            await page.goto(f"https://mychinarootslibrary-com.rpa.sccl.org/zupus/{id}")
            folder_name = await page.locator(".zupu-info > div:nth-child(1) > h1:nth-child(1)").inner_text()
            folder_name = SerializeFolderName(folder_name)
            print(folder_name)
            fullSavePath = book_dir(pathName, folder_name)
            await page.click(".zupu-info > div:nth-child(1) > div:nth-child(3) > button:nth-child(1) > div:nth-child(1)")
            await page.locator('.viewport').wait_for(state='visible', timeout=100000)
            await scrape_book_pages(page, responses, fullSavePath, on_image)
            print(f"Finished scraping {folder_name}")
            saved_dirs.append(fullSavePath)
    return saved_dirs

def main(ids, pathName = MCR_DIR, on_image=None):
    """Scrape each book id in ids; returns the directory saved for each.

    on_image(path), if given, is called as each page image is saved.
    """
    return asyncio.run(mainAsync(ids, pathName, on_image))

if __name__ == '__main__':
    main(["3ff8274e-d4b7-4400-9fec-b742cabee362"])
