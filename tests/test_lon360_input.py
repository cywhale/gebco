"""Tests for the lon360-input normalisation flow — v0.5.3 H3.

The flow lives in `gebco_app.gebco()` between validation and zprofile().
Here we test just the validator + normaliser pieces against the
documented contract from `specs/v0.5.3_hardening_checklist_v2.md` H3.
"""
from __future__ import annotations

import numpy as np

from src.validation import validate_lonlat


def test_lon360_accepts_input_in_0_360_range():
    loni = np.array([250.0, 260.0])
    lati = np.array([0.0, 0.0])
    assert validate_lonlat(loni, lati, allow_lon360=True) is None


def test_lon360_rejects_400_before_normalise():
    """The codex review fix: validate ORIGINAL input first.

    Pre-fix order would have done `400 - 360 = 40` first and silently
    accepted the request. The correct order rejects at the [0, 360]
    boundary check.
    """
    loni = np.array([400.0])
    lati = np.array([0.0])
    msg = validate_lonlat(loni, lati, allow_lon360=True)
    assert msg is not None
    assert "lon" in msg


def test_lon360_rejects_negative():
    loni = np.array([-1.0])
    lati = np.array([0.0])
    msg = validate_lonlat(loni, lati, allow_lon360=True)
    assert msg is not None


def test_lon360_post_normalisation_equivalence():
    """`250 - 360 = -110` and `260 - 360 = -100` — the normalised inputs
    are what the grid lookup actually receives. The unit test verifies
    the numpy normalisation matches that expectation."""
    loni = np.array([250.0, 260.0])
    normalised = np.where(loni > 180.0, loni - 360.0, loni)
    assert normalised.tolist() == [-110.0, -100.0]


def test_strict_path_rejects_lon250_without_lon360_mode():
    loni = np.array([250.0])
    lati = np.array([0.0])
    msg = validate_lonlat(loni, lati, allow_lon360=False)
    assert msg is not None
