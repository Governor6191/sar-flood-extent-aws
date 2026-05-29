"""End-to-end test of the async API: presign, upload, infer, poll, download, diff.

Walks the full caller flow against the live REST API:

  1. POST /uploads               -> presigned S3 PUT URL
  2. PUT the crop to that URL
  3. POST /infer {input_key}      -> job_id
  4. poll GET /infer/{job_id}     until status == done
  5. download the mask via the presigned URL
  6. diff it against the same crop run through the local inference path

The API key is read from the API_KEY environment variable so it never lands in
the repo. The local reference uses the research-repo checkpoint if present (no
model download), otherwise the inference core falls back to the HF Hub.

Usage (from repo root):
    API_KEY=... uv run python scripts/e2e_test.py
    API_KEY=... uv run python scripts/e2e_test.py --base-url https://<api>.execute-api.us-east-1.amazonaws.com/demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _req(method: str, url: str, key: str | None, payload: dict | None = None) -> dict:
    headers = {"content-type": "application/json"}
    if key:
        headers["x-api-key"] = key
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True, help="API invoke URL incl. stage")
    p.add_argument("--crop", default="tests/data/harvey_crop.tif")
    p.add_argument("--ref-repo", default=r"C:/Users/Sylve/Desktop/sar-flood-extent")
    p.add_argument("--timeout", type=int, default=180, help="seconds to wait for done")
    p.add_argument("--tol", type=float, default=0.005)
    args = p.parse_args()

    key = os.environ.get("API_KEY")
    if not key:
        sys.exit("set API_KEY in the environment")
    crop = Path(args.crop)
    if not crop.exists():
        sys.exit(f"missing crop: {crop}")
    base = args.base_url.rstrip("/")

    # 1. presign
    up = _req("POST", f"{base}/uploads", key, {})
    print(f"1. presigned upload -> {up['input_key']}")

    # 2. upload the crop to the presigned URL (no api key, no content-type)
    body = crop.read_bytes()
    put = urllib.request.Request(up["upload_url"], data=body, method="PUT")
    with urllib.request.urlopen(put, timeout=60) as r:
        if r.status not in (200, 204):
            sys.exit(f"upload failed: HTTP {r.status}")
    print(f"2. uploaded {len(body)} bytes")

    # 3. submit
    sub = _req("POST", f"{base}/infer", key, {"input_key": up["input_key"]})
    job_id = sub["job_id"]
    print(f"3. submitted job {job_id} (status {sub['status']})")

    # 4. poll
    deadline = time.time() + args.timeout
    status = None
    while time.time() < deadline:
        st = _req("GET", f"{base}/infer/{job_id}", key)
        status = st["status"]
        if status in ("done", "failed"):
            break
        time.sleep(3)
    print(f"4. final status: {status}")
    if status != "done":
        sys.exit(f"job did not finish: {json.dumps(st)}")
    print(
        f"   api reports water_fraction={st.get('water_fraction')} "
        f"n_polygons={st.get('n_polygons')} ms={st.get('ms')}"
    )

    # 5. download the mask
    with urllib.request.urlopen(st["mask_url"], timeout=60) as r:
        mask_bytes = r.read()
    out_path = Path("outputs") / f"e2e_{job_id[:8]}_floodmask.tif"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(mask_bytes)
    with rasterio.open(out_path) as m:
        api_mask = m.read(1)
    print(f"5. downloaded mask {api_mask.shape} -> {out_path}")

    # 6. local reference for the same crop
    from src.inference import load_constants, load_model, predict

    ckpt = Path(args.ref_repo) / "checkpoints" / "unet_resnet34_all_best.pt"
    local_ckpt = str(ckpt) if ckpt.exists() else None
    model = load_model(local_path=local_ckpt, device="cpu")
    result = predict(str(crop), model, load_constants(), device="cpu", want_geojson=False)
    local_mask = result.mask

    if api_mask.shape != local_mask.shape:
        sys.exit(f"shape mismatch: api {api_mask.shape} vs local {local_mask.shape}")
    diff = int((api_mask != local_mask).sum())
    total = api_mask.size
    disagree = diff / total
    inter = int(((api_mask == 1) & (local_mask == 1)).sum())
    union = int(((api_mask == 1) | (local_mask == 1)).sum())
    iou = inter / union if union else 1.0
    print("\n--- end-to-end mask diff (API vs local) ---")
    print(f"pixel agreement : {100 * (1 - disagree):.4f}%  ({diff} of {total} differ)")
    print(f"water-class IoU : {iou:.6f}")

    if disagree <= args.tol:
        print(f"\nPASS: async API reproduces the local mask (disagreement {disagree:.6f})")
        return 0
    print(f"\nFAIL: disagreement {disagree:.6f} > tol {args.tol}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
