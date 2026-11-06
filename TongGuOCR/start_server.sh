#!/usr/bin/env bash
# Start the TongGuOCR server inside WSL. From Windows:
#   wsl -d Ubuntu -- bash "/mnt/c/.../Genealogy Tools/TongGuOCR/start_server.sh"
# Override TONGGUOCR_ROOT / TONGGUOCR_PYTHON / TONGGUOCR_PORT / CUDA_VISIBLE_DEVICES as needed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TONGGUOCR_ROOT="${TONGGUOCR_ROOT:-$HOME/TongGuOCR}"
PYTHON="${TONGGUOCR_PYTHON:-$HOME/mamba/envs/tongguocr/bin/python}"
# GPU 0 is the RTX 5060 Ti; keep the OCR off the display GPU.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
exec "$PYTHON" "$HERE/server.py"
