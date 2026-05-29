"""Phase D2 — polygon cap probe for frontend-like shapes.

This complements `probe_bbox_limits.py`:

* Phase D (square bbox) gives a worst-case memory slope.
* Phase D2 probes a few frontend-like polygon archetypes and measures
  where the polygon mask path becomes operationally unsafe.

The script prints only scalar summaries; it never emits large payloads.
"""
from __future__ import annotations

import argparse
import gc
import math
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

warnings.simplefilter("ignore")

_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
_ARC = 240
_LAT0 = -30.0
_LON0 = -150.0
_CROSS180_LAT0 = -17.0


@dataclass(frozen=True)
class ProbeThresholds:
    slow_s: float = 2.0
    rss_mb: float = 1200.0
    stop_s: float = 5.0
    stop_rss_mb: float = 1500.0
    max_est_rows_sample5: int = 1_250_000


@dataclass
class ProbeResult:
    shape_id: str
    raw_bbox_cells: int
    mask_ratio: float
    inside_cells: int
    est_rows_sample5: int
    wall_s: float
    rss_delta_mb: float
    status: str


def _open_zarr(path: Path):
    import xarray as xr

    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


def _rss_mb() -> float:
    import psutil

    return psutil.Process().memory_info().rss / (1024 * 1024)


def _status(wall_s: float, rss_mb: float, limits: ProbeThresholds) -> str:
    slow = wall_s > limits.slow_s
    high = rss_mb > limits.rss_mb
    if slow and high:
        return "both"
    if slow:
        return "slow"
    if high:
        return "rss_high"
    return "ok"


def _bbox_cells(bounds: tuple[float, float, float, float]) -> int:
    minx, miny, maxx, maxy = bounds
    return int(round((maxx - minx) * (maxy - miny) * _ARC * _ARC))


def _size_from_cells(raw_cells: float, aspect: float) -> tuple[float, float]:
    area_deg2 = raw_cells / (_ARC * _ARC)
    width = math.sqrt(area_deg2 * aspect)
    height = area_deg2 / width
    return width, height


def _dense_rect(raw_cells: float):
    from shapely.geometry import Polygon

    width, height = _size_from_cells(raw_cells, aspect=1.0)
    x0, x1 = _LON0 - width / 2, _LON0 + width / 2
    y0, y1 = _LAT0 - height / 2, _LAT0 + height / 2
    return Polygon([(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)])


def _thin_rect(raw_cells: float, *, center_lon: float = _LON0):
    from shapely.geometry import LineString

    width, height = _size_from_cells(raw_cells, aspect=1.0)
    x0, x1 = center_lon - width / 2, center_lon + width / 2
    y0, y1 = _LAT0 - height / 2, _LAT0 + height / 2
    line = LineString([(x0, y0), (x1, y1)])
    # Diagonal ribbon that spans the full bbox but occupies only ~10% of it.
    return line.buffer(height * 0.018, cap_style=2, join_style=2)


def _coastal_jagged(raw_cells: float):
    from shapely.geometry import Polygon

    width, height = _size_from_cells(raw_cells, aspect=6.0)
    x0, x1 = _LON0 - width / 2, _LON0 + width / 2
    y0, y1 = _LAT0 - height / 2, _LAT0 + height / 2
    step = width / 6.0
    amp = height * 0.18
    coords = [
        (x0, y0),
        (x0, y1),
        (x0 + step, y1 - amp),
        (x0 + 2 * step, y1),
        (x0 + 3 * step, y1 - amp),
        (x0 + 4 * step, y1),
        (x0 + 5 * step, y1 - amp),
        (x1, y1),
        (x1, y0),
        (x1 - 2 * step, y0 + amp * 0.6),
        (x1 - 4 * step, y0),
        (x0, y0),
    ]
    return Polygon(coords)


def _cross180_parts(raw_cells: float):
    from shapely.geometry import LineString, Polygon
    from shapely.ops import split

    width, height = _size_from_cells(raw_cells, aspect=1.0)
    x0, x1 = 180.0 - width / 2, 180.0 + width / 2
    y0, y1 = _CROSS180_LAT0 - height / 2, _CROSS180_LAT0 + height / 2
    line = LineString([(x0, y0), (x1, y1)])
    poly360 = line.buffer(height * 0.018, cap_style=2, join_style=2)
    splitter = LineString([(180.0, 90.0), (180.0, -90.0)])
    pieces = split(poly360, splitter)

    out = []
    for part in pieces.geoms:
        coords = []
        for lon, lat in part.exterior.coords:
            lon180 = lon - 360.0 if lon > 180.0 else lon
            coords.append((lon180, lat))
        out.append(Polygon(coords))
    return out


def _probe_polygon(ds, polygon) -> tuple[int, int, float, float]:
    import shapely

    minx, miny, maxx, maxy = shapely.bounds(polygon)
    rss0 = _rss_mb()
    t0 = time.monotonic()
    sub = ds.sel(lat=slice(miny, maxy), lon=slice(minx, maxx))
    lons, lats = np.meshgrid(sub["lon"].values, sub["lat"].values)
    points = shapely.points(lons.ravel(), lats.ravel())
    mask = shapely.contains(polygon, points).reshape(lons.shape)
    inside = int(mask.sum())
    raw_cells = int(lons.size)
    _ = sub["elevation"].values[mask]
    wall_s = time.monotonic() - t0
    rss1 = _rss_mb()
    del _, mask, points, lons, lats, sub
    gc.collect()
    return raw_cells, inside, wall_s, (rss1 - rss0)


