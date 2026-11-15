"""Client for the PaddleOCR-VL-1.6 pipeline server (see docker-compose.yml).

The pipeline finds each text block on the page with PP-DocLayoutV3, reads every
block with the PaddleOCR-VL-1.6 model, and returns the blocks in reading order.
"""

import base64
import html
import os
import re
from io import BytesIO

import requests
from PIL import Image, ImageOps

from ocr_cleanup import collapse_repetitions

PADDLEOCR_URL = os.environ.get("PADDLEOCR_URL", "http://127.0.0.1:9200")
# A dense page takes a few seconds; leave plenty of headroom for queueing
# behind other pages and for very large scans.
REQUEST_TIMEOUT = 600

# Layout blocks that hold pictures rather than text. Their content is empty or
# an <img> reference, which shouldn't be searchable.
NON_TEXT_LABELS = {"image", "chart", "header_image", "footer_image", "seal"}


def _image_bytes(image_path: str) -> bytes:
    """The file's bytes, rewritten upright if it is a rotated phone photo.

    Browsers (and the other engines) show photos with their EXIF rotation
    applied, so do the same here rather than rely on the server's decoder.
    """
    with Image.open(image_path) as image:
        if image.getexif().get(0x0112, 1) == 1:  # 0x0112 = Orientation; 1 = upright
            with open(image_path, "rb") as image_file:
                return image_file.read()
        upright = ImageOps.exif_transpose(image).convert("RGB")
    buffer = BytesIO()
    upright.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def html_to_text(content: str) -> str:
    """Flatten the HTML the model writes for tables to plain text.

    Genealogy lineage tables come back as <table><tr><td>...; keep each row on
    its own line with its cells separated by spaces, so names stay searchable
    and tag names don't end up in the index.
    """
    if "<" not in content:
        return content
    content = re.sub(r"</t[dh]\s*>", " ", content, flags=re.IGNORECASE)
    content = re.sub(r"</tr\s*>|<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"<[^>]+>", "", content)
    # On garbled tables the model sometimes writes its internal cell tokens
    # (<ecel>, <fcel>, ...) as escaped text, which unescaping turns back into tags.
    content = re.sub(r"<(?:ecel|fcel|lcel|ucel|xcel|nl)>", " ", html.unescape(content))
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in content.splitlines())
    return "\n".join(line for line in lines if line)


def blocks_to_text(blocks: list[dict]) -> str:
    """Join the pipeline's text blocks, already in reading order, one per paragraph."""
    texts = []
    for block in blocks:
        if block.get("block_label") in NON_TEXT_LABELS:
            continue
        content = html_to_text(block.get("block_content") or "").strip()
        if content:
            texts.append(content)
    return "\n\n".join(texts)


def _parse(image_b64: str, use_layout_detection: bool) -> list[dict]:
    """The pipeline's blocks for one image, in reading order."""
    payload = {
        "file": image_b64,
        "fileType": 1,  # image (0 is PDF)
        "visualize": False,  # don't send back rendered layout images
        "useLayoutDetection": use_layout_detection,
    }
    response = requests.post(f"{PADDLEOCR_URL}/layout-parsing", json=payload, timeout=REQUEST_TIMEOUT)
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200 or not body or body.get("errorCode") != 0:
        detail = (body or {}).get("errorMsg") or response.text[:500]
        raise RuntimeError(f"PaddleOCR-VL server error {response.status_code}: {detail}")
    return [
        block
        for page in body["result"]["layoutParsingResults"]
        for block in page["prunedResult"].get("parsing_res_list", [])
    ]


def recognize(image_path: str) -> str:
    """OCR one page and return its text ("" if the page has none)."""
    print("Starting PaddleOCR-VL for image:", image_path)
    image_b64 = base64.b64encode(_image_bytes(image_path)).decode("ascii")
    blocks = _parse(image_b64, use_layout_detection=True)
    if not blocks:
        # The layout detector finds nothing on some faint, low-contrast scans
        # (grey grid-style lineage pages especially). Reading the whole page
        # as one block is slower and loses block order, but beats no text.
        blocks = _parse(image_b64, use_layout_detection=False)
    text = collapse_repetitions(blocks_to_text(blocks))
    print("PaddleOCR-VL Result:", text)
    return text


def is_available() -> bool:
    try:
        return requests.get(f"{PADDLEOCR_URL}/health", timeout=3).ok
    except requests.RequestException:
        return False
