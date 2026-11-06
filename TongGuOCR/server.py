"""Long-running TongGuOCR server.

Runs inside WSL with the `tongguocr` environment (see start_server.sh) and keeps
the layout detector and OCR model loaded between requests. The Windows app posts
raw image bytes to /ocr and receives the recognized text as JSON.

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
from tongguocr import load_config, run_pipeline_on_image  # noqa: E402

HOST = os.environ.get("TONGGUOCR_HOST", "0.0.0.0")
PORT = int(os.environ.get("TONGGUOCR_PORT", "9100"))
MAX_BODY = 100 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
LOGGER = logging.getLogger("tongguocr-server")

CONFIG = load_config(None)
LOCK = threading.Lock()


def recognize(image_bytes: bytes, suffix: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="tongguocr_") as work_dir:
        image_path = Path(work_dir) / f"page{suffix}"
        image_path.write_bytes(image_bytes)
        started = time.perf_counter()
        with LOCK:
            result = run_pipeline_on_image(
                image_path,
                Path(work_dir) / "out",
                CONFIG,
                visualize=False,
                crop_blocks=False,
            )
            # Hand cached VRAM back between pages so HunyuanOCR's vLLM, which
            # shares the GPU, can still start and allocate while this runs.
            torch.cuda.empty_cache()
        return {
            "text": result.ocr_text,
            "line_count": result.line_count,
            "block_count": result.block_count,
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
        if self.path.split("?")[0] != "/ocr":
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
            payload = recognize(image_bytes, suffix)
        except Exception as exc:  # report the failure to the client instead of dropping the connection
            LOGGER.exception("OCR failed")
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        LOGGER.info("OCR done: %s blocks in %ss", payload["block_count"], payload["seconds"])
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
