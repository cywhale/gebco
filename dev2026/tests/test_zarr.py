"""Pytest sanity checks for the GEBCO_2026 Zarr store.

Run:
    uv run pytest

Configuration:
    The tests find the Zarr to inspect by looking, in this order, at:
      1. The ``GEBCO_2026_ZARR`` env var
      2. ``../data/GEBCO_2026_sub_ice_topo.zarr`` relative to the repo root
         (canonical name — matches gebco_app.py:lifespan)
      3. ``../data/GEBCO_2026_sub_ice_topo_blosc.zarr`` (intermediate name
         used while converting in the cowork sandbox; harmless if absent)

    If none exists the whole module is *skipped* — you can therefore commit
    these tests safely; they only do work after the conversion has been run.

    The 2023 reference for spot-check diffs is found at:
      1. The ``GEBCO_2023_REF`` env var
      2. ``../data/GEBCO_2023_sub_ice_topo.zarr``
      3. ``../data_src/GEBCO_2023/GEBCO_2023.nc``

    If found, spot-check tests will additionally assert the 2026 value is within
    a reasonable distance (default 2000 m absolute difference) from the 2023
    value at each reference point. SRTM15+ v2.8 + new BedMachine versions can
    shift seafloor estimates by hundreds of metres in poorly-mapped areas, so
    the threshold is generous on purpose.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV2026 = Path(__file__).resolve().parents[1]


def _candidates_new() -> list[Path]:
    out = []
    env = os.environ.get("GEBCO_2026_ZARR")
    if env:
        out.append(Path(env))
    # Canonical name (production / gebco_app.py:lifespan)
    out.append(REPO_ROOT / "data" / "GEBCO_2026_sub_ice_topo.zarr")
    # Intermediate name used by the sandbox conversion path
    out.append(REPO_ROOT / "data" / "GEBCO_2026_sub_ice_topo_blosc.zarr")
    return out


def _candidates_ref() -> list[Path]:
    out = []
    env = os.environ.get("GEBCO_2023_REF")
    if env:
        out.append(Path(env))
    out.append(REPO_ROOT / "data" / "GEBCO_2023_sub_ice_topo.zarr")
    out.append(REPO_ROOT / "data_src" / "GEBCO_2023" / "GEBCO_2023.nc")
    return out


def _resolve(cands: list[Path]) -> Path | None:
    for c in cands:
        if c.exists():
            return c
    return None


NEW_PATH = _resolve(_candidates_new())
REF_PATH = _resolve(_candidates_ref())

# Same as scripts/verify_zarr.py
REFERENCE_POINTS = [
    ("NE of Taiwan",        23.326173, 123.978125),
    ("NE of Taiwan offset", 23.317670, 123.973958),
    ("Lanyu area",          21.336378, 123.003125),
    ("Lanyu rounded",       21.33,     123.0),
]


pytestmark = pytest.mark.skipif(
    NEW_PATH is None,
    reason=(
        "No GEBCO_2026 Zarr to verify. Set GEBCO_2026_ZARR or put it at "
        "data/GEBCO_2026_sub_ice_topo.zarr"
    ),
)


@pytest.fixture(scope="module")
def ds() -> xr.Dataset:
    """Open the new Zarr with the same flags gebco_app.py uses."""
    d = xr.open_zarr(
        str(NEW_PATH), chunks="auto", decode_cf=False, decode_times=False
    )
    yield d
    d.close()


@pytest.fixture(scope="module")
def ref() -> xr.Dataset | None:
    if REF_PATH is None:
        return None
    if REF_PATH.suffix == ".zarr" or (REF_PATH / ".zgroup").exists():
        d = xr.open_zarr(str(REF_PATH), chunks="auto", decode_cf=False, decode_times=False)
    else:
        d = xr.open_dataset(str(REF_PATH), chunks="auto", decode_cf=False, decode_times=False)
    yield d
    d.close()


def test_has_elevation(ds: xr.Dataset) -> None:
    assert "elevation" in ds.data_vars, f"data_vars = {list(ds.data_vars)}"


def test_elevation_dtype_int16(ds: xr.Dataset) -> None:
    assert ds["elevation"].dtype == np.int16


def test_global_shape(ds: xr.Dataset) -> None:
    # 15 arc-second global grid
    assert ds.sizes.get("lat") == 43200
    assert ds.sizes.get("lon") == 86400


def test_lat_range(ds: xr.Dataset) -> None:
    lat = ds["lat"].values
    assert lat.size == 43200
    assert -90.0 < lat.min() < -89.99
    assert 89.99 < lat.max() < 90.0
    # monotonically ascending
    assert lat[1] > lat[0]


def test_lon_range(ds: xr.Dataset) -> None:
    lon = ds["lon"].values
    assert lon.size == 86400
    assert -180.0 < lon.min() < -179.99
    assert 179.99 < lon.max() < 180.0
    assert lon[1] > lon[0]


def test_pixel_size_is_15_arcsec(ds: xr.Dataset) -> None:
    expected = 1 / 240.0  # 15 / 3600
    lat = ds["lat"].values
    lon = ds["lon"].values
    assert abs((lat[1] - lat[0]) - expected) < 1e-9
    assert abs((lon[1] - lon[0]) - expected) < 1e-9


def test_app_open_flags_work() -> None:
    """Exactly how gebco_app.py opens the store — must not raise."""
    d = xr.open_zarr(
        str(NEW_PATH), chunks="auto", decode_cf=False, decode_times=False
    )
    try:
        # also exercise the slicing path the API uses
        s = d.sel(lon=slice(120, 122), lat=slice(22, 24))
        _ = s["elevation"].isel(lat=slice(0, 4), lon=slice(0, 4)).values
    finally:
        d.close()


@pytest.mark.parametrize("label,la,lo", REFERENCE_POINTS)
def test_reference_points_plausible(ds: xr.Dataset, label: str, la: float, lo: float) -> None:
    v = ds["elevation"].sel(lat=la, lon=lo, method="nearest").values.item()
    # GEBCO elevation is int16; -11000..9000 covers the whole plausible range
    # (Mariana Trench to Everest) with margin.
    assert -12000 < v < 9500, f"{label}: implausible elevation {v}"


@pytest.mark.skipif(REF_PATH is None, reason="no 2023 reference dataset found")
@pytest.mark.parametrize("label,la,lo", REFERENCE_POINTS)
def test_2026_vs_2023_close(
    ds: xr.Dataset, ref: xr.Dataset, label: str, la: float, lo: float
) -> None:
    """At our four hand-picked Taiwan points, 2026 should be within 2000 m of 2023."""
    v_new = ds["elevation"].sel(lat=la, lon=lo, method="nearest").values.item()
    v_ref = ref["elevation"].sel(lat=la, lon=lo, method="nearest").values.item()
    diff = v_new - v_ref
    assert abs(diff) < 2000, (
        f"{label}: 2026 elev {v_new} differs from 2023 elev {v_ref} by {diff} m "
        "(>2000m) — investigate before believing the new grid"
    )
