"""Client for the TongGuOCR server running in WSL (see server.py / start_server.sh)."""

import json
import os

import requests

from ocr_cleanup import collapse_repetitions

TONGGUOCR_URL = os.environ.get("TONGGUOCR_URL", "http://127.0.0.1:9100")
# A dense page takes ~10-40s; leave plenty of headroom for very large scans.
REQUEST_TIMEOUT = 600

# TongGuOCR's training data writes the comma and colon as these two glyphs.
# Map them to the full-width punctuation HunyuanOCR emits so text from the two
# models reads alike.
PUNCTUATION_MAP = str.maketrans({"丶": "，", "⺀": "："})


def normalize_text(text: str) -> str:
    return collapse_repetitions(text.translate(PUNCTUATION_MAP))


def recognize(image_path: str, with_text: bool = True) -> tuple[str | None, str]:
    """OCR one page.

    Returns (text, layout_json). text is the searchable page text, or None when
    with_text is False (layout only, for pages that already have text).
    layout_json is the JSON string stored in pages.tongguocr_layout:
    {"image_size": [w, h], "columns": [{"bbox": [x1, y1, x2, y2], "text": ...}]}.
    """
    print("Starting TongGuOCR for image:", image_path)
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()

    response = requests.post(
        f"{TONGGUOCR_URL}/ocr" + ("" if with_text else "?mode=columns"),
        data=image_bytes,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Image-Suffix": os.path.splitext(image_path)[1].lower() or ".jpg",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"TongGuOCR server error {response.status_code}: {detail}")

    payload = response.json()
    layout = payload["layout"]
    for column in layout["columns"]:
        column["text"] = normalize_text(column["text"])
    text = normalize_text(payload["text"]) if with_text else None
    if text is not None:
        print("TongGuOCR Result:", text)
    return text, json.dumps(layout, ensure_ascii=False)


def is_available() -> bool:
    try:
        return requests.get(f"{TONGGUOCR_URL}/health", timeout=3).ok
    except requests.RequestException:
        return False
