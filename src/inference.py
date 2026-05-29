"""Inference core for the SAR flood-extent model, packaged for deployment.

This mirrors the local reference path in sar-flood-extent/scripts/infer_scene.py:
read a 2-band (VV, VH) sigma0 dB GeoTIFF, standardize it per channel with the
recovered Sen1Floods11 constants, tile to 512x512, run the U-Net per tile, stitch
the masks, and return a uint8 water mask plus an optional flood-polygon GeoJSON.

The model is the released checkpoint Governor6191/sar-flood-extent-unet-resnet34
(file model.pt). It loads byte-for-byte the same weights the local script uses.
The container caches the weights at build time so the first request is not slow.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# GDAL tuning for remote reads (s3:// / http(s):// via /vsicurl). Harmless locally.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff")

import numpy as np
import rasterio
import segmentation_models_pytorch as smp
import torch
import torch.nn as nn
from rasterio import features
from rasterio.warp import transform_geom

HERE = Path(__file__).resolve().parent
CONSTANTS_PATH = HERE / "sen1floods11_norm_constants.json"
CHANNELS = ["VV", "VH"]

HF_REPO_ID = "Governor6191/sar-flood-extent-unet-resnet34"
HF_FILENAME = "model.pt"
MODEL_VERSION = "unet-resnet34-sen1floods11-all"


def build_model(in_channels: int = 2, classes: int = 2) -> nn.Module:
    """U-Net with a ResNet34 encoder, no pretrained weights (we load our own)."""
    return smp.create_model(
        arch="unet",
        encoder_name="resnet34",
        encoder_weights=None,
        in_channels=in_channels,
        classes=classes,
    )


def load_constants(path: Path | str = CONSTANTS_PATH) -> list[tuple[float, float]]:
    """Return [(mean, std)] per channel in CHANNELS order."""
    c = json.loads(Path(path).read_text())["channels"]
    return [(c[n]["mean"], c[n]["std"]) for n in CHANNELS]


def _resolve_checkpoint(local_path: str | None) -> str:
    """Resolve a checkpoint path. Order: explicit arg, MODEL_LOCAL_PATH env, HF Hub.

    HF download is used only when no local copy is given, so the container can bake
    the weights in at build time and run fully offline at request time.
    """
    candidate = local_path or os.environ.get("MODEL_LOCAL_PATH")
    if candidate and Path(candidate).exists():
        return str(candidate)
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo_id=HF_REPO_ID, filename=HF_FILENAME)


def load_model(local_path: str | None = None, device: str = "cpu") -> nn.Module:
    """Load the released checkpoint into the model and put it in eval mode."""
    ckpt_path = _resolve_checkpoint(local_path)
    ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
    model = build_model(in_channels=2, classes=2).to(device)
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    return model


def standardize(db: np.ndarray, constants: list[tuple[float, float]]) -> np.ndarray:
    """db: (2, H, W) sigma0 dB -> standardized float32; non-finite -> 0."""
    out = np.empty(db.shape, dtype=np.float32)
    for c in range(2):
        mean, std = constants[c]
        ch = (db[c].astype(np.float32) - mean) / std
        ch[~np.isfinite(ch)] = 0.0
        out[c] = ch
    return out


def infer_scene(
    model: nn.Module,
    std_img: np.ndarray,
    device: str = "cpu",
    tile: int = 512,
    batch: int = 8,
) -> np.ndarray:
    """std_img: (2, H, W) standardized -> (H, W) uint8 water mask (0 dry, 1 water)."""
    _, H, W = std_img.shape
    mask = np.zeros((H, W), dtype=np.uint8)
    tiles, coords = [], []
    for y in range(0, H, tile):
        for x in range(0, W, tile):
            patch = std_img[:, y:y + tile, x:x + tile]
            ph, pw = patch.shape[1], patch.shape[2]
            if ph < tile or pw < tile:
                padded = np.zeros((2, tile, tile), dtype=np.float32)
                padded[:, :ph, :pw] = patch
                patch = padded
            tiles.append(patch)
            coords.append((y, x, ph, pw))

    with torch.no_grad():
        for i in range(0, len(tiles), batch):
            chunk = np.stack(tiles[i:i + batch])
            t = torch.from_numpy(chunk).to(device)
            with torch.autocast(device_type=device, enabled=(device == "cuda")):
                pred = model(t).argmax(dim=1).cpu().numpy().astype(np.uint8)
            for j, (y, x, ph, pw) in enumerate(coords[i:i + batch]):
                mask[y:y + ph, x:x + pw] = pred[j, :ph, :pw]
    return mask


def polygonize(
    mask: np.ndarray,
    transform,
    crs,
    to_4326: bool = True,
    min_pixels: int = 0,
) -> tuple[str, int]:
    """Vectorize the water class to a GeoJSON FeatureCollection string.

    Coordinates are reprojected to EPSG:4326 by default (the GeoJSON standard CRS)
    so web clients can drop the output straight onto a map. min_pixels drops
    speckle polygons smaller than the given pixel-area threshold.
    """
    px_area = abs(transform.a * transform.e)
    feats = []
    for geom, value in features.shapes(mask, mask == 1, transform=transform):
        if value != 1:
            continue
        if min_pixels and _geom_pixels(geom, px_area) < min_pixels:
            continue
        out_geom = transform_geom(crs, "EPSG:4326", geom) if (to_4326 and crs) else geom
        feats.append({"type": "Feature", "properties": {"class": "water"}, "geometry": out_geom})

    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326" if to_4326 else str(crs)}},
        "features": feats,
    }
    return json.dumps(fc), len(feats)


def _geom_pixels(geom: dict, px_area: float) -> float:
    """Rough polygon area in pixels, used only for the min_pixels speckle filter."""
    if not px_area:
        return float("inf")
    ring = geom["coordinates"][0]
    n = len(ring)
    area2 = 0.0
    for i in range(n - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        area2 += x1 * y2 - x2 * y1
    return abs(area2) / 2.0 / px_area


def _mask_profile(profile: dict) -> dict:
    """Single-band uint8 LZW profile derived from the input scene's georeferencing."""
    profile = dict(profile)
    profile.update(count=1, dtype="uint8", nodata=None, compress="lzw")
    return profile


