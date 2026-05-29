"""Render the CloudWatch dashboard panels to a single PNG for the README.

Uses the CloudWatch GetMetricWidgetImage API (no browser needed) to render each
of the four dashboard panels from live metrics, then montages them into a 2x2
grid. Run after a synthetic load so the widgets have data.

Usage (from repo root):
    uv run --with pillow python scripts/make_dashboard_image.py
"""
from __future__ import annotations

import io
import json

import boto3
from PIL import Image

REGION = "us-east-1"
ENDPOINT = "sar-flood-aws"
FUNCTION = "sar-flood-aws-kicker"
OUT = "docs/figures/cloudwatch_dashboard.png"

cw = boto3.client("cloudwatch", region_name=REGION)


def widget(title: str, metrics: list) -> dict:
    return {
        "title": title,
        "view": "timeSeries",
        "stacked": False,
        "width": 760,
        "height": 320,
        "start": "-PT1H",
        "end": "P0D",
        "region": REGION,
        "period": 60,
        "metrics": metrics,
    }


PANELS = [
    widget(
        "API Gateway requests",
        [
            ["AWS/ApiGateway", "Count", "ApiName", "sar-flood-aws", "Stage", "demo", {"stat": "Sum", "label": "requests"}],
            ["AWS/ApiGateway", "4XXError", "ApiName", "sar-flood-aws", "Stage", "demo", {"stat": "Sum", "label": "4XX"}],
            ["AWS/ApiGateway", "5XXError", "ApiName", "sar-flood-aws", "Stage", "demo", {"stat": "Sum", "label": "5XX"}],
        ],
    ),
    widget(
        "API Gateway latency (ms)",
        [
            ["AWS/ApiGateway", "Latency", "ApiName", "sar-flood-aws", "Stage", "demo", {"stat": "p50", "label": "p50"}],
            ["AWS/ApiGateway", "Latency", "ApiName", "sar-flood-aws", "Stage", "demo", {"stat": "p95", "label": "p95"}],
            ["AWS/ApiGateway", "Latency", "ApiName", "sar-flood-aws", "Stage", "demo", {"stat": "p99", "label": "p99"}],
        ],
    ),
    widget(
        "Lambda kicker",
        [
            ["AWS/Lambda", "Invocations", "FunctionName", FUNCTION, {"stat": "Sum", "label": "invocations"}],
            ["AWS/Lambda", "Errors", "FunctionName", FUNCTION, {"stat": "Sum", "label": "errors"}],
            ["AWS/Lambda", "Duration", "FunctionName", FUNCTION, {"stat": "Average", "label": "avg duration (ms)", "yAxis": "right"}],
        ],
    ),
    widget(
        "SageMaker serverless endpoint",
        [
            ["AWS/SageMaker", "Invocations", "EndpointName", ENDPOINT, "VariantName", "AllTraffic", {"stat": "Sum", "label": "invocations"}],
            ["AWS/SageMaker", "Invocation5XXErrors", "EndpointName", ENDPOINT, "VariantName", "AllTraffic", {"stat": "Sum", "label": "5XX"}],
            ["AWS/SageMaker", "ModelLatency", "EndpointName", ENDPOINT, "VariantName", "AllTraffic", {"stat": "Average", "label": "model latency (us)", "yAxis": "right"}],
        ],
    ),
]


def main() -> int:
    imgs = []
    for w in PANELS:
        resp = cw.get_metric_widget_image(MetricWidget=json.dumps(w))
        imgs.append(Image.open(io.BytesIO(resp["MetricWidgetImage"])).convert("RGB"))
    cell_w = max(im.width for im in imgs)
    cell_h = max(im.height for im in imgs)
    grid = Image.new("RGB", (cell_w * 2, cell_h * 2), "white")
    for im, (cx, cy) in zip(imgs, [(0, 0), (1, 0), (0, 1), (1, 1)]):
        grid.paste(im, (cx * cell_w, cy * cell_h))
    grid.save(OUT)
    print(f"wrote {OUT} ({grid.width}x{grid.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
