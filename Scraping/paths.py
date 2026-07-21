"""Filesystem locations for scraper output.

Paths are derived from this file's location, not the process working directory,
so the scrapers resolve the same way whether tasks.py imports them or they are
run standalone from another folder.

Output must stay under SCRAPE_ROOT: main.py serves scraped pages by mapping an
absolute image path back onto the /images mount, and get_image_url() falls back
to returning the raw path for anything outside the mounted roots.
"""

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRAPE_ROOT = os.path.join(PROJECT_ROOT, "Scraping")

# One directory per source site, matching the categories in categorize_url().
FAMILYSEARCH_DIR = os.path.join(SCRAPE_ROOT, "FamilySearch")
MCR_DIR = os.path.join(SCRAPE_ROOT, "MCR")
ZTZUPU_DIR = os.path.join(SCRAPE_ROOT, "ztzupu")


def book_dir(base: str, folder_name: str) -> str:
    """Create and return the directory for one book's page images."""
    path = os.path.join(base, folder_name)
    os.makedirs(path, exist_ok=True)
    return path
