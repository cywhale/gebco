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


def _full_width_ds():
    """A full-width (basex=180) toy grid whose lon index reaches ~86399.

    Needed to reproduce the cross-180 int16-overflow regression: the toy
    `fake_ds` is only 80 cells wide, so its indices never exceed int16. Here
    elevation depends only on the global lon index (folded into int16 range)
    so a wrong-column read is detectable.
    """
    import xarray as xr

    # basex=180 makes lon indices reach ~86399 (the overflow trigger). basey=1
    # with the test line near the south edge keeps the lat base index small so
    # the grid stays tiny (40 lat rows) while the lat slice/clamp stay valid.
    arc, basex, basey = 240, 180, 1
    line_lat = -0.95
    n_lon = 360 * arc          # 86400 -> indices up to 86399 >> int16 max 32767
    n_lat = 40
    lon = -basex + (np.arange(n_lon) + 0.5) / arc       # cell centres, -180..180
    lat = -basey + (np.arange(n_lat) + 0.5) / arc       # band around line_lat
    sig = ((np.arange(n_lon) % 30000) - 15000).astype(np.int16)
    elev = np.broadcast_to(sig[None, :], (n_lat, n_lon)).astype(np.int16)
    ds = xr.Dataset({"elevation": (("lat", "lon"), elev)},
                    coords={"lat": lat, "lon": lon})
    return ds, arc, basex, basey, line_lat


def test_cross_180_line_matches_equivalent_noncrossing_line():
    """Regression: a 180°-crossing line must read the same cells as the
    geometrically identical non-crossing line.

    The v0.5.4 H9 refactor stored bbox-relative cell indices as ``np.int16``.
    A line crossing the antimeridian makes ``crossBoundary`` insert break
    points at both ±180, so the subset spans the full grid width and the
    relative lon index reaches ~86399 — overflowing int16 (max 32767) and
    silently reading the wrong ocean (lon ~-94 instead of ~+179). The pre-H9
    ``np.append`` code was correct only because it promoted the array to
    int64. This asserts the crossing and non-crossing forms of 179°->180°
    agree (they are the same physical segment).
    """
    import src.config as config

    ds, arc, basex, basey, line_lat = _full_width_ds()
    prev = (config.ds, getattr(config, "elev_zarr", None),
            config.arc, config.basex, config.basey)
    config.ds, config.elev_zarr = ds, None
    config.arc, config.basex, config.basey = arc, basex, basey
    try:
        cross = _decode(zprofile(np.array([179.0, -180.0]),
                                 np.array([line_lat, line_lat]), "truncate", 1))
        nocross = _decode(zprofile(np.array([179.0, 180.0]),
                                   np.array([line_lat, line_lat]), "truncate", 1))
    finally:
        (config.ds, config.elev_zarr,
         config.arc, config.basex, config.basey) = prev

    zc = np.asarray(cross["z"])
    zn = np.asarray(nocross["z"])
    assert len(zc) > 100  # ~1° of 15-arcsec cells
    assert len(zc) == len(zn)
    # The shared interior (everything but the differing ±180 endpoint cell)
    # must be byte-identical. Pre-fix, the crossing form read the wrapped
    # column ~20623 and these arrays diverged.
    assert np.array_equal(zc[:-1], zn[:-1])


def test_cross_180_chunk_read_matches_dense_no_premature_413():
    """O-3: a cross-180° line in non-zonly mode reads only touched chunks and
    is byte-identical to the dense read, and is NOT prematurely rejected with
    413 just because its enclosing bbox spans the full grid width."""
    import src.config as config

    ds, arc, basex, basey, line_lat = _full_width_ds()
    # Give the toy grid chunk metadata so the chunk-aware read path activates.
    ds["elevation"].encoding["chunks"] = (int(ds.sizes["lat"]), 2700)
    prev = (config.ds, getattr(config, "elev_zarr", None),
            config.arc, config.basex, config.basey,
            config.LINE_SPARSE_MIN_CELLS, config.MAX_LINE_CHUNKS,
            config.MAX_BBOX_CELLS_LINE)
    config.ds, config.elev_zarr = ds, None
    config.arc, config.basex, config.basey = arc, basex, basey
    try:
        # Force the chunk path; generous chunk cap so it is not rejected.
        config.LINE_SPARSE_MIN_CELLS = 1
        config.MAX_LINE_CHUNKS = 1000
        chunk = _decode(zprofile(np.array([179.0, -180.0]),
                                 np.array([line_lat, line_lat]), "truncate", 1))
        # Force the dense path as the byte-equal reference.
        config.LINE_SPARSE_MIN_CELLS = 10 ** 18
        config.MAX_BBOX_CELLS_LINE = 10 ** 18
        dense = _decode(zprofile(np.array([179.0, -180.0]),
                                 np.array([line_lat, line_lat]), "truncate", 1))
    finally:
        (config.ds, config.elev_zarr, config.arc, config.basex, config.basey,
         config.LINE_SPARSE_MIN_CELLS, config.MAX_LINE_CHUNKS,
         config.MAX_BBOX_CELLS_LINE) = prev

    assert len(chunk["z"]) > 100
    assert np.array_equal(np.asarray(chunk["z"]), np.asarray(dense["z"]))
    assert np.array_equal(np.asarray(chunk["longitude"]),
                          np.asarray(dense["longitude"]))


def test_cross_180_chunk_guard_trips_with_kind():
    """O-3: when a cross-180° line touches more than MAX_LINE_CHUNKS chunks it
    is rejected with 413 and `kind='line_chunks'` (consistent with the zonly
    sparse path), not the old bbox_cells guard."""
    import src.config as config
    from src.zprofile import BboxTooLarge

    ds, arc, basex, basey, line_lat = _full_width_ds()
    ds["elevation"].encoding["chunks"] = (int(ds.sizes["lat"]), 2700)
    prev = (config.ds, getattr(config, "elev_zarr", None),
            config.arc, config.basex, config.basey,
            config.LINE_SPARSE_MIN_CELLS, config.MAX_LINE_CHUNKS)
    config.ds, config.elev_zarr = ds, None
    config.arc, config.basex, config.basey = arc, basex, basey
    config.LINE_SPARSE_MIN_CELLS = 1
    config.MAX_LINE_CHUNKS = 1   # force the trip
    try:
        with pytest.raises(BboxTooLarge) as exc_info:
            zprofile(np.array([179.0, -180.0]),
                     np.array([line_lat, line_lat]), "truncate", 1)
        assert exc_info.value.kind == "line_chunks"
        assert exc_info.value.path == "line"
    finally:
        (config.ds, config.elev_zarr, config.arc, config.basex, config.basey,
         config.LINE_SPARSE_MIN_CELLS, config.MAX_LINE_CHUNKS) = prev


def test_single_point_lon360_output_in_0_360(configured_ds):
    """O-2: a single-point lon360 request echoes longitude in [0, 360]."""
    resp = zprofile(np.array([-0.1]), np.array([0.05]), "lon360", 1)
    body = _decode(resp)
    assert body["longitude"][0] == pytest.approx(359.9)


def test_single_point_truncate_rounds_coords(configured_ds):
    """O-2: a single-point truncate request rounds echoed coords to 5 dp."""
    resp = zprofile(np.array([0.123456789]), np.array([0.087654321]), "truncate", 1)
    body = _decode(resp)
    assert body["longitude"][0] == round(0.123456789, 5)
    assert body["latitude"][0] == round(0.087654321, 5)
