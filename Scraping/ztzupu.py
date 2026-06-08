import aiohttp
import requests
import os
import asyncio
from bs4 import BeautifulSoup
from Scraping.FolderNameSerializer import SerializeFolderName
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
async def getImage(session, url, num, attempt = 0):
    if attempt < 10:
        print("Getting image from: " + url)
        try:
            async with session.get(url) as response:
                with open(f'{saveDir}{num}.{url.split(".")[-1]}', 'wb') as f:
                    f.write(await response.read())
                    print("Saved image to: " + f.name)
        except Exception as e:
            print(f"Attempt {attempt} failed: {e}")
            await getImage(session, url, num, attempt + 1)

async def imageManager():
    connector = aiohttp.TCPConnector(limit=100)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[getImage(session, url, n) for n, url in enumerate(imageUrls)])

def main(zupuIds, savePath = r"C:\Users\njwye\Documents\py\Genealogy Tools\CamoufoxScraping\ztzupu"):
    for zupuId in zupuIds:
        url = f'http://www.ztzupu.com/ztzupu/zpzoom/id/{zupuId}.html' 
        mainPage = BeautifulSoup(requests.get(url).text, 'html.parser')


        imageUrls = []
        for a in mainPage.select_one("#listagem-imagens > div:nth-child(1)").find_all('a'): # type: ignore
            imageUrls.append(f"https://www.ztzupu.com{a['href']}") # type: ignore
        name = SerializeFolderName(mainPage.find('title').text)


        fullSavePath = f"{savePath}/{name}/"
        os.makedirs(fullSavePath, exist_ok=True)
        asyncio.run(imageManager())
        return fullSavePath

if __name__ == '__main__':

    zupuIds = [29]
    path = r"C:\Users\njwye\Documents\py\Genealogy Tools\CamoufoxScraping\ztzupu"
    main(zupuIds)