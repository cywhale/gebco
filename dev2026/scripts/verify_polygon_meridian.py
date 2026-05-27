"""Polygon-mask consistency + cross-meridian regression for 2023 vs 2026 Zarr.

Scope (and what this script is NOT):

  This script answers the geophysical-consistency question — given the same
  GeoJSON polygon and the same grid, does the 2026 store produce the same
  set of cell centres and z-values (within tolerance) as the 2023 store? It
  runs at FULL resolution (no down-sampling), so its row counts are larger
  than what the public `/gebco?jsonsrc=...` endpoint actually returns. The
  endpoint defaults `sample=5` (a.k.a. `poly_sample=5`) and downsamples the
  bbox subset every 5th cell to keep response payloads small.

  → For the user-visible endpoint regression with the production
    `sample=5` default, see `verify_polyhandler_endpoint.py`. That script
    imports the REAL `src.polyhandler.polyhandler()`.

  → This script intentionally avoids importing polyhandler/polars so it can
    run in lean environments (e.g. a sandbox without polars). It replicates
    polyhandler's data flow at the *mask* level with shapely 2.x + xarray:

        bbox = polygon.bounds
        subset = ds.sel(lon=slice(minx,maxx), lat=slice(miny,maxy))  # no step
        lons, lats = np.meshgrid(subset.lon, subset.lat)
        mask = shapely.contains(polygon, shapely.points(lons.ravel(), lats.ravel()))
        cells = (lons[mask], lats[mask], subset.elevation.values[mask])

What it covers:

  Polygon mask (src/polyhandler.py data path, NO sample=5):
      Polygon / MultiPolygon / FeatureCollection bbox-subset → mesh-grid →
      shapely.contains mask. This isolates the question "does the 2026 grid
      land the same cells inside the polygon as 2023, and how do z-values
      differ?" — the grid is unchanged between the two releases.

  Cross-meridian line (src/xmeridian.py):
      For multi-point `lon=...&lat=...` line queries that cross 0° (prime
      meridian) or 180° (anti-meridian), `crossBoundary` inserts intermediate
      cell-centre breakpoints on both sides so the underlying gridded read
      stays within a single hemisphere. T5/T6 call `src.zprofile.zprofile()`
      directly — the SAME function the FastAPI endpoint dispatches to.

Test cases (each run against BOTH 2023 and 2026):
  T1  Polygon — small bbox in Taiwan area (well-mapped, expect very close)
  T2  Polygon — small bbox over Greenland coast (BedMachine v6 territory —
      cell coords identical, z values may shift more)
  T3  FeatureCollection — two non-overlapping polygons in one request
  T4  Polygon crossing the 180° meridian (Fiji area) — exercises the
      "split at 180" branch via shapely.ops.split
  T5  Line crossing 0° meridian (Gulf of Guinea)
  T6  Line crossing 180° meridian (mid-Pacific)

Token-aware output: one summary line per test, plus per-test pass/fail and
diff stats; mismatch details only when an unexpected divergence happens.

Usage:
    uv run python scripts/verify_polygon_meridian.py
    uv run python scripts/verify_polygon_meridian.py --pass-threshold 200 --pass-percentile 95
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np

warnings.simplefilter("ignore")
import dask
dask.config.set(scheduler="single-threaded")

_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]


def _open_zarr(path: Path):
    import xarray as xr
    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


# --- direct polygon-mask query (mirrors polyhandler's data flow) -----------

def _polygon_cells(ds, polygon, arc: int = 240) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (lons, lats, z) for the grid cells whose centres lie inside `polygon`.

    Mirrors what src/polyhandler.process_polygon_part() does:
      - bbox-subset the Zarr
      - mesh-grid the cell centres in the subset
      - mask to cells INSIDE the polygon
      - return the cell coords + elevation values
    """
    import shapely
    minx, miny, maxx, maxy = polygon.bounds
    # Same buffer polyhandler/zprofile use to make sure cells on the edge get included.
    lftx = minx - 0.25 / arc
    rgtx = min(maxx + 1.5 / arc, 180 - 0.01 / arc)
    boty = miny - 0.25 / arc
    topy = maxy + 1.5 / arc
    sub = ds.sel(lon=slice(lftx, rgtx), lat=slice(boty, topy))
    if sub.sizes["lat"] == 0 or sub.sizes["lon"] == 0:
        return (np.empty(0), np.empty(0), np.empty(0, dtype=np.int64))
    lon_vals = sub["lon"].values
    lat_vals = sub["lat"].values
    lons, lats = np.meshgrid(lon_vals, lat_vals)
    pts = shapely.points(lons.ravel(), lats.ravel())
    mask = shapely.contains(polygon, pts).reshape(lons.shape)
    z = sub["elevation"].values[mask]
    return lons[mask], lats[mask], z.astype(np.int64)


