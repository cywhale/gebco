"""End-to-end polygon-mode regression that calls the REAL `src.polyhandler.polyhandler()`.

Why this script exists (vs `verify_polygon_meridian.py`):

  `verify_polygon_meridian.py` replicates polyhandler's data-flow at the
  *mask* level (full-resolution bbox subset → meshgrid → shapely.contains).
  That is a good geophysical consistency check, but it does NOT match what
  the public `/gebco?jsonsrc=...` endpoint actually returns, because the
  endpoint defaults `sample=5` / `poly_sample=5` (see
  [gebco_app.py:167](../../gebco_app.py) → polyhandler → src.zprofile.zdata_bbox
  which applies `ds.sel(lon=..., step=5)`). A reviewer correctly pointed out
  that for the T1 polygon the mask test returns 57600 rows while the real
  endpoint returns 2304 rows — same data, very different downsampled product.

  This script closes that gap by importing the REAL polyhandler() and
  asserting that, for the same GeoJSON + the production `sample=5` default,
  the 2023 and 2026 stores produce:

    1. identical row counts (grid + downsampling are deterministic),
    2. identical (lon, lat) cell coordinates (the grid is unchanged),
    3. z-value diff distribution within a configurable pass band — exactly
       the same pass criterion the geophysical-distribution tests use.

Why it's a separate script (not folded into verify_polygon_meridian.py):

  Importing `src.polyhandler` requires `pygeos` + `polars`. We provide a
  shapely-2.x-backed pygeos shim (`_pygeos_shim.py`); polars is a real
  binary dep. In a normal dev2026 uv env on Mac or Linux, both install
  cleanly via `uv sync` and this script runs as-is. The earlier mask-level
  script intentionally stays shim-free so it can be run anywhere shapely is
  available, including resource-constrained sandboxes where polars install
  is flaky.

Cases:

  T1  Polygon in Taiwan EEZ (well-mapped, expect very close)
  T4  Polygon crossing 180° (Fiji), exercises split-at-180 branch

  These mirror the two strongest cases from `verify_polygon_meridian.py`
  so reviewers can cross-reference numbers between mask-level and
  endpoint-level results.

Usage:
    uv run python scripts/verify_polyhandler_endpoint.py
    uv run python scripts/verify_polyhandler_endpoint.py \
        --sample 5 --pass-percentile 95 --pass-threshold 200
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


def _install_shim_if_needed() -> None:
    """Install the shapely-backed pygeos shim *before* importing polyhandler."""
    try:
        import pygeos  # noqa: F401
        # Real pygeos present (unlikely on cp313). Keep it.
        return
    except Exception:
        pass
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _pygeos_shim import install_shim  # type: ignore
    install_shim()


def _open_zarr(path: Path):
    import xarray as xr
    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


# --- test cases (subset of verify_polygon_meridian.py for cross-reference) -

CASE_T1_TAIWAN_POLY = {
    "id": "T1",
    "label": "Polygon — small box NE of Taiwan (sample=5)",
    "geojson": {
        "type": "Polygon",
        "coordinates": [[[121.5, 23.0], [121.5, 24.0],
                          [122.5, 24.0], [122.5, 23.0],
                          [121.5, 23.0]]],
    },
}

CASE_T4_X180_POLY = {
    "id": "T4",
    "label": "Polygon crossing 180° meridian (Fiji, sample=5)",
    "geojson": {
        "type": "Polygon",
        "coordinates": [[[179.5, -17.5], [179.5, -17.0],
                          [-179.5, -17.0], [-179.5, -17.5],
                          [179.5, -17.5]]],
    },
}

ALL_CASES = [CASE_T1_TAIWAN_POLY, CASE_T4_X180_POLY]


# --- runner ----------------------------------------------------------------

def _run_polyhandler(geojson: dict, sample: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Call the production polyhandler the same way gebco_app.py does:

        polyhandler(json_obj, 0, mode, 1, poly_sample)

    where `1` is the (always-1) line/point sample and `poly_sample == sample`
    is the polygon down-sample. See gebco_app.py:208-210 for the exact call.
    """
    from src.polyhandler import polyhandler
    df, _ = polyhandler(geojson, 0, "", 1, sample)
    lon = df["longitude"].to_numpy().astype(np.float64)
    lat = df["latitude"].to_numpy().astype(np.float64)
    z = df["z"].to_numpy().astype(np.int64)
    return lon, lat, z


