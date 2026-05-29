"""Tests for the inference core.

The unit tests run with no model download and no large data, so they are CI-safe.
The integration test runs only when RUN_INTEGRATION=1 and a Harvey crop fixture
plus resolvable model weights are available; it is the small-scale stand-in for
the full-scene Harvey smoke test documented in docs/api_contract.md.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS

from src.inference import (
    build_model,
    infer_scene,
    load_constants,
    mask_to_geotiff_bytes,
    polygonize,
    predict,
    resolve_uri,
    standardize,
)

CROP_FIXTURE = Path(__file__).parent / "data" / "harvey_crop.tif"


def test_standardize_matches_formula():
    constants = [(-10.0, 2.0), (-18.0, 4.0)]
    db = np.array([[[-10.0, -8.0]], [[-18.0, -14.0]]], dtype=np.float64)
    out = standardize(db, constants)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out[0], [[0.0, 1.0]], rtol=1e-6)
    np.testing.assert_allclose(out[1], [[0.0, 1.0]], rtol=1e-6)


def test_standardize_zeroes_non_finite():
    constants = [(-10.0, 2.0), (-18.0, 4.0)]
    db = np.array([[[np.nan, np.inf]], [[-np.inf, -18.0]]], dtype=np.float64)
    out = standardize(db, constants)
    assert np.all(np.isfinite(out))
    assert out[0, 0, 0] == 0.0 and out[0, 0, 1] == 0.0


def test_load_constants_shape():
    constants = load_constants()
    assert len(constants) == 2
    # VV / VH means are negative dB, stds positive.
    assert constants[0][0] < 0 and constants[0][1] > 0
    assert constants[1][0] < 0 and constants[1][1] > 0


def test_resolve_uri_schemes():
    assert resolve_uri("s3://bucket/key/scene.tif") == "/vsis3/bucket/key/scene.tif"
    assert resolve_uri("https://h/x.tif") == "/vsicurl/https://h/x.tif"
    assert resolve_uri("/plain/path.tif") == "/plain/path.tif"
    # file:// maps back to a local path on either OS.
    assert resolve_uri("file:///tmp/x.tif").endswith("/tmp/x.tif") or resolve_uri(
        "file:///tmp/x.tif"
    ).endswith("\\tmp\\x.tif")


def test_infer_scene_synthetic_tiling():
    """Random-weight model exercises tile/pad/stitch on a non-multiple-of-512 image."""
    torch_model = build_model(in_channels=2, classes=2).eval()
    std_img = np.random.randn(2, 600, 700).astype(np.float32)
    mask = infer_scene(torch_model, std_img, device="cpu", tile=512, batch=4)
    assert mask.shape == (600, 700)
    assert mask.dtype == np.uint8
    assert set(np.unique(mask)).issubset({0, 1})


def test_polygonize_water_block():
    mask = np.zeros((20, 20), dtype=np.uint8)
    mask[5:15, 5:15] = 1
    transform = Affine.translation(0, 20) * Affine.scale(1.0, -1.0)
    geojson, n = polygonize(mask, transform, CRS.from_epsg(4326), to_4326=True)
    fc = json.loads(geojson)
    assert fc["type"] == "FeatureCollection"
    assert n == 1
    assert fc["features"][0]["properties"]["class"] == "water"


def test_polygonize_min_pixels_filters_speckle():
    mask = np.zeros((30, 30), dtype=np.uint8)
    mask[2:12, 2:12] = 1  # 100 px block
    mask[20, 20] = 1  # 1 px speckle
    transform = Affine.translation(0, 30) * Affine.scale(1.0, -1.0)
    _, n_all = polygonize(mask, transform, CRS.from_epsg(4326), min_pixels=0)
    _, n_filtered = polygonize(mask, transform, CRS.from_epsg(4326), min_pixels=10)
    assert n_all == 2
    assert n_filtered == 1


def test_mask_to_geotiff_bytes_round_trips():
    """The in-memory GeoTIFF (SageMaker path) reopens to the same mask and CRS."""
    from io import BytesIO

    mask = np.zeros((16, 24), dtype=np.uint8)
    mask[4:12, 6:18] = 1
    transform = Affine.translation(0, 16) * Affine.scale(1.0, -1.0)
    profile = {
        "driver": "GTiff",
        "height": 16,
        "width": 24,
        "count": 2,  # input had 2 bands; the writer must force count=1
        "dtype": "float32",
        "crs": CRS.from_epsg(4326),
        "transform": transform,
    }
    data = mask_to_geotiff_bytes(mask, profile)
    with rasterio.open(BytesIO(data)) as src:
        assert src.count == 1
        assert src.dtypes[0] == "uint8"
        assert src.crs == CRS.from_epsg(4326)
        np.testing.assert_array_equal(src.read(1), mask)


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1" or not CROP_FIXTURE.exists(),
    reason="set RUN_INTEGRATION=1 and provide tests/data/harvey_crop.tif",
)
def test_integration_harvey_crop():
    from src.inference import load_model

    model = load_model(device="cpu")
    constants = load_constants()
    result = predict(str(CROP_FIXTURE), model, constants, device="cpu", want_geojson=True)
    with rasterio.open(CROP_FIXTURE) as src:
        h, w = src.height, src.width
    assert result.mask.shape == (h, w)
    assert 0.0 <= result.water_fraction <= 1.0
    assert result.geojson is not None