def _query_polygon_geojson(ds, geojson: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resolve a GeoJSON (Polygon / Feature / FeatureCollection) into the
    union of all polygon-mask cells. Concatenates results across features."""
    import shapely
    from shapely.geometry import shape as _shape
    from shapely.ops import split as _split
    from shapely.geometry import LineString as _LineString

    geometries: list = []
    if geojson["type"] == "FeatureCollection":
        for feat in geojson["features"]:
            geometries.append(_shape(feat["geometry"]))
    elif geojson["type"] == "Feature":
        geometries.append(_shape(geojson["geometry"]))
    else:
        geometries.append(_shape(geojson))

    all_lons, all_lats, all_z = [], [], []
    for geom in geometries:
        minx, _, maxx, _ = geom.bounds
        crosses_180 = minx < -90 and maxx > 90  # heuristic mirror of polyhandler
        if crosses_180:
            # Shift to 0..360, split at 180, shift each piece back to -180..180
            from shapely.geometry import Polygon
            shifted_coords = [((x + 360) if x < 0 else x, y)
                              for x, y in shapely.get_coordinates(geom)]
            shifted = Polygon(shifted_coords)
            splitter = _LineString([(180, 90), (180, -90)])
            for part in _split(shifted, splitter).geoms:
                if part.is_empty:
                    continue
                # Decide which side of the dateline this part belongs to
                # by looking at the centroid of its x-coords.
                xs = [x for x, _ in shapely.get_coordinates(part)]
                is_right_part = sum(xs) / len(xs) > 180.0
                # Shift x back to -180..180. For the right part, force the
                # x==180 boundary vertex to -180 so the bbox stays in
                # negative territory (otherwise minx=-179, maxx=180 wraps the
                # whole world and ds.sel() pulls a 10M-cell strip).
                if is_right_part:
                    part_coords = [((x - 360) if x >= 180 else x, y)
                                   for x, y in shapely.get_coordinates(part)]
                else:
                    part_coords = [(x, y) for x, y in shapely.get_coordinates(part)]
                from shapely.geometry import Polygon as _Poly
                part_back = _Poly(part_coords)
                lon, lat, z = _polygon_cells(ds, part_back)
                all_lons.append(lon); all_lats.append(lat); all_z.append(z)
        else:
            lon, lat, z = _polygon_cells(ds, geom)
            all_lons.append(lon); all_lats.append(lat); all_z.append(z)

    if not all_lons:
        return (np.empty(0), np.empty(0), np.empty(0, dtype=np.int64))
    return (np.concatenate(all_lons),
            np.concatenate(all_lats),
            np.concatenate(all_z))


def _sort_by_coord(lons, lats, z):
    """Sort cells by (lon, lat) so comparisons are deterministic across runs."""
    order = np.lexsort((lats, lons))
    return lons[order], lats[order], z[order]


# --- test case definitions ------------------------------------------------

CASE_T1_TAIWAN_POLY = {
    "id": "T1",
    "kind": "polygon",
    "label": "Polygon — small box NE of Taiwan",
    "geojson": {
        "type": "Polygon",
        "coordinates": [[[121.5, 23.0], [121.5, 24.0],
                          [122.5, 24.0], [122.5, 23.0],
                          [121.5, 23.0]]],
    },
}

CASE_T2_GREENLAND_POLY = {
    "id": "T2",
    "kind": "polygon",
    "label": "Polygon — Greenland coast (BedMachine v6 region)",
    "geojson": {
        "type": "Polygon",
        "coordinates": [[[-45.0, 70.0], [-45.0, 71.0],
                          [-43.0, 71.0], [-43.0, 70.0],
                          [-45.0, 70.0]]],
    },
}

CASE_T3_FEATCOL = {
    "id": "T3",
    "kind": "polygon",
    "label": "FeatureCollection — two polygons in one request",
    "geojson": {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[120.0, 22.0], [120.0, 22.5],
                                           [120.5, 22.5], [120.5, 22.0],
                                           [120.0, 22.0]]]}},
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[140.0, 35.0], [140.0, 35.5],
                                           [140.5, 35.5], [140.5, 35.0],
                                           [140.0, 35.0]]]}},
        ],
    },
}

CASE_T4_X180_POLY = {
    "id": "T4",
    "kind": "polygon",
    "label": "Polygon crossing 180° meridian (Fiji, 1°×0.5°)",
    # Intentionally narrow: width 1° straddling the 180° line, height 0.5°.
    # That's ~288×120 = ~35 k cells masked, comfortable in 4 GB sandbox RAM.
    "geojson": {
        "type": "Polygon",
        "coordinates": [[[179.5, -17.5], [179.5, -17.0],
                          [-179.5, -17.0], [-179.5, -17.5],
                          [179.5, -17.5]]],
    },
}

CASE_T5_LINE_X0 = {
    "id": "T5",
    "kind": "line",
    "label": "Line crossing 0° meridian (Gulf of Guinea)",
    "lons": [-3.0, -1.0, 1.0, 3.0],
    "lats": [2.0, 2.0, 2.0, 2.0],
}

CASE_T6_LINE_X180 = {
    "id": "T6",
    "kind": "line",
    "label": "Line crossing 180° meridian (mid-Pacific)",
    "lons": [175.0, 179.0, -179.0, -175.0],
    "lats": [-10.0, -10.0, -10.0, -10.0],
}

ALL_CASES = [CASE_T1_TAIWAN_POLY, CASE_T2_GREENLAND_POLY, CASE_T3_FEATCOL,
             CASE_T4_X180_POLY, CASE_T5_LINE_X0, CASE_T6_LINE_X180]


# --- runner ----------------------------------------------------------------

def _diff_stats(a: np.ndarray, b: np.ndarray) -> dict:
    diff = a.astype(np.float64) - b.astype(np.float64)
    abs_diff = np.abs(diff)
    return {
        "n": int(a.size),
        "exact": int((abs_diff == 0).sum()),
        "max": float(abs_diff.max()) if abs_diff.size else 0.0,
        "mean": float(abs_diff.mean()) if abs_diff.size else 0.0,
        "p50": float(np.percentile(abs_diff, 50)) if abs_diff.size else 0.0,
        "p95": float(np.percentile(abs_diff, 95)) if abs_diff.size else 0.0,
        "p99": float(np.percentile(abs_diff, 99)) if abs_diff.size else 0.0,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    p.add_argument("--zarr-old", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2023_sub_ice_topo.zarr")
    p.add_argument("--zarr-new", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    p.add_argument("--pass-percentile", type=float, default=95.0)
    p.add_argument("--pass-threshold", type=float, default=200.0,
                   help="metres of elevation difference allowed at chosen percentile")
    args = p.parse_args()

    if args.zarr_old is None:
        args.zarr_old = args.repo_root / "data" / "GEBCO_2023_sub_ice_topo.zarr"
    if args.zarr_new is None:
        args.zarr_new = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    sys.path.insert(0, str(args.repo_root))
    import xarray as xr  # noqa: F401
    import src.config as config

    print("[scope] polygon MASK consistency (no sample=5 down-sampling) + cross-meridian lines")
    print("[scope] for endpoint-level regression with production sample=5, see verify_polyhandler_endpoint.py")
    print(f"[open] OLD {args.zarr_old}")
    ds_old = _open_zarr(args.zarr_old)
    print(f"[open] NEW {args.zarr_new}")
    ds_new = _open_zarr(args.zarr_new)
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90

    all_ok = True
    summary_rows = []

    for case in ALL_CASES:
        print()
        print(f"=== {case['id']} : {case['label']} ===")

        try:
            t0 = time.time()
            if case["kind"] == "polygon":
                lon_o, lat_o, z_o = _query_polygon_geojson(ds_old, case["geojson"])
                lon_o, lat_o, z_o = _sort_by_coord(lon_o, lat_o, z_o)
            else:
                from src.zprofile import zprofile
                config.ds = ds_old
                resp = zprofile(np.array(case["lons"]), np.array(case["lats"]), "", 1)
                body = json.loads(resp.body)
                lon_o = np.asarray(body["longitude"], dtype=np.float64)
                lat_o = np.asarray(body["latitude"], dtype=np.float64)
                z_o = np.asarray(body["z"], dtype=np.int64)
            t_old = time.time() - t0
        except Exception as exc:
            print(f"  [{case['id']}] OLD raised: {type(exc).__name__}: {exc}")
            all_ok = False
            summary_rows.append((case["id"], "FAIL", "OLD raised"))
            continue

        try:
            t0 = time.time()
            if case["kind"] == "polygon":
                lon_n, lat_n, z_n = _query_polygon_geojson(ds_new, case["geojson"])
                lon_n, lat_n, z_n = _sort_by_coord(lon_n, lat_n, z_n)
            else:
                from src.zprofile import zprofile
                config.ds = ds_new
                resp = zprofile(np.array(case["lons"]), np.array(case["lats"]), "", 1)
                body = json.loads(resp.body)
                lon_n = np.asarray(body["longitude"], dtype=np.float64)
                lat_n = np.asarray(body["latitude"], dtype=np.float64)
                z_n = np.asarray(body["z"], dtype=np.int64)
            t_new = time.time() - t0
        except Exception as exc:
            print(f"  [{case['id']}] NEW raised: {type(exc).__name__}: {exc}")
            all_ok = False
            summary_rows.append((case["id"], "FAIL", "NEW raised"))
            continue

        ok = True

        # 1) row count must match — grid + polygon mask are deterministic and
        #    grid is identical between 2023 / 2026
        if z_o.size != z_n.size:
            print(f"  ✗ row count mismatch: OLD={z_o.size}  NEW={z_n.size}")
            ok = False
        else:
            print(f"  rows: OLD={z_o.size}  NEW={z_n.size}  ✓ identical")

        # 2) coords must be identical (cell centres are grid-fixed)
        if ok:
            max_lon_drift = float(np.abs(lon_o - lon_n).max()) if lon_o.size else 0.0
            max_lat_drift = float(np.abs(lat_o - lat_n).max()) if lat_o.size else 0.0
            if max_lon_drift > 1e-9 or max_lat_drift > 1e-9:
                print(f"  ✗ coord drift: lon max={max_lon_drift:.2e}  lat max={max_lat_drift:.2e}")
                ok = False
            else:
                print(f"  coords: identical (max drift lon/lat = {max_lon_drift:.0e}/{max_lat_drift:.0e})")

        # 3) z diff distribution
        if ok:
            stats = _diff_stats(z_n, z_o)
            pct_at = float(np.percentile(np.abs(z_n.astype(np.int64) - z_o.astype(np.int64)),
                                          args.pass_percentile))
            band_pass = pct_at <= args.pass_threshold
            print(f"  z diff: n={stats['n']}  exact={stats['exact']}/{stats['n']}  "
                  f"mean={stats['mean']:.1f}  P50={stats['p50']:.0f}  "
                  f"P95={stats['p95']:.0f}  P99={stats['p99']:.0f}  max={stats['max']:.0f}")
            print(f"  pass: P{int(args.pass_percentile)} = {pct_at:.0f} m "
                  f"({'≤' if band_pass else '>'} threshold {args.pass_threshold:.0f} m)  "
                  f"{'✓ PASS' if band_pass else '✗ FAIL'}")
            ok = band_pass

        print(f"  timing: OLD={t_old:.3f}s  NEW={t_new:.3f}s")
        summary_rows.append((case["id"], "OK" if ok else "FAIL",
                             f"n={z_n.size if z_o.size==z_n.size else f'{z_o.size}/{z_n.size}'}"))
        if not ok:
            all_ok = False

    print()
    print("=== summary (polygon MASK consistency + cross-meridian lines) ===")
    for cid, status, extra in summary_rows:
        print(f"  {cid}  {status:<4s}  {extra}")
    print("note: T1-T4 row counts here are full-resolution (no sample=5);")
    print("      see verify_polyhandler_endpoint.py for the public endpoint regression.")
    ds_old.close()
    ds_new.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
