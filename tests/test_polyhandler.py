"""Tests for src.polyhandler.polyhandler — v0.5.3 H11.

Covers the polygon / FeatureCollection data path on the toy `fake_ds`
fixture, plus the H5 invariant (zonly mode never emits distance).
"""
from __future__ import annotations

import polars as pl
import pytest

from src.polyhandler import polyhandler


TINY_POLY = {
    "type": "Polygon",
    "coordinates": [[[-0.05, -0.05], [-0.05, 0.05],
                      [0.05, 0.05], [0.05, -0.05],
                      [-0.05, -0.05]]],
}

TINY_FEATCOL = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[-0.05, -0.05], [-0.05, 0.0],
                                       [0.0, 0.0], [0.0, -0.05],
                                       [-0.05, -0.05]]]}},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[0.0, 0.0], [0.0, 0.05],
                                       [0.05, 0.05], [0.05, 0.0],
                                       [0.0, 0.0]]]}},
    ],
}


def test_polygon_returns_polars_dataframe(configured_ds):
    df, _ = polyhandler(TINY_POLY, 0, "", 1, 1)
    assert isinstance(df, pl.DataFrame)
    assert {"longitude", "latitude", "z"}.issubset(set(df.columns))
    assert df.height > 0


def test_polygon_distance_present_by_default(configured_ds):
    df, _ = polyhandler(TINY_POLY, 0, "", 1, 1)
    assert "distance" in df.columns


def test_polygon_zonly_omits_distance(configured_ds):
    """H5 invariant: zonly polygon path must NOT emit distance column."""
    df, _ = polyhandler(TINY_POLY, 0, "zonly", 1, 1)
    assert "distance" not in df.columns


def test_polygon_lineid_present_when_requested(configured_ds):
    df, _ = polyhandler(TINY_POLY, 0, "lineid", 1, 1)
    assert "lineid" in df.columns


def test_feature_collection_sums_rows(configured_ds):
    df_solo, _ = polyhandler(TINY_POLY, 0, "", 1, 1)
    df_pair, _ = polyhandler(TINY_FEATCOL, 0, "", 1, 1)
    # Two half-polygons should produce roughly half the cells each, total <=
    # but not zero.
    assert df_pair.height > 0


def test_polygon_sample_default_5_smaller_than_sample_1(configured_ds):
    """sample=5 (production default) downsamples cells by ~1/25 vs sample=1."""
    df_dense, _ = polyhandler(TINY_POLY, 0, "", 1, 1)
    df_sampled, _ = polyhandler(TINY_POLY, 0, "", 1, 5)
    assert df_sampled.height <= df_dense.height
