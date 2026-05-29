"""Tests for src.xmeridian.crossBoundary — v0.5.3 H11.

These cases hand-verify the breakpoint-insertion logic that today is
only smoke-tested through the dev2026 verify_polygon_meridian.py
end-to-end script. Every case below was traced manually against the
documented contract: `crossBoundary(lon, lat)` returns
`(newx, newy, breakpoint_indices, autoFly_classifications)`.
"""
from __future__ import annotations

import numpy as np

from src.xmeridian import crossBoundary, whichSide


# -------- whichSide helper ------------------------------------------------

def test_whichside_same_sign_returns_none():
    assert whichSide([10], [20]) is None
    assert whichSide([-10], [-20]) is None


def test_whichside_cross_zero_close_to_meridian():
    """Two endpoints near 0° should be classified as cross-zero."""
    assert whichSide([-5], [5]) == "cross-zero"


def test_whichside_cross_180_far_from_meridian():
    """Two endpoints near 180° (one + one -) should be away-zero."""
    assert whichSide([175], [-175]) == "away-zero"


# -------- crossBoundary -------------------------------------------------

def test_single_point_returns_unchanged():
    lon = np.array([10.0])
    lat = np.array([20.0])
    newx, newy, idx, fly = crossBoundary(lon, lat)
    assert list(newx) == [10.0]
    assert list(newy) == [20.0]
    assert list(idx) == [-1]


def test_line_no_crossing_returns_unchanged():
    lon = np.array([10.0, 15.0, 20.0])
    lat = np.array([0.0, 0.0, 0.0])
    newx, newy, idx, fly = crossBoundary(lon, lat)
    assert list(newx) == [10.0, 15.0, 20.0]
    assert list(idx) == [-1]


def test_line_wholly_west_no_crossing():
    lon = np.array([-30.0, -20.0, -10.0])
    lat = np.array([0.0, 0.0, 0.0])
    newx, newy, idx, fly = crossBoundary(lon, lat)
    assert list(idx) == [-1]


def test_line_crossing_zero_inserts_breakpoint():
    """A line from -5° to +5° must get a break inserted near 0°."""
    lon = np.array([-5.0, 5.0])
    lat = np.array([0.0, 0.0])
    newx, newy, idx, fly = crossBoundary(lon, lat)
    # exactly one break expected
    breakpoints = [i for i in idx if i != -1]
    assert len(breakpoints) == 1
    assert "cross-zero" in list(fly)


def test_line_crossing_180_inserts_breakpoint():
    """A line from 175° to -175° must get a break inserted near 180°."""
    lon = np.array([175.0, -175.0])
    lat = np.array([0.0, 0.0])
    newx, newy, idx, fly = crossBoundary(lon, lat)
    breakpoints = [i for i in idx if i != -1]
    assert len(breakpoints) == 1
    assert "away-zero" in list(fly)


def test_line_crossing_both_meridians():
    """Going west from 175° → -10° → +10° crosses both 180° and 0°."""
    lon = np.array([175.0, -10.0, 10.0])
    lat = np.array([0.0, 0.0, 0.0])
    newx, newy, idx, fly = crossBoundary(lon, lat)
    breakpoints = [i for i in idx if i != -1]
    assert len(breakpoints) == 2
    assert "away-zero" in list(fly)
    assert "cross-zero" in list(fly)


def test_steep_slope_crossing_zero():
    """|slope| > 1 path crossing 0° still produces exactly one break."""
    lon = np.array([-5.0, 5.0])
    lat = np.array([0.0, 50.0])  # |m| = 5
    newx, newy, idx, fly = crossBoundary(lon, lat)
    breakpoints = [i for i in idx if i != -1]
    assert len(breakpoints) == 1


def test_shallow_slope_crossing_zero():
    """|slope| ≈ 0 path crossing 0° still produces exactly one break."""
    lon = np.array([-5.0, 5.0])
    lat = np.array([0.0, 0.0001])
    newx, newy, idx, fly = crossBoundary(lon, lat)
    breakpoints = [i for i in idx if i != -1]
    assert len(breakpoints) == 1


# -------- v0.5.4 W1 (H9) regression coverage ------------------------------

def test_long_alternating_crossings_breakpoint_count():
    """Long polyline that zig-zags across the prime meridian — every
    sign-change must produce exactly one breakpoint, no more no less.

    This guards H9 against off-by-one bugs introduced by the list-based
    accumulator (forgetting to append a breakpoint, or duplicating one
    because of a stale ``newidx`` index).
    """
    n = 200
    # alternating + / - around 0°
    base = np.linspace(-3.0, 3.0, n)
    lon = base * np.array([1.0, -1.0] * (n // 2))
    lat = np.linspace(-30.0, 30.0, n)
    sign_changes = int(np.sum(np.sign(lon[:-1]) != np.sign(lon[1:])))
    newx, newy, idx, fly = crossBoundary(lon, lat)
    breakpoints = [i for i in idx if i != -1]
    assert len(breakpoints) == sign_changes
    assert len(fly) == sign_changes
    # output arrays remain consistent
    assert newx.shape == newy.shape


def test_long_polyline_shape_stable():
    """A 500-point monotone polyline crossing no meridian should pass
    through untouched (no break-points inserted)."""
    n = 500
    lon = np.linspace(10.0, 80.0, n)
    lat = np.linspace(20.0, 50.0, n)
    newx, newy, idx, fly = crossBoundary(lon, lat)
    assert list(idx) == [-1]
    assert len(fly) == 0
    np.testing.assert_array_equal(newx, lon)
    np.testing.assert_array_equal(newy, lat)
