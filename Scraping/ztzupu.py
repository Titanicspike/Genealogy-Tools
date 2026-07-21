import aiohttp
import requests
import os
import sys
import asyncio
from bs4 import BeautifulSoup
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.paths import ZTZUPU_DIR, book_dir

# aiohttp misbehaves on Windows under the default Proactor loop (sockets can
# raise "Event loop is closed" during teardown), so prefer the Selector loop.
# The Windows*EventLoopPolicy classes only exist on Windows, and this runs at
# import time, so an unguarded call breaks importing this module anywhere else.
# Elsewhere the default loop is already selector-based and needs no override.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
async def getImage(session, url, num, saveDir, attempt = 0):
    if attempt < 10:
        print("Getting image from: " + url)
        try:
            async with session.get(url) as response:
                with open(os.path.join(saveDir, f'{num}.{url.split(".")[-1]}'), 'wb') as f:
                    f.write(await response.read())
                    print("Saved image to: " + f.name)
        except Exception as e:
            print(f"Attempt {attempt} failed: {e}")
            await getImage(session, url, num, saveDir, attempt + 1)

async def imageManager(imageUrls, saveDir):
    connector = aiohttp.TCPConnector(limit=100)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[getImage(session, url, n, saveDir) for n, url in enumerate(imageUrls)])

def main(zupuIds, savePath = ZTZUPU_DIR):
    for zupuId in zupuIds:
        url = f'http://www.ztzupu.com/ztzupu/zpzoom/id/{zupuId}.html'
        mainPage = BeautifulSoup(requests.get(url).text, 'html.parser')


        imageUrls = []
        for a in mainPage.select_one("#listagem-imagens > div:nth-child(1)").find_all('a'): # type: ignore
            imageUrls.append(f"https://www.ztzupu.com{a['href']}") # type: ignore
        name = SerializeFolderName(mainPage.find('title').text)

        fullSavePath = book_dir(savePath, name)
        asyncio.run(imageManager(imageUrls, fullSavePath))
        return fullSavePath

if __name__ == '__main__':

    zupuIds = [29]
    main(zupuIds)