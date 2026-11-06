"""Client for the TongGuOCR server running in WSL (see server.py / start_server.sh)."""

import os

import requests

TONGGUOCR_URL = os.environ.get("TONGGUOCR_URL", "http://127.0.0.1:9100")
# A dense page takes ~10-30s; leave plenty of headroom for very large scans.
REQUEST_TIMEOUT = 600

# TongGuOCR's training data writes the comma and colon as these two glyphs.
# Map them to the full-width punctuation HunyuanOCR emits so text from the two
# models reads alike.
PUNCTUATION_MAP = str.maketrans({"丶": "，", "⺀": "："})


def normalize_text(text: str) -> str:
    return text.translate(PUNCTUATION_MAP)


def extractText(image_path: str) -> str:
    print("Starting TongGuOCR for image:", image_path)
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()

    response = requests.post(
        f"{TONGGUOCR_URL}/ocr",
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

    text = normalize_text(response.json()["text"])
    print("TongGuOCR Result:", text)
    return text


def is_available() -> bool:
    try:
        return requests.get(f"{TONGGUOCR_URL}/health", timeout=3).ok
    except requests.RequestException:
        return False