def _sort_by_coord(lons, lats, z):
    order = np.lexsort((lats, lons))
    return lons[order], lats[order], z[order]


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
    p.add_argument("--sample", type=int, default=5,
                   help="poly_sample passed to polyhandler (production default = 5)")
    p.add_argument("--pass-percentile", type=float, default=95.0)
    p.add_argument("--pass-threshold", type=float, default=200.0,
                   help="metres of elevation difference allowed at chosen percentile")
    args = p.parse_args()

    if args.zarr_old is None:
        args.zarr_old = args.repo_root / "data" / "GEBCO_2023_sub_ice_topo.zarr"
    if args.zarr_new is None:
        args.zarr_new = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    sys.path.insert(0, str(args.repo_root))
    _install_shim_if_needed()
    import src.config as config

    print(f"[open] OLD {args.zarr_old}")
    ds_old = _open_zarr(args.zarr_old)
    print(f"[open] NEW {args.zarr_new}")
    ds_new = _open_zarr(args.zarr_new)
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90

    print(f"[setup] poly_sample={args.sample}  pass criterion: "
          f"P{int(args.pass_percentile)} ≤ {args.pass_threshold:.0f} m")

    all_ok = True
    summary_rows = []

    for case in ALL_CASES:
        print()
        print(f"=== {case['id']} : {case['label']} ===")

        try:
            t0 = time.time()
            config.ds = ds_old
            lon_o, lat_o, z_o = _run_polyhandler(case["geojson"], args.sample)
            lon_o, lat_o, z_o = _sort_by_coord(lon_o, lat_o, z_o)
            t_old = time.time() - t0
        except Exception as exc:
            print(f"  [{case['id']}] OLD raised: {type(exc).__name__}: {exc}")
            all_ok = False
            summary_rows.append((case["id"], "FAIL", "OLD raised"))
            continue

        try:
            t0 = time.time()
            config.ds = ds_new
            lon_n, lat_n, z_n = _run_polyhandler(case["geojson"], args.sample)
            lon_n, lat_n, z_n = _sort_by_coord(lon_n, lat_n, z_n)
            t_new = time.time() - t0
        except Exception as exc:
            print(f"  [{case['id']}] NEW raised: {type(exc).__name__}: {exc}")
            all_ok = False
            summary_rows.append((case["id"], "FAIL", "NEW raised"))
            continue

        ok = True

        if z_o.size != z_n.size:
            print(f"  ✗ row count mismatch: OLD={z_o.size}  NEW={z_n.size}")
            ok = False
        else:
            print(f"  rows: OLD={z_o.size}  NEW={z_n.size}  ✓ identical")

        if ok:
            max_lon_drift = float(np.abs(lon_o - lon_n).max()) if lon_o.size else 0.0
            max_lat_drift = float(np.abs(lat_o - lat_n).max()) if lat_o.size else 0.0
            if max_lon_drift > 1e-9 or max_lat_drift > 1e-9:
                print(f"  ✗ coord drift: lon max={max_lon_drift:.2e}  lat max={max_lat_drift:.2e}")
                ok = False
            else:
                print(f"  coords: identical (max drift lon/lat = {max_lon_drift:.0e}/{max_lat_drift:.0e})")

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
    print("=== summary (real polyhandler() with sample=%d) ===" % args.sample)
    for cid, status, extra in summary_rows:
        print(f"  {cid}  {status:<4s}  {extra}")
    ds_old.close()
    ds_new.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
