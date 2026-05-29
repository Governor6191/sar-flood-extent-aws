# Using the API

The API is async. Inference can outrun the gateway's 29 s limit, so you submit a
job, get a `job_id`, and poll for the result. Every call needs the `x-api-key`
header; the demo key allows 100 requests/day at 5 req/s.

Set your endpoint and key once:

```bash
export API="https://<api-id>.execute-api.us-east-1.amazonaws.com/demo"
export API_KEY="<your demo key>"
```

## 1. Get a presigned upload URL

```bash
curl -s -X POST "$API/uploads" -H "x-api-key: $API_KEY"
```

```json
{
  "upload_id": "0b8d38f0...",
  "input_key": "inputs/0b8d38f0.../scene.tif",
  "upload_url": "https://sar-flood-aws-<acct>.s3.amazonaws.com/inputs/...?X-Amz-...",
  "method": "PUT",
  "expires_in": 900
}
```

## 2. Upload the scene to that URL

A 2-band (VV, VH) Sentinel-1 GRD sigma0 dB GeoTIFF. Use a crop: the serverless
endpoint payload limit is about 4 MB.

```bash
curl -s -X PUT --upload-file scene.tif "<upload_url from step 1>"
```

No API key on this call. The URL itself is the credential.

## 3. Submit the job

```bash
curl -s -X POST "$API/infer" \
  -H "x-api-key: $API_KEY" -H "content-type: application/json" \
  -d '{"input_key": "inputs/0b8d38f0.../scene.tif"}'
```

```json
{ "job_id": "c2bc2160...", "status": "queued", "poll": "/infer/c2bc2160..." }
```

## 4. Poll for the result

```bash
curl -s "$API/infer/c2bc2160..." -H "x-api-key: $API_KEY"
```

While running:

```json
{ "job_id": "c2bc2160...", "status": "running" }
```

When done:

```json
{
  "job_id": "c2bc2160...",
  "status": "done",
  "water_fraction": 0.456871,
  "n_polygons": 25,
  "ms": 6941,
  "mask_url": "https://...s3...floodmask.tif?X-Amz-...",
  "geojson_url": "https://...s3...flood.geojson?X-Amz-..."
}
```

## 5. Download the outputs

```bash
curl -s "<mask_url>"    -o floodmask.tif      # single-band uint8 GeoTIFF (0 dry, 1 water)
curl -s "<geojson_url>" -o flood.geojson      # water polygons, EPSG:4326
```

The presigned output URLs are valid for one hour. Inputs and outputs are deleted
by an S3 lifecycle rule after 7 days.

## Status values

| status    | meaning                                         |
|-----------|-------------------------------------------------|
| `queued`  | job accepted, worker not started yet            |
| `running` | inference in progress                           |
| `done`    | outputs written, presigned URLs in the response |
| `failed`  | see the `error` field for the reason            |

## Errors

| HTTP | When                                                          |
|------|---------------------------------------------------------------|
| 403  | Missing or invalid API key, or the daily quota is exhausted   |
| 429  | Rate limit (5 req/s) exceeded                                 |
| 400  | `input_key` missing or not under the `inputs/` prefix         |
| 404  | Unknown `job_id`                                              |

A `failed` job with an "input too large" error means the scene exceeds the ~4 MB
serverless payload limit. Use a smaller crop.
