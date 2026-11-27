"""Download a FamilySearch book's page images.

The image viewer gets the whole image group's list of image IDs ("apids") in
one request as it loads, and its Download button just fetches
.../apid:<id>/$dist (the full-size JPEG) using the signed-in session. So
instead of clicking through the viewer, this signs in, opens the book's link
once to capture that list, and requests each image directly through the same
browser session.
"""

from camoufox.sync_api import Camoufox
import time
import os
from urllib.parse import urlparse, parse_qs
from Scraping.FolderNameSerializer import SerializeFolderName
from Scraping.credentials import require
from Scraping.paths import FAMILYSEARCH_DIR, book_dir


# The viewer requests the image group's details (image IDs, items, titles) here.
ORCHESTRATION_PATH = "/records/images/orchestration"
# The full-size image, as the viewer's Download button fetches it.
IMAGE_URL = "https://sg30p0.familysearch.org/service/records/storage/deepzoomcloud/dz/v1/apid:{apid}/$dist"
# How long to wait for the viewer to fetch the image list after opening a link.
IMAGE_LIST_TIMEOUT_S = 30
# After a burst of downloads FamilySearch slows them down, sometimes to many
# seconds each, so allow each image plenty of time.
IMAGE_TIMEOUT_MS = 120000
# Waits before retrying an image that failed with a server error or timeout.
RETRY_WAITS_S = (5, 15, 30, 60)


class Restricted(Exception):
    """FamilySearch refused the image: it can't be viewed online (HTTP 403)."""


def open_image_group(page, url):
    """Open a book's link and return the image group the viewer loads for it."""
    responses = []
    def collect(response):
        if ORCHESTRATION_PATH in response.url:
            responses.append(response)
    page.on("response", collect)
    try:
        page.goto(url)
        deadline = time.monotonic() + IMAGE_LIST_TIMEOUT_S
        seen = 0
        while time.monotonic() < deadline:
            # wait_for_timeout, not time.sleep, so the response events are delivered.
            page.wait_for_timeout(500)
            for response in responses[seen:]:
                seen += 1
                try:
                    group = (response.json().get("data") or {}).get("group")
                except Exception:
                    continue
                if group and group.get("imageApids"):
                    return group
    finally:
        page.remove_listener("response", collect)
    raise RuntimeError(f"FamilySearch's viewer didn't load the image list within {IMAGE_LIST_TIMEOUT_S}s")


def item_images(group, page_url):
    """The image IDs of the book the link points to, with their image numbers in the group.

    An image group can hold several books filmed together (some of them
    restricted), each listed as a child group; the viewer's URL names the one
    being viewed as groupId. A group with no such books is one book.
    """
    all_apids = group["imageApids"]
    number = {apid: i for i, apid in enumerate(all_apids, 1)}
    group_id = parse_qs(urlparse(page_url).query).get("groupId", [None])[0]
    apids = all_apids
    for child in group.get("childGroups") or []:
        if child.get("id") == group_id and child.get("imageApids"):
            apids = child["imageApids"]
            break
    else:
        if group.get("childGroups"):
            print(f"Item {group_id} not found in the image group; downloading all {len(all_apids)} images")
    return [(number[apid], apid) for apid in apids]


def download_image(page, apid, path):
    """Save one full-size image to path; raise Restricted if FamilySearch refuses it."""
    last_error = None
    for wait in (*RETRY_WAITS_S, None):
        try:
            response = page.request.get(IMAGE_URL.format(apid=apid), timeout=IMAGE_TIMEOUT_MS)
        except Exception as e:  # a timeout or dropped connection
            last_error = e
        else:
            if response.ok:
                with open(path, "wb") as f:
                    f.write(response.body())
                return
            if response.status == 403:
                raise Restricted(apid)
            if response.status == 401:
                raise RuntimeError("FamilySearch says the session isn't signed in (HTTP 401)")
            last_error = f"HTTP {response.status}"
        if wait is not None:
            print(f"Image {apid} failed ({last_error}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Could not download image {apid}: {last_error}")


def sign_in(page):
    print("Signing in")
    page.goto("https://familysearch.org/")
    time.sleep(3)
    page.click("#signInLink")
    time.sleep(3)
    page.fill("#userName", require("FAMILYSEARCH_USERNAME"))
    page.fill("#password", require("FAMILYSEARCH_PASSWORD"))
    page.click("#login")
    # Wait to be sent back from the sign-in site, or the first book's page
    # load can be cut off by that redirect.
    page.wait_for_url(lambda url: "ident.familysearch.org" not in url, timeout=30000)
    page.wait_for_load_state()
    time.sleep(3)
    print("Signed in")


def main(ids, savePath = FAMILYSEARCH_DIR, on_image=None, on_total=None):
    """Scrape each book URL in ids; returns the directory saved for each.

    on_image(path), if given, is called as each page image is saved, and
    on_total(n) with a book's image count before its download starts (and
    again, lowered, as restricted images are skipped).
    """
    saved_dirs = []
    with Camoufox() as browser:
        page = browser.new_page()
        sign_in(page)
        for id in ids:
            group = open_image_group(page, id)
            folder_name = page.locator("h1.textBaseCss_tt5gnaq > span:nth-child(1)").inner_text()
            fullSavePath = book_dir(savePath, SerializeFolderName(folder_name))
            images = item_images(group, page.url)
            total = len(images)
            print(f"Scraping {folder_name}: {total} images "
                  f"(images {images[0][0]}-{images[-1][0]} of {len(group['imageApids'])})")
            if on_total:
                on_total(total)
            restricted = 0
            for number, apid in images:
                # Named as the viewer's Download button names them: film number
                # and the image's number in the whole group.
                path = os.path.join(fullSavePath, f"{group['imageGroupNumber']}_{number:05d}.jpg")
                try:
                    download_image(page, apid, path)
                except Restricted:
                    restricted += 1
                    print(f"Image {number} is restricted; skipping it")
                    # Keep the page count to what can actually be downloaded.
                    if on_total:
                        on_total(total - restricted)
                    continue
                if on_image:
                    on_image(path)
            if restricted == total:
                raise RuntimeError(f"All {total} images of {folder_name} are restricted")
            skipped = f", skipped {restricted} restricted" if restricted else ""
            print(f"Finished scraping {folder_name}{skipped}")
            saved_dirs.append(fullSavePath)
    return saved_dirs

if __name__ == '__main__':
    main(["https://www.familysearch.org/ark:/61903/3:1:3Q9M-CSVJ-SZKK?view=explore&groupId=M94N-2BY"])