def write_mask(mask: np.ndarray, profile: dict, out_path: Path | str) -> str:
    """Write the mask as a single-band uint8 LZW GeoTIFF, matching the reference."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **_mask_profile(profile)) as dst:
        dst.write(mask, 1)
    return str(out_path)


def mask_to_geotiff_bytes(mask: np.ndarray, profile: dict) -> bytes:
    """Encode the mask to an in-memory GeoTIFF, byte-identical to write_mask output.

    Used by the SageMaker /invocations path, which returns the mask bytes to the
    caller (the Lambda kicker writes them to S3) rather than to a local file.
    """
    from rasterio.io import MemoryFile

    with MemoryFile() as mem:
        with mem.open(**_mask_profile(profile)) as dst:
            dst.write(mask, 1)
        return mem.read()


def resolve_uri(uri: str) -> str:
    """Map a caller-supplied URI to something rasterio can open.

    s3://bucket/key      -> /vsis3/bucket/key
    https://host/x.tif   -> /vsicurl/https://host/x.tif
    file:///abs/path     -> /abs/path
    plain path           -> unchanged
    """
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme == "s3":
        return f"/vsis3/{parsed.netloc}{parsed.path}"
    if scheme in ("http", "https"):
        return f"/vsicurl/{uri}"
    if scheme == "file":
        path = parsed.path
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]  # /C:/x -> C:/x
        return path
    return uri


@dataclass
class PredictResult:
    mask: np.ndarray
    water_fraction: float
    geojson: str | None
    n_polygons: int
    transform: object
    crs: object
    profile: dict


def predict(
    scene_uri: str,
    model: nn.Module,
    constants: list[tuple[float, float]],
    device: str = "cpu",
    tile: int = 512,
    batch: int = 8,
    want_geojson: bool = True,
    min_pixels: int = 0,
) -> PredictResult:
    """Full path: open a 2-band dB scene, standardize, infer, optionally vectorize."""
    path = resolve_uri(scene_uri)
    with rasterio.open(path) as src:
        db = src.read().astype(np.float64)
        profile = src.profile
        transform, crs = src.transform, src.crs
    if db.shape[0] < 2:
        raise ValueError(f"expected >=2 bands (VV, VH), got {db.shape[0]}")
    std_img = standardize(db[:2], constants)
    mask = infer_scene(model, std_img, device, tile=tile, batch=batch)
    water_fraction = float((mask == 1).mean())
    geojson, n = (None, 0)
    if want_geojson:
        geojson, n = polygonize(mask, transform, crs, min_pixels=min_pixels)
    return PredictResult(mask, water_fraction, geojson, n, transform, crs, dict(profile))
