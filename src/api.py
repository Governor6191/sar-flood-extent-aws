"""FastAPI service that wraps the SAR flood-extent model.

Routes:
  GET  /health       liveness + model version (Stage 1 contract)
  POST /infer        run inference on a scene, return mask URI + GeoJSON
  GET  /ping         SageMaker liveness probe (200 only when the model is loaded)
  POST /invocations  SageMaker inference entrypoint (same JSON contract as /infer)

The model loads once at startup. In the container the weights are baked in and
MODEL_LOCAL_PATH points at them, so startup is fast and needs no network. For
local dev without baked weights the model is pulled from the public HF Hub repo.
"""
from __future__ import annotations

import base64
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.inference import (
    MODEL_VERSION,
    load_constants,
    load_model,
    mask_to_geotiff_bytes,
    predict,
    write_mask,
)

DEVICE = os.environ.get("DEVICE", "cpu")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./outputs"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(800 * 1024 * 1024)))

STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE["constants"] = load_constants()
    STATE["model"] = load_model(device=DEVICE)
    STATE["ready"] = True
    yield
    STATE.clear()


app = FastAPI(title="sar-flood-aws", version="0.1.0", lifespan=lifespan)


def _run(scene_uri: str, want_geojson: bool, name_hint: str, mask_as_bytes: bool = False) -> dict:
    t0 = time.perf_counter()
    result = predict(
        scene_uri,
        STATE["model"],
        STATE["constants"],
        device=DEVICE,
        want_geojson=want_geojson,
    )
    out = {
        "water_fraction": round(result.water_fraction, 6),
        "n_polygons": result.n_polygons,
        "geojson": result.geojson,
        "ms": int((time.perf_counter() - t0) * 1000),
        "model_version": MODEL_VERSION,
    }
    if mask_as_bytes:
        # SageMaker path: return the mask GeoTIFF inline so the kicker can write it
        # to S3. base64 keeps it JSON-safe; masks compress well so this stays small.
        out["mask_b64"] = base64.b64encode(
            mask_to_geotiff_bytes(result.mask, result.profile)
        ).decode("ascii")
    else:
        # Local path: write the mask to disk and return its path.
        stem = Path(name_hint).stem or "scene"
        out_mask = OUTPUT_DIR / f"{stem}_{uuid.uuid4().hex[:8]}_floodmask.tif"
        out["mask_uri"] = write_mask(result.mask, result.profile, out_mask)
    return out


async def _handle_infer(request: Request, mask_as_bytes: bool = False) -> JSONResponse:
    if not STATE.get("ready"):
        return JSONResponse({"error": "model not loaded"}, status_code=503)

    ctype = request.headers.get("content-type", "")
    want_geojson = request.query_params.get("geojson", "true").lower() != "false"

    try:
        if ctype.startswith("application/json"):
            body = await request.json()
            scene_uri = body.get("scene_uri")
            if not scene_uri:
                return JSONResponse({"error": "missing scene_uri"}, status_code=400)
            return JSONResponse(_run(scene_uri, want_geojson, scene_uri, mask_as_bytes))

        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None:
                return JSONResponse({"error": "missing file field"}, status_code=400)
            data = await upload.read()
            if len(data) > MAX_UPLOAD_BYTES:
                return JSONResponse({"error": "upload exceeds max size"}, status_code=413)
            suffix = Path(upload.filename or "scene.tif").suffix or ".tif"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                name = upload.filename or "scene"
                return JSONResponse(_run(tmp_path, want_geojson, name, mask_as_bytes))
            finally:
                os.unlink(tmp_path)

        return JSONResponse(
            {"error": "send application/json {scene_uri} or multipart file"},
            status_code=415,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)


@app.get("/health")
async def health():
    return {"status": "ok" if STATE.get("ready") else "loading", "model_version": MODEL_VERSION}


@app.post("/infer")
async def infer(request: Request):
    return await _handle_infer(request)


@app.get("/ping")
async def ping():
    return JSONResponse({}, status_code=200 if STATE.get("ready") else 503)


@app.post("/invocations")
async def invocations(request: Request):
    # SageMaker entrypoint: return the mask bytes inline (the kicker writes them to S3).
    return await _handle_infer(request, mask_as_bytes=True)
