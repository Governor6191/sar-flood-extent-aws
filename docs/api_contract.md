# Inference API contract

The container exposes a small HTTP API around the SAR flood-extent model. This is
the Stage 1 local contract. The public AWS endpoint (API Gateway) adds an async
`job_id` layer on top of this in a later stage; the container itself stays
synchronous.

## Routes

| Method | Path           | Purpose                                            |
|--------|----------------|----------------------------------------------------|
| GET    | `/health`      | Liveness and model version                         |
| POST   | `/infer`       | Run inference on one scene                         |
| GET    | `/ping`        | SageMaker liveness probe (200 when model is ready) |
| POST   | `/invocations` | SageMaker inference entrypoint (same as `/infer`)  |

`/ping` and `/invocations` exist so the same image loads unmodified as a SageMaker
container in Stage 2. The exact S3 output wiring for `/invocations` is finalized
when the Lambda kicker lands in Stage 3.

## GET /health

Response `200`:

```json
{ "status": "ok", "model_version": "unet-resnet34-sen1floods11-all" }
```

`status` is `"loading"` until the model finishes loading at startup.

## POST /infer

Two input forms.

**JSON body** (a scene already reachable by the server):

```json
{ "scene_uri": "file:///data/harvey_s1_houston_2017-08-30.tif" }
```

`scene_uri` accepts `file://`, a plain path, `s3://bucket/key`, or
`https://host/scene.tif`. `s3://` and `https://` are read through GDAL virtual
file systems and need the matching access (AWS credentials for `s3://`).

**Multipart upload** (caller sends the GeoTIFF bytes):

```
POST /infer
Content-Type: multipart/form-data
file=<scene.tif>
```

Upload size is capped by `MAX_UPLOAD_BYTES` (default 800 MB).

Optional query param `?geojson=false` skips polygon vectorization (faster, smaller
response) and returns `geojson: null`.

### Input requirements

- GeoTIFF, at least 2 bands. Band 1 is VV, band 2 is VH, both Sentinel-1 GRD
  sigma0 in decibels (the `COPERNICUS/S1_GRD` representation). Extra bands are
  ignored.
- Any size. The server tiles to 512x512, pads partial edge tiles, and stitches.
- The model standardizes per channel with the bundled Sen1Floods11 constants
  before inference, matching the local reference path exactly.

### Response `200`

```json
{
  "mask_uri": "/tmp/outputs/harvey_..._floodmask.tif",
  "water_fraction": 0.0123,
  "n_polygons": 412,
  "geojson": "{\"type\":\"FeatureCollection\", ...}",
  "ms": 48213,
  "model_version": "unet-resnet34-sen1floods11-all"
}
```

- `mask_uri`: path to the written single-band uint8 GeoTIFF (0 dry, 1 water),
  LZW-compressed, same georeferencing as the input.
- `water_fraction`: fraction of pixels classified water.
- `n_polygons`: number of water polygons in the GeoJSON (EPSG:4326).
- `geojson`: FeatureCollection string, or `null` when `?geojson=false`.
- `ms`: server-side wall-clock for the inference.

### Errors

| Status | When                                                       |
|--------|------------------------------------------------------------|
| 400    | Missing `scene_uri` (JSON) or missing `file` (multipart)   |
| 413    | Upload exceeds `MAX_UPLOAD_BYTES`                          |
| 415    | Content-Type is neither JSON nor multipart                |
| 422    | Scene has fewer than 2 bands, or is unreadable             |
| 503    | Model not loaded yet                                       |

## Validation reference

The smoke test for Stage 1 runs the full Hurricane Harvey 2017 Houston scene
(`harvey_s1_houston_2017-08-30.tif`) through `/infer` and diffs the returned mask
against the committed reference mask from the research repo
(`sar-flood-extent/outputs/predictions/harvey_s1_houston_2017-08-30_floodmask.tif`).
The two must match within numerical tolerance, confirming the deployment path
reproduces the local inference path.
