"""Tests for the H6 bbox cell-count guards — v0.5.3.

We construct synthetic line + polygon inputs whose bbox spans the whole
globe so the guard fires at a single calculated cell count. The `fake_ds`
fixture wires a tiny Zarr-like Dataset into `src.config.ds`; this isn't
exercised by the guard (which only inspects coordinates / arc / cap),
but keeps the import chain valid.
"""
from __future__ import annotations

import numpy as np
import pytest

import src.config as config
from src.zprofile import BboxTooLarge, zprofile, zdata_bbox


def test_line_bbox_under_cap_runs(configured_ds, monkeypatch):
    """A narrow line should pass through the guard."""
    monkeypatch.setattr(config, "MAX_BBOX_CELLS_LINE", 10**12)
    # Two points 0.1° apart at lat=0 — well inside fake_ds support.
    loni = np.array([0.0, 0.1])
    lati = np.array([0.0, 0.0])
    # We don't care about the result, only that no BboxTooLarge raised.
    try:
        zprofile(loni, lati, "row", 1)
    except BboxTooLarge:
        pytest.fail("guard fired below cap")
    except Exception:
        # Other internal exceptions are OK for this test; we only assert
        # that BboxTooLarge specifically did not fire.
        pass


def test_line_bbox_over_cap_raises(configured_ds, monkeypatch):
    """Force the guard to fire by setting cap to 1 cell."""
    monkeypatch.setattr(config, "MAX_BBOX_CELLS_LINE", 1)
    loni = np.array([-179.0, 179.0])
    lati = np.array([-89.0, 89.0])
    with pytest.raises(BboxTooLarge) as exc_info:
        zprofile(loni, lati, "row", 1)
    assert exc_info.value.path == "line"
    assert exc_info.value.kind == "bbox_cells"
    assert exc_info.value.cap == 1


def test_polygon_bbox_over_cap_raises(configured_ds, monkeypatch):
    monkeypatch.setattr(config, "MAX_POLYGON_CELLS", 1)
    with pytest.raises(BboxTooLarge) as exc_info:
        zdata_bbox((-179.0, -89.0, 179.0, 89.0), crosses_180=False, isRight=False, sample=5)
    assert exc_info.value.path == "polygon"
    assert exc_info.value.kind == "polygon_cells"


def test_polygon_bbox_under_cap_runs(configured_ds, monkeypatch):
    monkeypatch.setattr(config, "MAX_POLYGON_CELLS", 10**12)
    # Bbox inside the toy fake_ds support.
    subset = zdata_bbox((-0.1, -0.05, 0.1, 0.05),
                        crosses_180=False, isRight=False, sample=1)
    assert subset.sizes["lat"] > 0
    assert subset.sizes["lon"] > 0


def test_bbox_too_large_message_includes_path_and_cap():
    err = BboxTooLarge(99999999999, 1000, path="line", kind="line_chunks")
    msg = str(err)
    assert "line" in msg
    assert "line_chunks" in msg
    assert "99,999,999,999" in msg
    assert "1,000" in msg
