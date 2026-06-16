import asyncio
import os
import sys
import traceback
import requests
import base64
import re
print(os.path.join(os.path.dirname(__file__), 'CamoufoxScraping'))
# Add CamoufoxScraping to the path so we can import the scrapers
sys.path.append(os.path.join(os.path.dirname(__file__), 'CamoufoxScraping'))

from Scraping import FamilySearch, MCR, ztzupu
from database import get_db

def extract_text_with_paddle(image_path: str) -> str:
    url = "http://localhost:8085/layout-parsing"
    try:
        with open(image_path, "rb") as f:
            img_str = base64.b64encode(f.read()).decode('utf-8')
        
        payload = {
            "file": img_str,
            "fileType": 1
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            data = response.json()
            print(data)
            # The result usually contains 'doc_content' (the text/markdown)
            return data.get('result').get("layoutParsingResults")[0].get("markdown").get("text", "")
        else:
            print(f"OCR Error {response.status_code}: {response.text}")
            return ""
    except Exception as e:
        print(f"Exception during OCR for {image_path}: {e}")
        return ""

async def perform_ocr(source_id: int, image_dir: str):
    if not image_dir or not os.path.isdir(image_dir):
        print(f"Directory not found or invalid: {image_dir}")
        return

    print(f"Running OCR on images in {image_dir}")
    db = await get_db()
    try:
        for filename in os.listdir(image_dir):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                image_path = os.path.join(image_dir, filename)
                print(f"Processing {image_path}...")
                ocr_text = await asyncio.to_thread(extract_text_with_paddle, image_path)
                
                # Save to pages table
                await db.execute(
                    "INSERT INTO pages (source_id, image_path, ocr_text) VALUES (?, ?, ?)",
                    (source_id, image_path, ocr_text)
                )
        await db.commit()
    finally:
        await db.close()

def run_scraper_sync(category: str, url: str):
    print(f"Running scraper for {category} with URL: {url}")
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        if category == "FamilySearch":
            print([url])
            return FamilySearch.main([url])
        elif category == "MyChinaRoots":
            return MCR.main([url])
        elif category == "ZtZupu":
            return ztzupu.main([re.sub(r'[^\d]', '', url)])
        else:
            print(f"Unknown category {category}")
            return None
    except Exception as e:
        print(f"Error during scraping: {e}")
        print(f"Error type: {type(e)}")
        print(f"Error: {e}")
        print(f"Traceback: {traceback.format_exc()}")  # full traceback
        raise e

async def process_jiapu_source(source_id: int, url: str, category: str):
    db = await get_db()
    try:
        print("1")
        # Update status to Scraping
        await db.execute("UPDATE sources SET status = 'Scraping' WHERE id = ?", (source_id,))
        await db.commit()
        print("2")
        # Run the synchronous scraper in a separate thread so it doesn't block the async event loop
        image_dir = await asyncio.to_thread(run_scraper_sync, category, url)
        print("3")
        # Update status to OCR
        await db.execute("UPDATE sources SET status = 'Running OCR' WHERE id = ?", (source_id,))
        await db.commit()
        print("4")
        # Run the OCR
        if image_dir:
            await perform_ocr(source_id, image_dir)
        else:
            print("No image directory returned from scraper.")
        print("5")
        # Update status to Completed
        await db.execute("UPDATE sources SET status = 'Completed' WHERE id = ?", (source_id,))
        await db.commit()
        print("6")
    except Exception as e:
        print(f"Failed to process source {source_id}: {e}")
        await db.execute("UPDATE sources SET status = 'Failed', error_message = ? WHERE id = ?", (str(e), source_id))
        await db.commit()
    finally:
        await db.close()
