"""Drive a synthetic load through the API so the CloudWatch widgets have data.

Runs N full async jobs (presign, upload, submit, poll to done) against the live
API. Each job exercises API Gateway, the Lambda kicker (submit + worker), and the
SageMaker endpoint, so all dashboard widgets get populated. Reuses one uploaded
object across jobs to keep the request count (and the usage-plan quota) low.

Usage (from repo root):
    API_KEY=... uv run python scripts/synthetic_load.py --base-url <url> --jobs 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def _req(method: str, url: str, key: str, payload: dict | None = None) -> dict:
    headers = {"content-type": "application/json", "x-api-key": key}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--crop", default="tests/data/harvey_crop.tif")
    p.add_argument("--jobs", type=int, default=30)
    p.add_argument("--gap", type=float, default=12.0, help="seconds between job submits")
    args = p.parse_args()

    key = os.environ.get("API_KEY")
    if not key:
        sys.exit("set API_KEY")
    base = args.base_url.rstrip("/")
    body = Path(args.crop).read_bytes()

    # Upload the crop once, reuse the same input_key for every job.
    up = _req("POST", f"{base}/uploads", key, {})
    put = urllib.request.Request(up["upload_url"], data=body, method="PUT")
    with urllib.request.urlopen(put, timeout=60) as r:
        assert r.status in (200, 204)
    input_key = up["input_key"]

    done = 0
    latencies = []
    for i in range(args.jobs):
        t0 = time.perf_counter()
        sub = _req("POST", f"{base}/infer", key, {"input_key": input_key})
        job_id = sub["job_id"]
        status = "queued"
        for _ in range(40):
            st = _req("GET", f"{base}/infer/{job_id}", key)
            status = st["status"]
            if status in ("done", "failed"):
                break
            time.sleep(2)
        dt = time.perf_counter() - t0
        if status == "done":
            done += 1
            latencies.append(dt)
        print(f"[{i + 1:02}/{args.jobs}] {status:6} {dt:5.1f}s ms={st.get('ms')}")
        if i < args.jobs - 1:
            time.sleep(args.gap)

    print(f"\n{done}/{args.jobs} jobs done")
    if latencies:
        latencies.sort()
        print(f"round-trip p50 {latencies[len(latencies)//2]:.2f}s "
              f"min {latencies[0]:.2f}s max {latencies[-1]:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
