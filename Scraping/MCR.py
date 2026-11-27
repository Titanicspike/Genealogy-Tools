from camoufox.async_api import AsyncCamoufox
import time
import os
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.credentials import require
from Scraping.paths import MCR_DIR, book_dir
import asyncio
import requests
from urllib.parse import unquote


def save_image(image_src, save_dir):
    """Download the page image the viewer is showing; returns where it was saved."""
    print(image_src)
    response = requests.get(image_src, timeout=120)
    response.raise_for_status()
    path = os.path.join(save_dir, unquote(image_src.split('?')[0].split('/')[-1]))
    with open(path, "wb") as f:
        f.write(response.content)
    return path


async def mainAsync(ids, pathName, on_image=None):
    username = require("MCR_USERNAME")
    password = require("MCR_PASSWORD")

    saved_dirs = []
    async with AsyncCamoufox() as browser:
        page = await browser.new_page()
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
            while True:
                image_path = save_image(await page.locator(".image").get_attribute('src'), fullSavePath)
                if on_image:
                    on_image(image_path)
                if not await page.is_visible("[title = 'Next Page [PAGE DOWN]']"):
                    break
                await page.click("[title = 'Next Page [PAGE DOWN]']")
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