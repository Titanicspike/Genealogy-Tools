from camoufox.sync_api import Camoufox
import time
import os
from Scraping.FolderNameSerializer import SerializeFolderName


print("starting")
def main(ids, savePath = r"C:\Users\njwye\Documents\py\Genealogy Tools\CamoufoxScraping\FamilySearch"):
    username = "Titanicspike"
    password = "6Wjb$9-&r4h&ZaY"
    print("Does the function start?")
    with Camoufox() as browser:
        page = browser.new_page()
        print("Signing in")
        page.goto("https://familysearch.org/")
        time.sleep(3)
        page.click("#signInLink")
        time.sleep(3)
        page.fill("#userName", username)
        page.fill("#password", password)
        page.click("#login")
        time.sleep(3)
        print("Signed in")
        for id in ids:
            page.goto(id)
            time.sleep(3)
            folder_name = page.locator("h1.textBaseCss_tt5gnaq > span:nth-child(1)").inner_text()
            print(folder_name)
            fullSavePath = f"{savePath}\{SerializeFolderName(folder_name)}"
            os.makedirs(fullSavePath, exist_ok=True)
            print(f"Scraping {folder_name}")
            page.fill("[aria-label='Enter Image number']", "1")
            while page.locator("//button[@aria-label='Next Image']").get_attribute('aria-disabled') == 'false':
                for _ in range(40):
                    try:
                        with page.expect_download(timeout=1000) as download_info:
                            page.click("//button[@aria-label='Download']", timeout=1000)
                        download = download_info.value
                        download.save_as(f"{fullSavePath}\{download.suggested_filename}")
                        page.click("//button[@aria-label='Next Image']")
                        break
                    except Exception as e:
                        print(e)
                
            with page.expect_download() as download_info:
                page.click("//button[@aria-label='Download']")
            download = download_info.value
            download.save_as(f"{fullSavePath}\{download.suggested_filename}")
            print(f"Finished scraping {folder_name}")
        print("If we make it this far, I'll be amazed")
        return fullSavePath

if __name__ == '__main__':
    main(["https://www.familysearch.org/ark:/61903/3:1:3Q9M-CSVJ-SZKK?view=explore&groupId=M94N-2BY"])