from camoufox.sync_api import Camoufox
import time
import os
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.credentials import require
from Scraping.paths import FAMILYSEARCH_DIR, book_dir


DOWNLOAD_BUTTON = "//button[@aria-label='Download']"
NEXT_BUTTON = "//button[@aria-label='Next Image']"
# The Download button isn't ready the moment a page loads, so poll it with
# short timeouts; give up on the page after about this many tries.
MAX_DOWNLOAD_ATTEMPTS = 40
DOWNLOAD_TIMEOUT_MS = 1000


def download_current_image(page, save_dir):
    """Download the image the viewer is showing, or raise if it never arrives."""
    last_error = None
    for _ in range(MAX_DOWNLOAD_ATTEMPTS):
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                page.click(DOWNLOAD_BUTTON, timeout=DOWNLOAD_TIMEOUT_MS)
            download = download_info.value
            download.save_as(os.path.join(save_dir, download.suggested_filename))
            return
        except Exception as e:
            last_error = e
    raise RuntimeError(
        f"Could not download image after {MAX_DOWNLOAD_ATTEMPTS} attempts: {last_error}"
    )


def main(ids, savePath = FAMILYSEARCH_DIR):
    """Scrape each book URL in ids; returns the directory saved for each."""
    username = require("FAMILYSEARCH_USERNAME")
    password = require("FAMILYSEARCH_PASSWORD")
    saved_dirs = []
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
            fullSavePath = book_dir(savePath, SerializeFolderName(folder_name))
            print(f"Scraping {folder_name}")
            page.fill("[aria-label='Enter Image number']", "1")
            while True:
                download_current_image(page, fullSavePath)
                next_button = page.locator(NEXT_BUTTON)
                if next_button.get_attribute('aria-disabled') != 'false':
                    break
                next_button.click()
            print(f"Finished scraping {folder_name}")
            saved_dirs.append(fullSavePath)
    return saved_dirs

if __name__ == '__main__':
    main(["https://www.familysearch.org/ark:/61903/3:1:3Q9M-CSVJ-SZKK?view=explore&groupId=M94N-2BY"])