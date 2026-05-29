"""Tests for src.validation — v0.5.3 H2 / H8."""
from __future__ import annotations

import numpy as np
import pytest

from src.validation import numarr_query_validator, validate_lonlat


# -------- validate_lonlat (H2) --------------------------------------------

def test_valid_lonlat_returns_none():
    loni = np.array([121.0, 122.0])
    lati = np.array([24.0, 25.0])
    assert validate_lonlat(loni, lati) is None


def test_shape_mismatch_rejected():
    loni = np.array([1.0, 2.0])
    lati = np.array([1.0])
    msg = validate_lonlat(loni, lati)
    assert msg is not None
    assert "equal length" in msg


def test_nan_rejected():
    loni = np.array([121.0, float("nan")])
    lati = np.array([24.0, 25.0])
    msg = validate_lonlat(loni, lati)
    assert msg is not None
    assert "non-finite" in msg


def test_inf_rejected():
    loni = np.array([121.0, float("inf")])
    lati = np.array([24.0, 25.0])
    assert validate_lonlat(loni, lati) is not None


def test_lon_out_of_range_rejected():
    loni = np.array([181.0])
    lati = np.array([0.0])
    msg = validate_lonlat(loni, lati)
    assert msg is not None
    assert "lon" in msg


def test_lat_out_of_range_rejected():
    loni = np.array([0.0])
    lati = np.array([91.0])
    msg = validate_lonlat(loni, lati)
    assert msg is not None
    assert "lat" in msg


def test_boundary_values_accepted():
    loni = np.array([-180.0, 180.0])
    lati = np.array([-90.0, 90.0])
    assert validate_lonlat(loni, lati) is None


def test_allow_lon360_accepts_0_to_360():
    loni = np.array([0.0, 250.0, 360.0])
    lati = np.array([0.0, 0.0, 0.0])
    assert validate_lonlat(loni, lati, allow_lon360=True) is None


def test_allow_lon360_rejects_400():
    """Codex review fix: validate ORIGINAL input before normalising."""
    loni = np.array([400.0])
    lati = np.array([0.0])
    msg = validate_lonlat(loni, lati, allow_lon360=True)
    assert msg is not None
    assert "lon" in msg


def test_allow_lon360_strict_rejects_negative():
    loni = np.array([-1.0])
    lati = np.array([0.0])
    msg = validate_lonlat(loni, lati, allow_lon360=True)
    assert msg is not None


# -------- numarr_query_validator (H8) -------------------------------------

def test_numarr_single_value():
    out = numarr_query_validator("121.5")
    assert out.shape == (1,)
    assert out[0] == 121.5


def test_numarr_comma_separated():
    out = numarr_query_validator("121.5,122.7,123.0")
    assert out.shape == (3,)
    assert out.tolist() == [121.5, 122.7, 123.0]


def test_numarr_whitespace_tolerated():
    out = numarr_query_validator(" 1.0 , 2.0 ")
    assert out.tolist() == [1.0, 2.0]


def test_numarr_raises_on_bad_input():
    """H8: error path now uses exceptions, not string sentinels."""
    with pytest.raises(ValueError, match="parse"):
        numarr_query_validator("abc")


def test_numarr_raises_on_partial_failure():
    with pytest.raises(ValueError):
        numarr_query_validator("1.0,xyz,3.0")
