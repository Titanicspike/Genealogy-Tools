"""Scrape every book listed in one of the *JiapuLinks.txt files, from the command line.

Run from anywhere, e.g. `python Scraping/ScraperManager.py`. Link files live next
to this script; blank lines and lines starting with # are skipped.
"""

import os
import sys

SCRAPING_DIR = os.path.dirname(os.path.abspath(__file__))
# The scrapers import each other as Scraping.<module>, so the project root has
# to be importable even when this file is run directly.
sys.path.insert(0, os.path.dirname(SCRAPING_DIR))

from Scraping import FamilySearch, MCR, ztzupu


def read_links(filename: str) -> list[str]:
    with open(os.path.join(SCRAPING_DIR, filename), "r", encoding="utf-8") as f:
        lines = (line.strip() for line in f)
        return [line for line in lines if line and not line.startswith("#")]


def main():
    source = input("Is this from Familysearch (1), MyChinaRoots (2), or ZtZupu (3)? ")

    if source == "1":
        saved_dirs = FamilySearch.main(read_links("FamilySearchJiapuLinks.txt"))
    elif source == "2":
        saved_dirs = MCR.main(read_links("MCRJiapuLinks.txt"))
    elif source == "3":
        saved_dirs = ztzupu.main(read_links("ZtZupuJiapuLinks.txt"))
    else:
        print(f"Unknown choice {source!r}")
        return

    for saved_dir in saved_dirs:
        print(f"Saved {saved_dir}")


if __name__ == "__main__":
    main()
