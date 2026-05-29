"""Lambda kicker for the SAR flood-extent async API.

One function, four operations:

  POST /uploads          presign_upload  -> presigned S3 PUT URL for the input scene
  POST /infer            submit          -> create a job, kick off processing, return job_id
  GET  /infer/{job_id}   get_status      -> job status, presigned output URLs when done
  (async self-invoke)    process         -> run inference, write outputs to S3

The API is async because inference can outrun API Gateway's 29 s limit. `submit`
writes a `queued` job to DynamoDB and invokes this same function asynchronously
with `{"op": "process", ...}`; that invocation forwards the scene to the SageMaker
serverless endpoint, writes the mask GeoTIFF and GeoJSON to S3, and flips the job
to `done`. The caller polls `get_status` until then.

The API key is enforced by the API Gateway usage plan, so there is no auth code
here. IAM is least-privilege: this bucket, this table, this endpoint, this
function (for the self-invoke) only.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from decimal import Decimal

import boto3
from botocore.config import Config

BUCKET = os.environ["BUCKET"]
TABLE = os.environ["TABLE"]
ENDPOINT = os.environ["ENDPOINT"]
SELF_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
TTL_DAYS = int(os.environ.get("TTL_DAYS", "7"))
UPLOAD_EXPIRY = int(os.environ.get("UPLOAD_EXPIRY", "900"))
OUTPUT_EXPIRY = int(os.environ.get("OUTPUT_EXPIRY", "3600"))
# SageMaker serverless caps the request payload at ~4 MB; keep a margin.
MAX_INPUT_BYTES = int(os.environ.get("MAX_INPUT_BYTES", str(3_500_000)))
# Hard cap on what S3 accepts on a presigned upload (5 MB). This is the
# infrastructure ceiling; the smaller MAX_INPUT_BYTES above is the inference
# limit, surfaced as a job failure for anything between the two.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
# Only the portfolio origin may read these responses in the browser.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://governor6191.github.io")
BOUNDARY = "sarfloodextentboundary"

# Force SigV4 so presigned URLs sign only the host. A SigV2 presign would fold
# the client's Content-Type into the signature, and clients (curl, urllib) add
# one by default, which breaks the upload with SignatureDoesNotMatch.
s3 = boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE)
smr = boto3.client("sagemaker-runtime")
lam = boto3.client("lambda")


# --- helpers ---------------------------------------------------------------

def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            # API Gateway adds CORS headers only to the MOCK preflight; a Lambda
            # proxy response has to carry its own so the browser can read it.
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        },
        "body": json.dumps(body),
    }


def _jsonable(obj):
    """Recursively turn DynamoDB Decimals into ints/floats for JSON output."""
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def _to_ddb(obj):
    """Convert floats to Decimal so the DynamoDB resource accepts the item."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def _multipart(data: bytes, filename: str = "scene.tif") -> bytes:
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: image/tiff\r\n\r\n"
    ).encode()
    return head + data + f"\r\n--{BOUNDARY}--\r\n".encode()


# --- operations ------------------------------------------------------------

def presign_upload() -> dict:
    upload_id = uuid.uuid4().hex
    input_key = f"inputs/{upload_id}/scene.tif"
    # A presigned POST (not PUT) so the policy can carry a content-length-range.
    # S3 rejects anything outside [1, MAX_UPLOAD_BYTES] before it is stored, so a
    # caller cannot push a large payload even with a valid URL. A PUT presign has
    # no way to bound the size; only a POST policy does. The caller sends a
    # multipart/form-data POST with these fields plus the file (file field last).
    post = s3.generate_presigned_post(
        Bucket=BUCKET,
        Key=input_key,
        Conditions=[["content-length-range", 1, MAX_UPLOAD_BYTES]],
        ExpiresIn=UPLOAD_EXPIRY,
    )
    return _resp(
        200,
        {
            "upload_id": upload_id,
            "input_key": input_key,
            "upload_url": post["url"],
            "upload_fields": post["fields"],
            "method": "POST",
            "max_bytes": MAX_UPLOAD_BYTES,
            "expires_in": UPLOAD_EXPIRY,
        },
    )


