# Lambda kicker

`kicker/handler.py` is the async API backend. It has no third-party
dependencies: it uses only `boto3`, which is already in the Lambda Python 3.11
runtime. So packaging is just zipping the one file, no `uv` build or layer
needed.

## Package

```bash
cd lambda/kicker
zip -j ../kicker.zip handler.py
```

`-j` flattens the path so the handler lands at the zip root, which is what the
Lambda handler reference `handler.handler` expects.

## Environment variables

| Var               | Value                              |
|-------------------|------------------------------------|
| `BUCKET`          | `sar-flood-aws-<account>`          |
| `TABLE`           | `sar-flood-aws-jobs`               |
| `ENDPOINT`        | `sar-flood-aws` (SageMaker)        |
| `TTL_DAYS`        | `7` (matches the S3 lifecycle)     |
| `MAX_INPUT_BYTES` | `3500000` (serverless payload cap) |

`AWS_LAMBDA_FUNCTION_NAME` is set by the runtime and is used for the async
self-invoke; it does not need to be set by hand.

## Operations

- `POST /uploads` -> `presign_upload`: presigned S3 PUT URL for the input scene.
- `POST /infer` -> `submit`: writes a `queued` job, async-invokes the worker, returns `job_id`.
- `GET /infer/{job_id}` -> `get_status`: status, plus presigned output URLs when `done`.
- async `{"op": "process"}` -> `process`: forwards the scene to SageMaker, writes
  the mask GeoTIFF and GeoJSON to S3, flips the job to `done` (or `failed`).

The API key is enforced by the API Gateway usage plan, so the handler has no auth
code. In Stage 4 the CDK app builds this function from this directory.
