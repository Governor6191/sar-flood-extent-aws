"""Local validation: reproduce the Harvey flood mask through the deployment path.

Runs the packaged inference core on the Hurricane Harvey 2017 scene and diffs the
result against the reference mask committed in the research repo. This is the
Stage 1 smoke test (no Docker, no AWS): if the deployment path agrees with the
local path within tolerance, the container will too, since it runs the same code.

Usage (from repo root):
    uv run python scripts/validate_harvey_local.py
    uv run python scripts/validate_harvey_local.py --ref-repo ../sar-flood-extent
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.inference import load_constants, load_model, predict, write_mask


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ref-repo", default=r"C:/Users/Sylve/Desktop/sar-flood-extent")
    p.add_argument("--tol", type=float, default=0.005, help="max allowed disagreement fraction")
    args = p.parse_args()

    ref = Path(args.ref_repo)
    scene = ref / "data" / "harvey" / "harvey_s1_houston_2017-08-30.tif"
    ref_mask_path = ref / "outputs" / "predictions" / "harvey_s1_houston_2017-08-30_floodmask.tif"
    checkpoint = ref / "checkpoints" / "unet_resnet34_all_best.pt"

    for path in (scene, ref_mask_path):
        if not path.exists():
            sys.exit(f"missing required file: {path}")

    # Use the local checkpoint if present (identical to the released model.pt),
    # otherwise load_model falls back to the HF Hub.
    local_ckpt = str(checkpoint) if checkpoint.exists() else None
    print(f"loading model ({'local checkpoint' if local_ckpt else 'HF Hub'}) ...")
    model = load_model(local_path=local_ckpt, device="cpu")
    constants = load_constants()

    print("running inference on the full Harvey scene ...")
    t0 = time.perf_counter()
    result = predict(str(scene), model, constants, device="cpu", want_geojson=True)
    elapsed = time.perf_counter() - t0

    out_path = Path("outputs") / "harvey_s1_houston_2017-08-30_floodmask.tif"
    write_mask(result.mask, result.profile, out_path)

    with rasterio.open(ref_mask_path) as m:
        ref_mask = m.read(1)

    if result.mask.shape != ref_mask.shape:
        sys.exit(f"shape mismatch: mine {result.mask.shape} vs ref {ref_mask.shape}")

    mine = result.mask
    diff = int((mine != ref_mask).sum())
    total = mine.size
    disagree = diff / total
    inter = int(((mine == 1) & (ref_mask == 1)).sum())
    union = int(((mine == 1) | (ref_mask == 1)).sum())
    iou = inter / union if union else 1.0

    print("\n--- Harvey smoke test ---")
    print(f"inference wall-clock      : {elapsed:.1f} s (CPU)")
    print(f"mask shape                : {mine.shape}")
    print(f"water fraction (mine)     : {(mine == 1).mean():.6f}")
    print(f"water fraction (reference): {(ref_mask == 1).mean():.6f}")
    print(f"polygons (mine)           : {result.n_polygons}")
    print(f"pixel agreement           : {100 * (1 - disagree):.4f}%  ({diff:,} of {total:,} differ)")
    print(f"water-class IoU vs ref    : {iou:.6f}")

    if disagree <= args.tol:
        print(f"\nPASS: disagreement {disagree:.6f} <= tolerance {args.tol}")
        return 0
    print(f"\nFAIL: disagreement {disagree:.6f} > tolerance {args.tol}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
