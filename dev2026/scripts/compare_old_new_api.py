"""Step 3: compare OLD (2023) API vs NEW (2026) API on the same query set.

The two versions of the API are byte-identical except for the Zarr path they
open (see gebco_app.py:lifespan). We therefore swap `src.config.ds` between
the 2023 and 2026 stores and invoke the same `src.zprofile.zprofile()`
function used by the FastAPI endpoint. This is equivalent to running two
separate uvicorn instances and is what we use here to stay within the
sandbox's 3.8 GB RAM (two simultaneous uvicorn + dask + multiprocessing
servers would OOM).

The 2026 grid is built from new SRTM15+ v2.8 (SWOT-derived gravity + ML
bathymetry), updated BedMachine GL/Antarctica releases, etc. We therefore
expect:

  * Most ocean/land points: differences should be small (< 10 m for the
    well-mapped multibeam areas).
  * Some interpolated / poorly-mapped points: differences may reach hundreds
    of metres.
  * Around Greenland/Antarctica ice areas: a few points may shift by 1000+
    metres because BedMachine version bumped.

Pass criterion (configurable via --pass-percentile / --pass-threshold):
  Default: 95 % of sampled points must have |Δ| < 200 m.
  We also always print percentile bands (50/75/90/95/99/100) so the user can
  judge tail behaviour even if the chosen pass threshold is debatable.
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT,
                   help=f"repo root containing data/ "
                        f"(default: auto-derived from this script's path → {_REPO_ROOT_DEFAULT})")
    p.add_argument("--zarr-old", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2023_sub_ice_topo.zarr")
    p.add_argument("--zarr-new", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    p.add_argument("--total-points", type=int, default=1000)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--strata", choices=["global", "risk"], default="global",
                   help="sampling strategy. 'global' = uniform over the whole "
                        "grid (default; gives an unbiased per-point picture but "
                        "≤1 % of samples land on the ice sheets). 'risk' = "
                        "stratified: 25%% Greenland (60..84 N, 75..10 W), 25%% "
                        "Antarctica (south of 60 S), 50%% global uniform — use "
                        "this to actually verify behaviour over BedMachine-"
                        "updated areas before claiming they 'held up cleanly'.")
    p.add_argument("--pass-percentile", type=float, default=95.0,
                   help="percentile of |Δ| that must be ≤ pass_threshold")
    p.add_argument("--pass-threshold", type=float, default=200.0,
                   help="metres of elevation difference allowed at the chosen percentile")
    p.add_argument("--show-tail", type=int, default=10,
                   help="print this many largest-|Δ| outliers with their coords")
    args = p.parse_args()

    if args.zarr_old is None:
        args.zarr_old = args.repo_root / "data" / "GEBCO_2023_sub_ice_topo.zarr"
    if args.zarr_new is None:
        args.zarr_new = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    sys.path.insert(0, str(args.repo_root))
    import xarray as xr
    import src.config as config

    print(f"[open] OLD zarr {args.zarr_old}")
    ds_old = xr.open_zarr(str(args.zarr_old), chunks="auto",
                          decode_cf=False, decode_times=False)
    print(f"[open] NEW zarr {args.zarr_new}")
    ds_new = xr.open_zarr(str(args.zarr_new), chunks="auto",
                          decode_cf=False, decode_times=False)
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90

    from src.zprofile import zprofile

    def run_api(ds: xr.Dataset, lon: float, lat: float) -> tuple[float, float, int]:
        config.ds = ds
        resp = zprofile(np.array([lon]), np.array([lat]), "point", 1)
        d = json.loads(resp.body)
        return float(d["longitude"][0]), float(d["latitude"][0]), int(d["z"][0])

    rng = np.random.default_rng(args.seed)
    strata_label_per_point: list[str]
    if args.strata == "global":
        lats = rng.uniform(-89.9, 89.9, size=args.total_points)
        lons = rng.uniform(-179.9, 179.9, size=args.total_points)
        strata_label_per_point = ["global"] * args.total_points
    elif args.strata == "risk":
        n_gl = args.total_points // 4         # Greenland: 25 %
        n_an = args.total_points // 4         # Antarctica: 25 %
        n_glob = args.total_points - n_gl - n_an
        lat_gl = rng.uniform(60.0, 83.9, size=n_gl)
        lon_gl = rng.uniform(-74.9, -10.1, size=n_gl)
        lat_an = rng.uniform(-89.9, -60.0, size=n_an)
        lon_an = rng.uniform(-179.9, 179.9, size=n_an)
        lat_g = rng.uniform(-89.9, 89.9, size=n_glob)
        lon_g = rng.uniform(-179.9, 179.9, size=n_glob)
        lats = np.concatenate([lat_gl, lat_an, lat_g])
        lons = np.concatenate([lon_gl, lon_an, lon_g])
        strata_label_per_point = (
            ["greenland"] * n_gl + ["antarctica"] * n_an + ["global"] * n_glob
        )
        # Shuffle so OLD/NEW timing isn't biased by region order
        order = rng.permutation(args.total_points)
        lats = lats[order]
        lons = lons[order]
        strata_label_per_point = [strata_label_per_point[i] for i in order]
        print(f"[strata] risk: greenland={n_gl}  antarctica={n_an}  global={n_glob}")
    else:  # unreachable due to argparse choices
        raise SystemExit(f"unknown --strata={args.strata!r}")

    z_old = np.empty(args.total_points, dtype=np.int64)
    z_new = np.empty(args.total_points, dtype=np.int64)
    cell_lon = np.empty(args.total_points, dtype=np.float64)
    cell_lat = np.empty(args.total_points, dtype=np.float64)

    t_old = t_new = 0.0
    t0 = time.time()
    print(f"[query] {args.total_points} random points, swapping config.ds per call ...")
    for i in range(args.total_points):
        s = time.time()
        lo_o, la_o, zo = run_api(ds_old, lons[i], lats[i])
        t_old += time.time() - s
        z_old[i] = zo

        s = time.time()
        lo_n, la_n, zn = run_api(ds_new, lons[i], lats[i])
        t_new += time.time() - s
        z_new[i] = zn
        cell_lon[i] = lo_n
        cell_lat[i] = la_n

    wall = time.time() - t0
    diff = z_new.astype(np.int64) - z_old.astype(np.int64)
    abs_diff = np.abs(diff)

    # Histogram / percentiles
    pct_levels = [50, 75, 90, 95, 99, 100]
    pct_vals = np.percentile(abs_diff, pct_levels)
    exact_match = int((abs_diff == 0).sum())
    within_10 = int((abs_diff <= 10).sum())
    within_50 = int((abs_diff <= 50).sum())
    within_100 = int((abs_diff <= 100).sum())
    within_200 = int((abs_diff <= 200).sum())
    within_500 = int((abs_diff <= 500).sum())
    within_1000 = int((abs_diff <= 1000).sum())

    print()
    print(f"=== timing ===  wall={wall:.1f}s  per-point: OLD={t_old/args.total_points*1000:.1f}ms  NEW={t_new/args.total_points*1000:.1f}ms")
    print(f"=== |Δ| distribution over N={args.total_points} ===")
    print(f"  exact match (|Δ|==0)    : {exact_match:>5d} ({100*exact_match/args.total_points:5.1f} %)")
    print(f"  |Δ| ≤    10 m           : {within_10:>5d} ({100*within_10/args.total_points:5.1f} %)")
    print(f"  |Δ| ≤    50 m           : {within_50:>5d} ({100*within_50/args.total_points:5.1f} %)")
    print(f"  |Δ| ≤   100 m           : {within_100:>5d} ({100*within_100/args.total_points:5.1f} %)")
    print(f"  |Δ| ≤   200 m           : {within_200:>5d} ({100*within_200/args.total_points:5.1f} %)")
    print(f"  |Δ| ≤   500 m           : {within_500:>5d} ({100*within_500/args.total_points:5.1f} %)")
    print(f"  |Δ| ≤  1000 m           : {within_1000:>5d} ({100*within_1000/args.total_points:5.1f} %)")
    print(f"  mean  |Δ| = {abs_diff.mean():.1f} m   median |Δ| = {int(np.median(abs_diff))} m   max |Δ| = {abs_diff.max()} m")
    print(f"  signed Δ:  mean = {diff.mean():+.1f} m  (positive = new is shallower / higher)")
    print()
    print(f"=== percentiles of |Δ| (m) ===")
    for p, v in zip(pct_levels, pct_vals):
        print(f"  P{p:>3d} : {v:.0f}")

    # Top tail
    if args.show_tail > 0:
        idx_tail = np.argsort(abs_diff)[-args.show_tail:][::-1]
        print()
        print(f"=== top {args.show_tail} largest |Δ| outliers ===")
        for i in idx_tail:
            print(f"  [{strata_label_per_point[i]:>10s}]  "
                  f"lat={cell_lat[i]:>+9.4f}  lon={cell_lon[i]:>+9.4f}  "
                  f"old={z_old[i]:>+6d} m  new={z_new[i]:>+6d} m  Δ={diff[i]:+d} m")

    # Per-stratum breakdown (only useful in stratified modes)
    if args.strata != "global":
        print()
        print("=== per-stratum |Δ| ===")
        labels_arr = np.array(strata_label_per_point)
        for label in sorted(set(strata_label_per_point)):
            mask = labels_arr == label
            sub = abs_diff[mask]
            if sub.size == 0:
                continue
            print(f"  {label:>10s} (n={sub.size:>4d}):  "
                  f"mean={sub.mean():>6.1f}  median={int(np.median(sub)):>5d}  "
                  f"P95={int(np.percentile(sub,95)):>5d}  "
                  f"P99={int(np.percentile(sub,99)):>5d}  "
                  f"max={int(sub.max()):>5d}  "
                  f"exact={int((sub==0).sum())}/{sub.size}")

    # Pass/fail
    pct_at = float(np.percentile(abs_diff, args.pass_percentile))
    ok = pct_at <= args.pass_threshold
    print()
    print(f"=== verdict ===")
    print(f"  P{args.pass_percentile:.0f} of |Δ| = {pct_at:.0f} m   (threshold = {args.pass_threshold:.0f} m)")
    if ok:
        print(f"  ✓ PASS  ({args.pass_percentile:.0f} % of points are within {args.pass_threshold:.0f} m)")
    else:
        print(f"  ✗ FAIL  ({args.pass_percentile:.0f} % of points exceed {args.pass_threshold:.0f} m)")
        print("    The tail above can usually be explained by BedMachine updates near")
        print("    the ice sheets or SRTM15+ v2.8 changes in sparsely-mapped abyssal areas;")
        print("    inspect the outlier list to judge whether it's expected.")

    ds_old.close()
    ds_new.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
