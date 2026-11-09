"""Long-running TongGuOCR server.

Runs inside WSL with the `tongguocr` environment (see start_server.sh) and keeps
the layout detector and OCR model loaded between requests. The Windows app posts
raw image bytes to /ocr and receives JSON with:

- text: the page text from TongGuOCR's normal pipeline (layout -> blocks ->
  block OCR). This is the searchable text.
- layout: every text column the layout detector found, each with its box in
  image pixels and the text read from that column on its own. Block OCR text
  can't be traced back to individual columns, so this second, per-column pass
  is what lets the viewer show where a word sits on the page.

POST /ocr?mode=columns skips block OCR and returns only the layout, for pages
that already have text.

Requests are processed one at a time: the pipeline holds a single GPU model and
is not safe to run concurrently.
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TONGGUOCR_ROOT = Path(os.environ.get("TONGGUOCR_ROOT", Path.home() / "TongGuOCR"))
sys.path.insert(0, str(TONGGUOCR_ROOT))
# The pipeline resolves weights/ relative to the checkout.
os.chdir(TONGGUOCR_ROOT)

import torch  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402
from tongguocr import load_config, run_pipeline_on_image  # noqa: E402
from tongguocr.layout import extract_line_bboxes  # noqa: E402
# Private helpers of the pinned TongGuOCR checkout; reusing them shares the
# already-loaded model and keeps preprocessing identical to block OCR.
from tongguocr.ocr.preprocess import prepare_pil_image  # noqa: E402
from tongguocr.ocr.relation_tokens import relation_tokens_to_newlines, strip_control_tokens  # noqa: E402
from tongguocr.ocr.transformers_backend import _load_transformers_runtime  # noqa: E402

HOST = os.environ.get("TONGGUOCR_HOST", "0.0.0.0")
PORT = int(os.environ.get("TONGGUOCR_PORT", "9100"))
MAX_BODY = 100 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
LOGGER = logging.getLogger("tongguocr-server")

CONFIG = load_config(None)
LOCK = threading.Lock()


COLUMN_BATCH_SIZE = 16


def _column_token_cap(bbox) -> int:
    """Upper bound on tokens for one column, from its shape.

    A column holds roughly (long side / short side) characters. Capping
    generation there stops the occasional repetition loop on a bad crop from
    stalling the whole batch.
    """
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return int(max(width, height) / max(1, min(width, height)) * 2.5) + 10


def recognize_columns(image_path: Path, column_boxes: list) -> list[str]:
    """Read each detected column on its own, batched by similar length."""
    if not column_boxes:
        return []
    inference = CONFIG["ocr"]["inference"]
    backend = CONFIG["ocr"]["transformers"]
    processor, model = _load_transformers_runtime(backend["model_path"], backend.get("device", "cuda:0"))
    max_pixels, min_pixels = int(inference["image_max_pixels"]), int(inference["image_min_pixels"])
    processor.image_max_pixels, processor.image_min_pixels = max_pixels, min_pixels
    processor.image_do_pan_and_scan = False
    processor.crop_to_patches = False
    tokenizer = getattr(processor, "tokenizer", processor)
    tokenizer.padding_side = "left"  # batched generation needs left padding
    messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": CONFIG["ocr"]["prompt"]}]}]
    prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    # The layout detector reads phone photos upright (EXIF rotation applied),
    # so its boxes are in rotated coordinates; crop from the same orientation.
    page = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    crop_dir = image_path.parent / "columns"
    crop_dir.mkdir(exist_ok=True)
    crop_paths = {}
    for index, (x1, y1, x2, y2) in enumerate(column_boxes):
        pad = max(4, int(min(x2 - x1, y2 - y1) * 0.15))
        box = (max(0, x1 - pad), max(0, y1 - pad), min(page.width, x2 + pad), min(page.height, y2 + pad))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            LOGGER.warning("Skipping column %s: box %s lies outside the %sx%s page", index, (x1, y1, x2, y2), page.width, page.height)
            continue
        crop_path = crop_dir / f"{index}.png"
        page.crop(box).save(crop_path)
        crop_paths[index] = crop_path

    texts = [""] * len(column_boxes)
    order = sorted(crop_paths, key=lambda i: _column_token_cap(column_boxes[i]))
    for start in range(0, len(order), COLUMN_BATCH_SIZE):
        batch = order[start:start + COLUMN_BATCH_SIZE]
        images = [prepare_pil_image(crop_paths[i], image_max_pixels=max_pixels, image_min_pixels=min_pixels) for i in batch]
        inputs = processor(text=[prompt] * len(images), images=images, return_tensors="pt", padding=True).to(model.device)
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max(_column_token_cap(column_boxes[i]) for i in batch),
                do_sample=False,
            )
        for i, row in zip(batch, outputs[:, inputs["input_ids"].shape[1]:]):
            raw = processor.decode(row, skip_special_tokens=False)
            texts[i] = relation_tokens_to_newlines(strip_control_tokens(raw, tokenizer.all_special_tokens)).strip()
    return texts


def _apply_exif_rotation(image_path: Path) -> None:
    """Rewrite a phone photo upright, without its EXIF rotation tag.

    TongGuOCR's layout detector reads images with the camera rotation applied,
    but its block cropping (and PIL generally) does not, so boxes for a rotated
    photo land outside the unrotated image. Baking the rotation in once means
    every step sees the same upright image, which is also how browsers show it.
    """
    with Image.open(image_path) as image:
        if image.getexif().get(0x0112, 1) == 1:  # 0x0112 = Orientation; 1 = upright
            return
        upright = ImageOps.exif_transpose(image)
        if upright.mode not in ("RGB", "L"):
            upright = upright.convert("RGB")
    if image_path.suffix.lower() in (".jpg", ".jpeg"):
        upright.save(image_path, quality=95)
    else:
        upright.save(image_path)


def recognize(image_bytes: bytes, suffix: str, with_text: bool = True) -> dict:
    with tempfile.TemporaryDirectory(prefix="tongguocr_") as work_dir:
        image_path = Path(work_dir) / f"page{suffix}"
        image_path.write_bytes(image_bytes)
        _apply_exif_rotation(image_path)
        started = time.perf_counter()
        with LOCK:
            try:
                if with_text:
                    result = run_pipeline_on_image(
                        image_path,
                        Path(work_dir) / "out",
                        CONFIG,
                        visualize=False,
                        crop_blocks=False,
                    )
                    text = result.ocr_text
                    image_size = list(result.image_size)
                    boxes = [list(map(int, line["bbox"])) for line in result.line_bboxes]
                else:
                    layout_page = extract_line_bboxes(image_path, CONFIG)
                    text = None
                    image_size = list(layout_page.image_size)
                    boxes = [list(map(int, line.bbox)) for line in layout_page.lines]
                column_texts = recognize_columns(image_path, boxes)
            finally:
                # Hand cached VRAM back between pages so HunyuanOCR's vLLM, which
                # shares the GPU, can still start and allocate while this runs.
                torch.cuda.empty_cache()
        return {
            "text": text,
            "layout": {
                "image_size": image_size,
                "columns": [{"bbox": box, "text": col_text} for box, col_text in zip(boxes, column_texts)],
            },
            "seconds": round(time.perf_counter() - started, 2),
        }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path != "/ocr":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"error": "missing or oversized image body"})
            return
        image_bytes = self.rfile.read(length)
        suffix = self.headers.get("X-Image-Suffix", ".jpg")
        if suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
            suffix = ".jpg"
        try:
            payload = recognize(image_bytes, suffix, with_text="mode=columns" not in query)
        except Exception as exc:  # report the failure to the client instead of dropping the connection
            LOGGER.exception("OCR failed")
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        LOGGER.info("OCR done: %s columns in %ss", len(payload["layout"]["columns"]), payload["seconds"])
        self._send_json(200, payload)

    def log_message(self, format, *args):
        pass


def main():
    # Load both models up front so the first real request isn't slow.
    warmup = TONGGUOCR_ROOT / "weights" / "warmup.jpg"
    if warmup.is_file():
        LOGGER.info("Warming up models...")
        recognize(warmup.read_bytes(), ".jpg")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOGGER.info("TongGuOCR server listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
