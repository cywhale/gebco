"""S1 spike — bbox materialisation cost curve (line + polygon paths).

v0.5.3 H6 introduces two backend caps (`MAX_BBOX_CELLS_LINE`,
`MAX_POLYGON_CELLS`) that translate "implied bbox too large" into a
413. This script measures the wall-time / peak-RSS curve as bbox cell
count grows, so the production caps can be picked just above the
"abusive" knee rather than at the typical-drawn-polygon point.

It does NOT run pytest — it's a one-shot probe driven by humans and
reviewers. Run on a machine with the canonical 2026 Zarr at
`data/GEBCO_2026_sub_ice_topo.zarr` and a working dev2026 `uv sync`
env (includes xarray + shapely + psutil).

Output: a table of (raw_cells, wall_time_s, peak_rss_mb) for both the
line/point path (just `ds.sel(...).elevation.values`) and the polygon
path (meshgrid + shapely points + shapely.contains mask).

Acceptance criterion documented in
`specs/v0.5.3_hardening_checklist_v2.md` §3.

Usage:
    uv run python scripts/probe_bbox_limits.py
    uv run python scripts/probe_bbox_limits.py \
        --zarr ../data/GEBCO_2026_sub_ice_topo.zarr
"""
from __future__ import annotations

import argparse
import math
import sys
import time
import warnings
from pathlib import Path

import numpy as np

warnings.simplefilter("ignore")

_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]


def _open_zarr(path: Path):
    import xarray as xr
    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


def _rss_mb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _line_probe(ds, side_deg: float) -> tuple[int, float, float]:
    """Materialise a square open-Pacific bbox `side_deg × side_deg`.

    Returns (raw_cells, wall_time_s, peak_rss_delta_mb).
    """
    # Centre the bbox in the South Pacific abyssal, away from coasts.
    lat_lo, lat_hi = -30.0 - side_deg / 2, -30.0 + side_deg / 2
    lon_lo, lon_hi = -150.0 - side_deg / 2, -150.0 + side_deg / 2
    # clamp to grid
    lat_lo, lat_hi = max(lat_lo, -89.9), min(lat_hi, 89.9)
    lon_lo, lon_hi = max(lon_lo, -179.9), min(lon_hi, 179.9)

    rss0 = _rss_mb()
    t0 = time.monotonic()
    sub = ds.sel(lat=slice(lat_lo, lat_hi), lon=slice(lon_lo, lon_hi))
    arr = sub["elevation"].values  # materialise
    raw_cells = int(arr.size)
    elapsed = time.monotonic() - t0
    rss1 = _rss_mb()
    del arr, sub
    return raw_cells, elapsed, rss1 - rss0


def _polygon_probe(ds, side_deg: float) -> tuple[int, float, float]:
    """Mask a polygon inscribed in the square bbox (~95% mask ratio)."""
    import shapely
    from shapely.geometry import Polygon

    lat_lo, lat_hi = -30.0 - side_deg / 2, -30.0 + side_deg / 2
    lon_lo, lon_hi = -150.0 - side_deg / 2, -150.0 + side_deg / 2
    lat_lo, lat_hi = max(lat_lo, -89.9), min(lat_hi, 89.9)
    lon_lo, lon_hi = max(lon_lo, -179.9), min(lon_hi, 179.9)

    inset = 0.025 * side_deg
    poly = Polygon([
        (lon_lo + inset, lat_lo + inset),
        (lon_lo + inset, lat_hi - inset),
        (lon_hi - inset, lat_hi - inset),
        (lon_hi - inset, lat_lo + inset),
        (lon_lo + inset, lat_lo + inset),
    ])

    rss0 = _rss_mb()
    t0 = time.monotonic()
    sub = ds.sel(lat=slice(lat_lo, lat_hi), lon=slice(lon_lo, lon_hi))
    lons, lats = np.meshgrid(sub["lon"].values, sub["lat"].values)
    points = shapely.points(lons.ravel(), lats.ravel())
    mask = shapely.contains(poly, points).reshape(lons.shape)
    elev = sub["elevation"].values[mask]
    raw_cells = int(lons.size)
    masked = int(elev.size)
    elapsed = time.monotonic() - t0
    rss1 = _rss_mb()
    del elev, mask, points, lons, lats, sub
    return raw_cells, elapsed, rss1 - rss0


def _side_for_cells(target_cells: float, arc: int = 240) -> float:
    """side_deg such that side² * arc² ≈ target_cells (square bbox)."""
    return math.sqrt(target_cells) / arc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    p.add_argument("--zarr", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    p.add_argument("--targets", type=str,
                   default="1e5,1e6,1e7,5e7,1e8,2.5e8,5e8",
                   help="comma-separated target raw cell counts")
    args = p.parse_args()

    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    print(f"[open] {args.zarr}")
    ds = _open_zarr(args.zarr)

    targets = [float(s) for s in args.targets.split(",")]

    print()
    print(f"{'target':>12s}  {'side(°)':>9s}  | "
          f"{'line_cells':>12s}  {'line_s':>7s}  {'line_RSS_MB':>11s}  | "
          f"{'poly_cells':>12s}  {'poly_s':>7s}  {'poly_RSS_MB':>11s}")
    print("-" * 110)

    for t in targets:
        side = _side_for_cells(t)
        try:
            l_cells, l_s, l_rss = _line_probe(ds, side)
        except MemoryError:
            l_cells, l_s, l_rss = 0, float("inf"), float("inf")
        try:
            p_cells, p_s, p_rss = _polygon_probe(ds, side)
        except MemoryError:
            p_cells, p_s, p_rss = 0, float("inf"), float("inf")
        print(f"{t:>12.1e}  {side:>9.3f}  | "
              f"{l_cells:>12d}  {l_s:>7.3f}  {l_rss:>11.1f}  | "
              f"{p_cells:>12d}  {p_s:>7.3f}  {p_rss:>11.1f}")

    print()
    print("Cap-picking heuristic (see specs/v0.5.3_hardening_checklist_v2.md §3):")
    print("  * MAX_BBOX_CELLS_LINE = lowest cell count where line_s > 1.0 OR line_RSS > 800 MB")
    print("  * MAX_POLYGON_CELLS   = lowest cell count where poly_s > 2.0 OR poly_RSS > 1200 MB")
    print()
    print("Then update the fallback defaults in src/config.py and record the")
    print("chosen values + this table in dev2026/TESTING.md Phase D.")

    ds.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
