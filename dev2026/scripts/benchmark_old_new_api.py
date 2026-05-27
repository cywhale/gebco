"""Step 4: benchmark OLD vs NEW API per-point latency.

Methodology:
  * Warm up each store by querying 20 random points BEFORE measuring (so OS
    page cache + dask graph cache aren't a confounding factor).
  * Measure N points, randomly interleaved between OLD and NEW so any drift
    in disk / CPU contention affects both equally.
  * Report mean / median / P95 / P99 / max latency for each, plus the
    speed-ratio (NEW/OLD).

Pass criterion: NEW median latency ≤ OLD median latency × (1 + --tolerance).
Default tolerance = 10 %.
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
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--warmup", type=int, default=20)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--tolerance", type=float, default=0.10,
                   help="allowed slowdown fraction; NEW median ≤ OLD median × (1+tol)")
    args = p.parse_args()

    if args.zarr_old is None:
        args.zarr_old = args.repo_root / "data" / "GEBCO_2023_sub_ice_topo.zarr"
    if args.zarr_new is None:
        args.zarr_new = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    sys.path.insert(0, str(args.repo_root))
    import xarray as xr
    import src.config as config

    print(f"[open] OLD {args.zarr_old}")
    ds_old = xr.open_zarr(str(args.zarr_old), chunks="auto",
                          decode_cf=False, decode_times=False)
    print(f"[open] NEW {args.zarr_new}")
    ds_new = xr.open_zarr(str(args.zarr_new), chunks="auto",
                          decode_cf=False, decode_times=False)
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90

    from src.zprofile import zprofile

    rng = np.random.default_rng(args.seed)
    warm_lats = rng.uniform(-89.9, 89.9, size=args.warmup)
    warm_lons = rng.uniform(-179.9, 179.9, size=args.warmup)
    lats = rng.uniform(-89.9, 89.9, size=args.n)
    lons = rng.uniform(-179.9, 179.9, size=args.n)

    def ping(ds, lon, lat):
        config.ds = ds
        resp = zprofile(np.array([lon]), np.array([lat]), "point", 1)
        return json.loads(resp.body)["z"][0]

    print(f"[warmup] {args.warmup} points per store ...")
    for i in range(args.warmup):
        ping(ds_old, warm_lons[i], warm_lats[i])
        ping(ds_new, warm_lons[i], warm_lats[i])

    # Interleaved measurement
    print(f"[measure] {args.n} points per store (interleaved order) ...")
    t_old = np.empty(args.n, dtype=np.float64)
    t_new = np.empty(args.n, dtype=np.float64)
    # Randomise which goes first per iteration
    flip = rng.integers(0, 2, size=args.n)
    for i in range(args.n):
        if flip[i] == 0:
            s = time.perf_counter(); ping(ds_old, lons[i], lats[i]); t_old[i] = time.perf_counter() - s
            s = time.perf_counter(); ping(ds_new, lons[i], lats[i]); t_new[i] = time.perf_counter() - s
        else:
            s = time.perf_counter(); ping(ds_new, lons[i], lats[i]); t_new[i] = time.perf_counter() - s
            s = time.perf_counter(); ping(ds_old, lons[i], lats[i]); t_old[i] = time.perf_counter() - s

    def stats(name, t):
        return (f"{name}: "
                f"mean={t.mean()*1000:>7.2f}ms  "
                f"median={np.median(t)*1000:>7.2f}ms  "
                f"P95={np.percentile(t,95)*1000:>7.2f}ms  "
                f"P99={np.percentile(t,99)*1000:>7.2f}ms  "
                f"max={t.max()*1000:>7.2f}ms")

    print()
    print(stats("OLD (2023)", t_old))
    print(stats("NEW (2026)", t_new))
    ratio_mean = t_new.mean() / t_old.mean()
    ratio_med = np.median(t_new) / np.median(t_old)
    print()
    print(f"NEW / OLD ratio:  mean = {ratio_mean:.2f}×   median = {ratio_med:.2f}×")
    pass_speed = ratio_med <= (1 + args.tolerance)
    print()
    print("=== verdict ===")
    print(f"  tolerance = {args.tolerance*100:.0f}%   NEW median allowed ≤ OLD median × {1+args.tolerance:.2f}")
    if pass_speed:
        print(f"  ✓ PASS  NEW is {ratio_med:.2f}× of OLD (within tolerance)")
    else:
        print(f"  ✗ FAIL  NEW is {ratio_med:.2f}× of OLD (exceeds tolerance)")
        print()
        print("  Most likely cause: compressor mismatch.")
        print("    2023 Zarr: Blosc/LZ4 clevel=5 (fast decompress)")
        print("    2026 Zarr: Zlib level=1       (much slower decompress)")
        print("  Recommendation: re-encode 2026 with Blosc to match.")

    ds_old.close()
    ds_new.close()
    return 0 if pass_speed else 1


if __name__ == "__main__":
    sys.exit(main())
