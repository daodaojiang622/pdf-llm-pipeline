"""Download the MinerU (magic-pdf 1.2.2) models into the image at build time.

Lambda can only write to /tmp, so every model must be on disk before the
function runs. Revisions are pinned so rebuilds get the same weights.
Paths must match magic-pdf.json next to this script.

Table recognition and the LayoutLMv3 layout model are disabled in
magic-pdf.json, so their weights are not downloaded.
"""

import os
import shutil

from huggingface_hub import snapshot_download

MINERU_DIR = os.environ.get("MINERU_DIR", "/opt/mineru")

PDF_EXTRACT_KIT = {
    "repo_id": "opendatalab/PDF-Extract-Kit-1.0",
    "revision": "ed6b654c018d742e65a17671e379c5e6ecc87ec9",
    "allow_patterns": [
        "models/Layout/YOLO/doclayout_yolo_docstructbench_imgsz1280_2501.pt",
        "models/MFD/YOLO/yolo_v8_ft.pt",
        "models/MFR/unimernet_small_2501/*",
    ],
    "local_dir": os.path.join(MINERU_DIR, "pdf-extract-kit"),
}

# Reading-order model. MinerU downloads it at runtime if it is missing.
LAYOUTREADER = {
    "repo_id": "hantian/layoutreader",
    "revision": "629be376d86fbab624ddc4020804a4e93b5515bc",
    "allow_patterns": ["*.json", "*.safetensors"],
    "local_dir": os.path.join(MINERU_DIR, "layoutreader"),
}


def download(spec):
    path = snapshot_download(**spec)
    # Download metadata is not needed at runtime
    shutil.rmtree(os.path.join(path, ".cache"), ignore_errors=True)
    print(f"Downloaded {spec['repo_id']}@{spec['revision'][:10]} to {path}")


if __name__ == "__main__":
    download(PDF_EXTRACT_KIT)
    download(LAYOUTREADER)