def submit(body: dict) -> dict:
    input_key = body.get("input_key")
    scene_uri = body.get("scene_uri")
    if not input_key and scene_uri and scene_uri.startswith(f"s3://{BUCKET}/"):
        input_key = scene_uri[len(f"s3://{BUCKET}/"):]
    if not input_key:
        return _resp(400, {"error": "provide input_key (from POST /uploads) or a scene_uri in this bucket"})
    if not input_key.startswith("inputs/"):
        return _resp(400, {"error": "input_key must be under the inputs/ prefix"})

    job_id = uuid.uuid4().hex
    now = int(time.time())
    table.put_item(
        Item={
            "job_id": job_id,
            "status": "queued",
            "input_key": input_key,
            "created_at": now,
            "expires_at": now + TTL_DAYS * 86400,
        }
    )
    lam.invoke(
        FunctionName=SELF_NAME,
        InvocationType="Event",
        Payload=json.dumps({"op": "process", "job_id": job_id, "input_key": input_key}).encode(),
    )
    return _resp(202, {"job_id": job_id, "status": "queued", "poll": f"/infer/{job_id}"})


def get_status(job_id: str) -> dict:
    item = table.get_item(Key={"job_id": job_id}).get("Item")
    if not item:
        return _resp(404, {"error": "unknown job_id"})
    status = item["status"]
    out = {"job_id": job_id, "status": status}
    if status == "failed":
        out["error"] = item.get("error", "unknown error")
    elif status == "done":
        out["water_fraction"] = _jsonable(item.get("water_fraction"))
        out["n_polygons"] = _jsonable(item.get("n_polygons"))
        out["ms"] = _jsonable(item.get("ms"))
        out["mask_url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": item["mask_key"]},
            ExpiresIn=OUTPUT_EXPIRY,
        )
        if item.get("geojson_key"):
            out["geojson_url"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET, "Key": item["geojson_key"]},
                ExpiresIn=OUTPUT_EXPIRY,
            )
    return _resp(200, out)


def process(event: dict) -> dict:
    job_id = event["job_id"]
    input_key = event["input_key"]
    try:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "running"},
        )
        data = s3.get_object(Bucket=BUCKET, Key=input_key)["Body"].read()
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(
                f"input is {len(data)} bytes; the serverless endpoint payload limit "
                f"is ~4 MB. Use a smaller Sentinel-1 crop."
            )

        resp = smr.invoke_endpoint(
            EndpointName=ENDPOINT,
            ContentType=f"multipart/form-data; boundary={BOUNDARY}",
            Accept="application/json",
            Body=_multipart(data),
        )
        result = json.loads(resp["Body"].read())

        mask_key = f"outputs/{job_id}/floodmask.tif"
        s3.put_object(
            Bucket=BUCKET,
            Key=mask_key,
            Body=base64.b64decode(result["mask_b64"]),
            ContentType="image/tiff",
        )
        geojson_key = None
        if result.get("geojson"):
            geojson_key = f"outputs/{job_id}/flood.geojson"
            s3.put_object(
                Bucket=BUCKET,
                Key=geojson_key,
                Body=result["geojson"].encode(),
                ContentType="application/geo+json",
            )

        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #s = :s, mask_key = :m, geojson_key = :g, "
                "water_fraction = :w, n_polygons = :n, ms = :ms, finished_at = :f"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=_to_ddb(
                {
                    ":s": "done",
                    ":m": mask_key,
                    ":g": geojson_key,
                    ":w": result.get("water_fraction"),
                    ":n": result.get("n_polygons"),
                    ":ms": result.get("ms"),
                    ":f": int(time.time()),
                }
            ),
        )
        return {"ok": True, "job_id": job_id}
    except Exception as e:  # noqa: BLE001 - record the failure, do not retry
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, #e = :e",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "failed", ":e": str(e)},
        )
        return {"ok": False, "job_id": job_id, "error": str(e)}


# --- entrypoint ------------------------------------------------------------

def handler(event, context):
    # Async self-invoke (the processing worker).
    if event.get("op") == "process":
        return process(event)

    # API Gateway REST proxy event.
    method = event.get("httpMethod")
    resource = event.get("resource")
    if method == "POST" and resource == "/uploads":
        return presign_upload()
    if method == "POST" and resource == "/infer":
        body = json.loads(event.get("body") or "{}")
        return submit(body)
    if method == "GET" and resource == "/infer/{job_id}":
        return get_status(event["pathParameters"]["job_id"])
    return _resp(404, {"error": "not found"})
