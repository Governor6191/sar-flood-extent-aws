"""Invoke the SageMaker serverless endpoint and report latency.

Sends a Sentinel-1 crop to the endpoint as a multipart upload (the same form the
container's /invocations route accepts) and prints the inference result plus the
round-trip latency. The first call is the cold start (the serverless container
has to spin up); the rest are warm. Use this to capture the Stage 2 latency
baseline.

Usage (from repo root):
    uv run python scripts/invoke_sm.py
    uv run python scripts/invoke_sm.py --runs 4 --crop tests/data/harvey_crop.tif
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

import boto3

BOUNDARY = "sarfloodextentboundary"


def build_multipart(file_path: str) -> bytes:
    """Encode a single GeoTIFF as a multipart/form-data body with field name 'file'."""
    fname = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        content = f.read()
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
        f"Content-Type: image/tiff\r\n\r\n"
    ).encode()
    tail = f"\r\n--{BOUNDARY}--\r\n".encode()
    return head + content + tail


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default="sar-flood-aws")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--crop", default="tests/data/harvey_crop.tif")
    p.add_argument("--runs", type=int, default=4, help="total calls; first is the cold start")
    args = p.parse_args()

    crop = Path(args.crop)
    if not crop.exists():
        raise SystemExit(f"missing crop payload: {crop}")
    body = build_multipart(str(crop))
    print(f"payload: {crop.name} ({len(body) / 1024:.0f} KiB multipart)")

    client = boto3.client("sagemaker-runtime", region_name=args.region)
    content_type = f"multipart/form-data; boundary={BOUNDARY}"

    latencies = []
    for i in range(args.runs):
        t0 = time.perf_counter()
        resp = client.invoke_endpoint(
            EndpointName=args.endpoint,
            ContentType=content_type,
            Accept="application/json",
            Body=body,
        )
        dt = time.perf_counter() - t0
        result = json.loads(resp["Body"].read())
        latencies.append(dt)
        kind = "cold" if i == 0 else f"warm {i}"
        print(
            f"[{kind:7}] {dt:6.2f} s  "
            f"water_fraction={result.get('water_fraction')}  "
            f"n_polygons={result.get('n_polygons')}  "
            f"server_ms={result.get('ms')}"
        )

    print("\n--- latency baseline ---")
    print(f"cold start          : {latencies[0]:.2f} s")
    warm = latencies[1:]
    if warm:
        print(f"warm calls          : {[round(x, 2) for x in warm]}")
        print(f"warm median         : {statistics.median(warm):.2f} s")
        print(f"warm min / max      : {min(warm):.2f} s / {max(warm):.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