def _run_shape(ds, shape_id: str, raw_cells_target: float, limits: ProbeThresholds) -> ProbeResult:
    if shape_id == "dense_rect":
        polygons = [_dense_rect(raw_cells_target)]
    elif shape_id == "thin_rect":
        polygons = [_thin_rect(raw_cells_target)]
    elif shape_id == "coastal":
        polygons = [_coastal_jagged(raw_cells_target)]
    elif shape_id == "cross180_thin":
        polygons = _cross180_parts(raw_cells_target)
    else:
        raise ValueError(f"unknown shape_id: {shape_id}")

    total_raw = 0
    total_inside = 0
    wall_total = 0.0
    rss_peak = 0.0
    for poly in polygons:
        raw, inside, wall_s, rss_mb = _probe_polygon(ds, poly)
        total_raw += raw
        total_inside += inside
        wall_total += wall_s
        rss_peak = max(rss_peak, rss_mb)

    mask_ratio = (total_inside / total_raw) if total_raw else 0.0
    est_rows = int(round(total_inside / 25.0))
    status = _status(wall_total, rss_peak, limits)
    if wall_total > limits.stop_s or rss_peak > limits.stop_rss_mb or est_rows > limits.max_est_rows_sample5:
        if status == "ok":
            status = "stop"
    return ProbeResult(
        shape_id=shape_id,
        raw_bbox_cells=total_raw,
        mask_ratio=mask_ratio,
        inside_cells=total_inside,
        est_rows_sample5=est_rows,
        wall_s=wall_total,
        rss_delta_mb=rss_peak,
        status=status,
    )


def _is_bad(result: ProbeResult, limits: ProbeThresholds) -> bool:
    return (
        result.wall_s > limits.slow_s
        or result.rss_delta_mb > limits.rss_mb
        or result.est_rows_sample5 > limits.max_est_rows_sample5
    )


def _search_shape(ds, shape_id: str, start_cells: float, max_cells: float, limits: ProbeThresholds):
    runs: list[ProbeResult] = []
    lo = None
    hi = None
    current = start_cells
    for _ in range(8):
        result = _run_shape(ds, shape_id, current, limits)
        runs.append(result)
        if _is_bad(result, limits):
            hi = current
            break
        lo = current
        if current >= max_cells:
            break
        current = min(current * 2.0, max_cells)

    if lo is not None and hi is not None:
        for _ in range(3):
            mid = math.sqrt(lo * hi)
            result = _run_shape(ds, shape_id, mid, limits)
            runs.append(result)
            if _is_bad(result, limits):
                hi = mid
            else:
                lo = mid

    runs.sort(key=lambda r: r.raw_bbox_cells)
    return runs


def _summary(runs: list[ProbeResult]):
    safe = None
    bad = None
    for r in runs:
        if _is_bad(r, ProbeThresholds()):
            bad = r
            break
        safe = r
    return safe, bad


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    p.add_argument("--zarr", type=Path, default=None)
    p.add_argument("--start-cells", type=float, default=1e6)
    p.add_argument("--max-cells", type=float, default=2.5e8)
    args = p.parse_args()

    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    ds = _open_zarr(args.zarr)
    limits = ProbeThresholds()
    shape_ids = ["dense_rect", "thin_rect", "coastal", "cross180_thin"]

    print(f"[open] {args.zarr}")
    print("[limits] slow>2.0s rss>1200MB stop>5.0s/1500MB est_rows_sample5>1.25M")
    print()

    all_runs: dict[str, list[ProbeResult]] = {}
    for shape_id in shape_ids:
        print(f"=== {shape_id} ===")
        runs = _search_shape(ds, shape_id, args.start_cells, args.max_cells, limits)
        all_runs[shape_id] = runs
        for r in runs:
            print(
                f"  raw={r.raw_bbox_cells:>10,d}  mask={r.mask_ratio:>6.3f}  "
                f"est_rows5={r.est_rows_sample5:>8,d}  wall={r.wall_s:>5.3f}s  "
                f"rss={r.rss_delta_mb:>7.1f}MB  status={r.status}"
            )
        safe, bad = _summary(runs)
        print("  summary:")
        print(
            f"    safe_up_to={safe.raw_bbox_cells:,}" if safe else "    safe_up_to=none"
        )
        print(
            f"    first_bad={bad.raw_bbox_cells:,}  limiting="
            f"{'rows' if bad and bad.est_rows_sample5 > limits.max_est_rows_sample5 else ('rss/time' if bad else 'n/a')}"
            if bad else "    first_bad=not found within max_cells"
        )
        print()

    print("=== phase_d2_summary ===")
    print("| shape | safe_up_to_raw_cells | first_bad_raw_cells | mask_ratio_at_knee | est_rows_sample5_at_knee | limiting_factor |")
    print("|---|---:|---:|---:|---:|---|")
    for shape_id in shape_ids:
        safe, bad = _summary(all_runs[shape_id])
        mask = f"{bad.mask_ratio:.3f}" if bad else "n/a"
        rows = f"{bad.est_rows_sample5:,}" if bad else "n/a"
        limiting = (
            "rows"
            if bad and bad.est_rows_sample5 > limits.max_est_rows_sample5
            else ("rss/time" if bad else "not found")
        )
        print(
            f"| {shape_id} | "
            f"{safe.raw_bbox_cells if safe else 'none'} | "
            f"{bad.raw_bbox_cells if bad else 'not found'} | "
            f"{mask} | {rows} | {limiting} |"
        )

    ds.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
