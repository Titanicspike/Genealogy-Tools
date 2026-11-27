from camoufox.sync_api import Camoufox
import time
import os
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.credentials import require
from Scraping.paths import FAMILYSEARCH_DIR, book_dir


DOWNLOAD_BUTTON = "//button[@aria-label='Download']"
# The Next Image button only appears while the mouse is over the image, so
# move between images by typing the number into the page box instead.
IMAGE_NUMBER_BOX = "[aria-label='Enter Image number']"
# How long to wait for the viewer to switch images after entering a number.
NAVIGATION_TIMEOUT_MS = 15000
# How long to wait for an image group's list of items ("Images: 84-249").
ITEM_LIST_TIMEOUT_MS = 5000
# The Download button isn't ready the moment a page loads, so poll it with
# short timeouts; give up on the page after about this many tries.
MAX_DOWNLOAD_ATTEMPTS = 40
DOWNLOAD_TIMEOUT_MS = 1000


def is_restricted(page):
    """Whether the viewer is showing FamilySearch's "Image Restricted" notice instead of an image."""
    return page.get_by_text("Image Restricted", exact=True).first.is_visible()


def download_current_image(page, save_dir):
    """Download the image the viewer is showing and return its path.

    Returns None for a restricted image, which has no Download button; raises
    if an unrestricted image never downloads.
    """
    last_error = None
    restricted_checks = 0
    for _ in range(MAX_DOWNLOAD_ATTEMPTS):
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                page.click(DOWNLOAD_BUTTON, timeout=DOWNLOAD_TIMEOUT_MS)
            download = download_info.value
            path = os.path.join(save_dir, download.suggested_filename)
            download.save_as(path)
            return path
        except Exception as e:
            last_error = e
        # Seen on two tries in a row, so a notice left over from the previous
        # image while the viewer switches isn't mistaken for this one's.
        restricted_checks = restricted_checks + 1 if is_restricted(page) else 0
        if restricted_checks >= 2:
            return None
    raise RuntimeError(
        f"Could not download image after {MAX_DOWNLOAD_ATTEMPTS} attempts: {last_error}"
    )


def image_count(page):
    """Return how many images the book has: the page box's max, or the "of N" beside it."""
    box = page.locator(IMAGE_NUMBER_BOX)
    box.wait_for(timeout=NAVIGATION_TIMEOUT_MS)
    total = box.get_attribute("max")
    if not (total and total.isdigit()):
        total = box.evaluate(
            """el => {
                for (let node = el.parentElement; node; node = node.parentElement) {
                    const m = node.innerText.match(/of\\s*([\\d,]+)/i);
                    if (m) return m[1].replace(/,/g, '');
                }
                return null;
            }"""
        )
    if not (total and total.isdigit()):
        raise RuntimeError("Could not find the number of images next to the page box")
    return int(total)


def item_range(page, total):
    """The first and last image numbers of the book the link points to.

    A link can open a whole image group: several books filmed together (some
    of them restricted), listed in the side panel as "Images: 1-83",
    "Images: 84-249" and so on. Keep to the item holding the image the link
    opened on. A link to a single item has no such list, so it's 1 to total.
    """
    try:
        page.wait_for_function(
            "() => [...document.querySelectorAll('p')].some(p => /^Images:\\s*\\d/.test(p.textContent))",
            timeout=ITEM_LIST_TIMEOUT_MS,
        )
    except Exception:
        return 1, total
    ranges = page.locator("p").evaluate_all(
        """els => els
            .map(el => el.textContent.match(/^Images:\\s*(\\d+)\\s*-\\s*(\\d+)/))
            .filter(Boolean)
            .map(m => [parseInt(m[1]), parseInt(m[2])])"""
    )
    current = page.locator(IMAGE_NUMBER_BOX).input_value()
    current = int(current) if current.isdigit() else 1
    for first, last in ranges:
        if first <= current <= last:
            return first, last
    return 1, total


def go_to_image(page, number):
    """Show image `number` by typing it into the page box, and wait for the viewer to switch."""
    previous_url = page.url
    box = page.locator(IMAGE_NUMBER_BOX)
    box.fill(str(number))
    box.press("Enter")
    # Each image has its own ark ID in the URL, so a URL change means it switched.
    page.wait_for_url(lambda url: url != previous_url, timeout=NAVIGATION_TIMEOUT_MS)


def main(ids, savePath = FAMILYSEARCH_DIR, on_image=None, on_total=None):
    """Scrape each book URL in ids; returns the directory saved for each.

    on_image(path), if given, is called as each page image is saved, and
    on_total(n) with a book's image count before its download starts.
    """
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
        # Wait to be sent back from the sign-in site, or the first book's page
        # load can be cut off by that redirect.
        page.wait_for_url(lambda url: "ident.familysearch.org" not in url, timeout=30000)
        page.wait_for_load_state()
        time.sleep(3)
        print("Signed in")
        for id in ids:
            page.goto(id)
            time.sleep(3)
            folder_name = page.locator("h1.textBaseCss_tt5gnaq > span:nth-child(1)").inner_text()
            fullSavePath = book_dir(savePath, SerializeFolderName(folder_name))
            print(f"Scraping {folder_name}")
            first, last = item_range(page, image_count(page))
            total = last - first + 1
            print(f"{total} images (images {first}-{last})")
            if on_total:
                on_total(total)
            restricted = 0
            for number in range(first, last + 1):
                box_value = page.locator(IMAGE_NUMBER_BOX).input_value()
                if box_value != str(number):
                    go_to_image(page, number)
                image_path = download_current_image(page, fullSavePath)
                if image_path is None:
                    restricted += 1
                    print(f"Image {number} is restricted; skipping it")
                    # Keep the page count to what can actually be downloaded.
                    if on_total:
                        on_total(total - restricted)
                    continue
                if on_image:
                    on_image(image_path)
            if restricted == total:
                raise RuntimeError(f"All {total} images of {folder_name} are restricted")
            skipped = f", skipped {restricted} restricted" if restricted else ""
            print(f"Finished scraping {folder_name}{skipped}")
            saved_dirs.append(fullSavePath)
    return saved_dirs

if __name__ == '__main__':
    main(["https://www.familysearch.org/ark:/61903/3:1:3Q9M-CSVJ-SZKK?view=explore&groupId=M94N-2BY"])