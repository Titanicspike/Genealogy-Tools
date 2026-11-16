"""Start the OCR engines when a page needs them and stop them once idle.

The three engines (HunyuanOCR's vLLM container, the two PaddleOCR-VL
containers, and the TongGuOCR server in WSL) together hold ~14 GB of GPU 0
while they're up, whether or not anything is being read. So instead of leaving
them running:

- Every OCR call goes through in_use(engine), which starts that engine if it
  is down and waits until it answers its health check.
- The app runs an idle monitor (start_idle_monitor) that stops any engine that
  hasn't been used for OCR_IDLE_MINUTES (default 10; 0 turns stopping off).
  Stopping the process is the only way to get all of its GPU memory back,
  CUDA context included.

Last use is recorded as the modification time of a file per engine in
.ocr_activity/, so OCR run by the reindex scripts (separate processes) also
keeps an engine awake while the app is running.

Starting is slow (about a minute per engine, mostly model loading), so the
first page after an idle period waits for that; later pages don't.
"""

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ACTIVITY_DIR = ROOT / ".ocr_activity"
IDLE_SECONDS = float(os.environ.get("OCR_IDLE_MINUTES", "10")) * 60
# vLLM compiles and profiles on start; give a slow cold start plenty of room.
STARTUP_TIMEOUT = 900
CHECK_INTERVAL = 30

HUNYUAN_URL = os.environ.get("HUNYUAN_URL", "http://127.0.0.1:9000")
TONGGUOCR_URL = os.environ.get("TONGGUOCR_URL", "http://127.0.0.1:9100")
PADDLEOCR_URL = os.environ.get("PADDLEOCR_URL", "http://127.0.0.1:9200")
WSL_DISTRO = os.environ.get("TONGGUOCR_WSL_DISTRO", "Ubuntu")


def _healthy(url: str) -> bool:
    try:
        return requests.get(f"{url}/health", timeout=3).ok
    except requests.RequestException:
        return False


def _compose(compose_dir: str, *args: str) -> None:
    compose_file = ROOT / compose_dir / "docker-compose.yml"
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=STARTUP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed for {compose_dir}: {result.stderr.strip()[-500:]}")


def _wsl_path(path: Path) -> str:
    drive, rest = path.drive, path.as_posix()[len(path.drive):]
    return f"/mnt/{drive[0].lower()}{rest}" if drive else path.as_posix()


def _start_tongguocr() -> None:
    script = ROOT / "TongGuOCR" / "start_server.sh"
    ACTIVITY_DIR.mkdir(exist_ok=True)
    log = open(ACTIVITY_DIR / "tongguocr.log", "ab")
    if sys.platform == "win32":
        command = ["wsl", "-d", WSL_DISTRO, "--", "bash", _wsl_path(script)]
        # Detached, so the server outlives the app (or script) that started it
        # until the idle monitor shuts it down.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)
    else:
        subprocess.Popen(["bash", str(script)], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    log.close()


def _stop_tongguocr() -> None:
    # The server finishes any page in progress, then exits.
    requests.post(f"{TONGGUOCR_URL}/shutdown", timeout=600)


class Engine:
    def __init__(self, name, url, start, stop):
        self.name, self.url, self._start, self._stop = name, url, start, stop
        self.lock = threading.Lock()  # one start/stop decision at a time
        self.busy = 0  # OCR calls in flight in this process

    @property
    def activity_file(self) -> Path:
        return ACTIVITY_DIR / self.name

    def touch(self) -> None:
        ACTIVITY_DIR.mkdir(exist_ok=True)
        self.activity_file.touch()

    def idle_seconds(self) -> float:
        try:
            return time.time() - self.activity_file.stat().st_mtime
        except FileNotFoundError:
            # Never used since the monitor started: count from now, so an
            # engine that was already up gets a full idle period.
            self.touch()
            return 0.0

    def is_up(self) -> bool:
        return _healthy(self.url)

    def ensure_running(self) -> None:
        with self.lock:
            self.touch()
            if self.is_up():
                return
            # Start one engine at a time: each sizes its GPU memory from what's
            # free when it starts, so starting two at once can make one fail.
            with _START_LOCK:
                print(f"Starting {self.name}...")
                started = time.time()
                self._start()
                while not self.is_up():
                    if time.time() - started > STARTUP_TIMEOUT:
                        raise RuntimeError(f"{self.name} did not come up within {STARTUP_TIMEOUT}s")
                    time.sleep(2)
                print(f"{self.name} is up ({time.time() - started:.0f}s)")
            self.touch()

    def stop_if_idle(self) -> None:
        with self.lock:
            if self.busy or self.idle_seconds() < IDLE_SECONDS or not self.is_up():
                return
            print(f"Stopping {self.name} after {self.idle_seconds() / 60:.0f} idle minutes to free GPU memory")
            self._stop()


_START_LOCK = threading.Lock()

ENGINES = {
    "hunyuan": Engine("hunyuan", HUNYUAN_URL,
                      lambda: _compose("HunyuanOCR", "up", "-d"), lambda: _compose("HunyuanOCR", "stop")),
    "paddleocr": Engine("paddleocr", PADDLEOCR_URL,
                        lambda: _compose("PaddleOCRVL", "up", "-d"), lambda: _compose("PaddleOCRVL", "stop")),
    "tongguocr": Engine("tongguocr", TONGGUOCR_URL, _start_tongguocr, _stop_tongguocr),
}


def ensure_running(name: str) -> None:
    ENGINES[name].ensure_running()


def ensure_all() -> None:
    """Bring every engine up, one after another, before OCR runs them in parallel.

    HunyuanOCR first: its vLLM needs the largest contiguous share of free memory.
    """
    for name in ("hunyuan", "paddleocr", "tongguocr"):
        try:
            ENGINES[name].ensure_running()
        except Exception as exc:  # the OCR call itself reports the failure
            print(f"Could not start {name}: {exc}")


@contextmanager
def in_use(name: str):
    """Wrap one OCR request: starts the engine if needed and keeps it awake."""
    engine = ENGINES[name]
    engine.ensure_running()
    with engine.lock:
        engine.busy += 1
    try:
        yield
    finally:
        with engine.lock:
            engine.busy -= 1
        engine.touch()


def _monitor(stop_event: threading.Event) -> None:
    while not stop_event.wait(CHECK_INTERVAL):
        for engine in ENGINES.values():
            try:
                engine.stop_if_idle()
            except Exception as exc:
                print(f"Could not stop idle {engine.name}: {exc}")


def start_idle_monitor() -> threading.Event | None:
    """Stop idle engines in the background. Set the returned event to end it."""
    if IDLE_SECONDS <= 0:
        return None
    stop_event = threading.Event()
    threading.Thread(target=_monitor, args=(stop_event,), name="ocr-idle-monitor", daemon=True).start()
    return stop_event
