"""Container smoke test: reproduce the Harvey flood mask through the running image.

Assumes the container is up and serving on http://localhost:8000 with the data
and outputs directories mounted (see docker-compose.yml or the docker run command
in the README). Posts the full Hurricane Harvey 2017 scene to /infer, reads the
mask the container wrote, and diffs it against the reference mask in the research
repo. This is the Docker counterpart to scripts/validate_harvey_local.py: it
confirms the built image reproduces the local inference path, not just the code.

Usage (from repo root, container already running):
    uv run python scripts/smoke_test_container.py
    uv run python scripts/smoke_test_container.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import rasterio


def wait_for_health(base_url: str, timeout_s: float = 120.0) -> str:
    """Block until /health reports ok, return the model version it advertises."""
    import json

    deadline = time.perf_counter() + timeout_s
    last = ""
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=5) as r:
                body = json.loads(r.read())
            if body.get("status") == "ok":
                return body.get("model_version", "")
            last = body.get("status", "")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = str(e)
        time.sleep(2)
    sys.exit(f"container did not become healthy within {timeout_s:.0f}s (last: {last})")


def post_infer(base_url: str, scene_uri: str) -> dict:
    import json

    payload = json.dumps({"scene_uri": scene_uri}).encode()
    req = urllib.request.Request(
        f"{base_url}/infer",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument(
        "--scene-uri",
        default="file:///data/harvey_s1_houston_2017-08-30.tif",
        help="scene path as the container sees it (data/ is mounted at /data)",
    )
    p.add_argument("--ref-repo", default=r"C:/Users/Sylve/Desktop/sar-flood-extent")
    p.add_argument("--outputs-dir", default="outputs", help="host dir mounted at /tmp/outputs")
    p.add_argument("--tol", type=float, default=0.005, help="max allowed disagreement fraction")
    args = p.parse_args()

    ref_mask_path = (
        Path(args.ref_repo)
        / "outputs"
        / "predictions"
        / "harvey_s1_houston_2017-08-30_floodmask.tif"
    )
    if not ref_mask_path.exists():
        sys.exit(f"missing reference mask: {ref_mask_path}")

    print("waiting for container health ...")
    version = wait_for_health(args.base_url)
    print(f"container healthy, model_version={version}")

    print(f"posting {args.scene_uri} to /infer ...")
    t0 = time.perf_counter()
    resp = post_infer(args.base_url, args.scene_uri)
    round_trip = time.perf_counter() - t0

    mask_name = Path(resp["mask_uri"]).name
    host_mask = Path(args.outputs_dir) / mask_name
    if not host_mask.exists():
        sys.exit(
            f"container wrote {resp['mask_uri']} but {host_mask} is not on the host. "
            "Mount the outputs volume: -v <repo>/outputs:/tmp/outputs"
        )

    with rasterio.open(host_mask) as m:
        mine = m.read(1)
    with rasterio.open(ref_mask_path) as m:
        ref_mask = m.read(1)

    if mine.shape != ref_mask.shape:
        sys.exit(f"shape mismatch: container {mine.shape} vs ref {ref_mask.shape}")

    diff = int((mine != ref_mask).sum())
    total = mine.size
    disagree = diff / total
    inter = int(((mine == 1) & (ref_mask == 1)).sum())
    union = int(((mine == 1) | (ref_mask == 1)).sum())
    iou = inter / union if union else 1.0

    print("\n--- Harvey container smoke test ---")
    print(f"round-trip wall-clock     : {round_trip:.1f} s")
    print(f"server-side inference      : {resp.get('ms', 0) / 1000:.1f} s")
    print(f"mask shape                : {mine.shape}")
    print(f"water fraction (container): {resp.get('water_fraction')}")
    print(f"water fraction (reference): {(ref_mask == 1).mean():.6f}")
    print(f"polygons (container)      : {resp.get('n_polygons')}")
    print(f"pixel agreement           : {100 * (1 - disagree):.4f}%  ({diff:,} of {total:,} differ)")
    print(f"water-class IoU vs ref    : {iou:.6f}")

    if disagree <= args.tol:
        print(f"\nPASS: disagreement {disagree:.6f} <= tolerance {args.tol}")
        return 0
    print(f"\nFAIL: disagreement {disagree:.6f} > tolerance {args.tol}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
