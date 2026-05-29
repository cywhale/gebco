"""Shared pytest fixtures for v0.5.3 hardening unit tests.

These tests exercise `src/` legacy modules (`zprofile`, `polyhandler`,
`xmeridian`, `modes`, `validation`, `logger`) without going through the
FastAPI lifespan. A small in-memory toy Zarr-like Dataset is wired into
`src.config.ds` so the production read paths run unchanged on a known
data shape.

`fake_ds` is intentionally tiny (40×80 elevation cells). The fixture also
temporarily overrides `config.arc/basex/basey` so the legacy index math stays
consistent with the toy grid instead of production's full-earth indexing.
"""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

# Make `src.*` importable from the repo root when pytest is launched from
# any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def fake_ds():
    # xarray imported lazily so tests that only need src.modes /
    # src.validation / src.jsonsrc don't pay the import cost or hard-fail
    # when xarray isn't installed in a minimal CI image.
    import xarray as xr
    """A 40x80 toy bathymetry grid centred on (0°N, 0°E).

    Why this shape:
      * 40 lat × 80 lon = 3200 cells: tiny, fits in test process trivially.
      * Bounded to ±(40 cells × 1/240°) = ±0.167°, so all internal index
        computations stay within bounds once `configured_ds` aligns the
        legacy base constants to the toy grid.

    Elevation values are -10000 + lat_idx * 100 + lon_idx so each cell
    has a distinct integer signature; tests that assert "expected z[i]"
    can compute it deterministically.
    """
    lat = np.linspace(-0.16666667, 0.16666667, 40, dtype=np.float64)
    lon = np.linspace(-0.33333333, 0.33333333, 80, dtype=np.float64)
    lat_idx = np.arange(40, dtype=np.int16)[:, None]
    lon_idx = np.arange(80, dtype=np.int16)[None, :]
    elev = (-10000 + lat_idx * 100 + lon_idx).astype(np.int16)

    ds = xr.Dataset(
        {"elevation": (("lat", "lon"), elev)},
        coords={"lat": lat, "lon": lon},
    )
    return ds


@pytest.fixture
def configured_ds(fake_ds):
    """Wire `fake_ds` into `src.config.ds` for the duration of the test.

    The legacy modules read `config.ds` plus `config.arc/basex/basey` as
    module globals; this fixture sets all four and restores the previous
    values on teardown.
    """
    import src.config as config

    prev_ds = config.ds
    prev_arc = config.arc
    prev_basex = config.basex
    prev_basey = config.basey

    config.ds = fake_ds
    config.arc = 240
    config.basex = 0
    config.basey = 0

    yield fake_ds

    config.ds = prev_ds
    config.arc = prev_arc
    config.basex = prev_basex
    config.basey = prev_basey


@pytest.fixture
def logger_lifecycle():
    """Configure the v0.5.3 structured logger and shut it down on teardown.

    Prevents listener-thread accumulation across the test suite.
    """
    from src import logger as gebco_logger
    gebco_logger.configure()
    yield gebco_logger
    gebco_logger.shutdown()
