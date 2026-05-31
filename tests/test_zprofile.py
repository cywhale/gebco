"""Tests for src.zprofile.zprofile — v0.5.3 H11.

These tests run zprofile() against the toy `fake_ds` fixture
(`conftest.py`) which is wired into `src.config.ds`. The fixture also
overrides `config.arc/basex/basey` so zprofile's legacy index math stays
consistent with the toy grid instead of the production full-earth grid.

The toy grid is centred on (0°, 0°) so most cells map to indices well
inside the array bounds. We assert response shape rather than exact
z-values because the fake grid uses synthetic elevation; the
end-to-end byte-equality tests (verify_polygon_meridian.py) cover
real-grid correctness.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from src.zprofile import empty_data, zprofile


def _decode(resp):
    """Decode either ORJSONResponse or JSONResponse body to dict."""
    return json.loads(resp.body)


def test_single_point_returns_z(configured_ds):
    resp = zprofile(np.array([0.05]), np.array([0.05]), None, 1)
    body = _decode(resp)
    assert set(body) >= {"longitude", "latitude", "z"}
    assert len(body["longitude"]) == 1
    assert len(body["z"]) == 1


def test_two_point_line_returns_multiple_cells(configured_ds):
    resp = zprofile(np.array([-0.05, 0.05]), np.array([0.0, 0.0]), None, 1)
    body = _decode(resp)
    # Line mode interpolates intermediate cells between the two endpoints.
    assert len(body["longitude"]) >= 2


def test_zonly_mode_omits_distance(configured_ds):
    resp = zprofile(np.array([0.01]), np.array([0.0]), "zonly", 1)
    body = _decode(resp)
    assert "distance" not in body


def test_row_mode_returns_list_of_dicts(configured_ds):
    resp = zprofile(np.array([-0.05, 0.05]), np.array([0.0, 0.0]), "row", 1)
    body = _decode(resp)
    assert isinstance(body, list)
    assert all("longitude" in row for row in body)


def test_point_mode_returns_only_endpoints(configured_ds):
    # In `point` mode the response should have only the two queried cells,
    # not interpolated intermediates.
    resp = zprofile(
        np.array([-0.05, 0.0, 0.05]),
        np.array([0.0, 0.0, 0.0]),
        "point",
        1,
    )
    body = _decode(resp)
    assert len(body["longitude"]) == 3


def test_dataframe_mode_returns_polars_frame(configured_ds):
    df = zprofile(
        np.array([-0.05, 0.05]),
        np.array([0.0, 0.0]),
        "dataframe",
        1,
    )
    import polars as pl
    assert isinstance(df, pl.DataFrame)
    assert {"longitude", "latitude", "z"}.issubset(set(df.columns))


def test_lon360_mode_output_in_0_360(configured_ds):
    resp = zprofile(np.array([-0.05, 0.05]), np.array([0.0, 0.0]), "lon360", 1)
    body = _decode(resp)
    assert all(0 <= lon <= 360 for lon in body["longitude"])


def test_mismatched_lon_lat_lengths_return_400(configured_ds):
    resp = zprofile(np.array([0.0, 0.1]), np.array([0.0]), None, 1)
    assert resp.status_code == 400


def test_empty_data_schema():
    df = empty_data()
    assert df.is_empty()
    assert set(df.columns) >= {"longitude", "latitude", "z"}


# -------- v0.5.4 W1 (H9) long-polyline shape coverage --------------------

def test_long_polyline_shape_invariants(configured_ds):
    """A long diagonal polyline through the toy grid must produce one
    longitude / latitude / z value per intermediate cell, and the
    distance column must have the same length.

    This catches H9 off-by-one bugs in the list-based accumulator: the
    legacy code paid an O(n²) penalty for many ``np.append`` calls, the
    refactor must produce identical row counts.
    """
    n = 30
    loni = np.linspace(-0.1, 0.1, n)
    lati = np.linspace(-0.05, 0.05, n)
    resp = zprofile(loni, lati, None, 1)
    body = _decode(resp)
    rows = len(body["longitude"])
    # All four arrays must have identical length.
    assert rows == len(body["latitude"]) == len(body["z"]) == len(body["distance"])
    # Row count must exceed the input vertex count (line mode interpolates
    # intermediate cells); otherwise the buffer never grew past inputs.
    assert rows >= n


def test_long_polyline_distance_first_is_zero(configured_ds):
    """The first distance entry is the placeholder 0.0 carried over from
    the old ``dis1 = np.array([0.0])`` initialiser; H9's list buffer keeps
    the same seed so consumers reading ``distance[0]`` don't break."""
    loni = np.linspace(-0.1, 0.1, 20)
    lati = np.linspace(-0.05, 0.05, 20)
    resp = zprofile(loni, lati, None, 1)
    body = _decode(resp)
    assert body["distance"][0] == 0.0


def test_zonly_long_polyline_omits_distance(configured_ds):
    loni = np.linspace(-0.1, 0.1, 30)
    lati = np.linspace(-0.05, 0.05, 30)
    resp = zprofile(loni, lati, "zonly", 1)
    body = _decode(resp)
    assert "distance" not in body
    assert len(body["longitude"]) == len(body["latitude"]) == len(body["z"])


def test_sparse_line_falls_back_when_chunk_metadata_missing(configured_ds):
    loni = np.array([-0.1, 0.1])
    lati = np.array([-0.05, 0.05])
    prev_chunks = configured_ds["elevation"].encoding.get("chunks")
    configured_ds["elevation"].encoding = {}
    try:
        resp = zprofile(loni, lati, "zonly", 1)
    finally:
        if prev_chunks is None:
            configured_ds["elevation"].encoding = {}
        else:
            configured_ds["elevation"].encoding["chunks"] = prev_chunks
    body = _decode(resp)
    assert resp.status_code == 200
    assert len(body["longitude"]) == len(body["latitude"]) == len(body["z"])
