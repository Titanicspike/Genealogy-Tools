from camoufox.async_api import AsyncCamoufox
import time
import os
from Scraping.FolderNameSerializer import SerializeFolderName
import asyncio
import requests

username = "23305018526547"
password = "0926"


async def mainAsync(ids, pathName):
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
            fullSavePath = f"{pathName}\"{folder_name}"
            os.makedirs(fullSavePath, exist_ok=True)
            await page.click(".zupu-info > div:nth-child(1) > div:nth-child(3) > button:nth-child(1) > div:nth-child(1)")
            await page.locator('.viewport').wait_for(state='visible', timeout=100000)
            while await page.is_visible("[title = 'Next Page [PAGE DOWN]']"):
                image_src = await page.locator(".image").get_attribute('src')
                print(image_src)
                response = requests.get(image_src)
                with open(f"{fullSavePath}\{image_src.split('?')[0].split('/')[-1]}", "wb") as f:
                    f.write(response.content)
                await page.click("[title = 'Next Page [PAGE DOWN]']")
            image_src = await page.locator(".image").get_attribute('src')
            print(image_src)
            response = requests.get(image_src)
            with open(f"{fullSavePath}\{image_src.split('?')[0].split('/')[-1]}", "wb") as f:
                f.write(response.content)
            print(f"Finished scraping {folder_name}")
            return fullSavePath

def main(ids, pathName = r"C:\Users\njwye\Documents\py\Genealogy Tools\CamoufoxScraping\MCR"):
    asyncio.run(mainAsync(ids, pathName))

if __name__ == '__main__':
    main(["3ff8274e-d4b7-4400-9fec-b742cabee362"])