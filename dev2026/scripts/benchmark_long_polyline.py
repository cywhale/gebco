"""P-Bench-1 — long-polyline benchmark for v0.5.4 W1 / H9.

Drives ``src.zprofile.zprofile()`` on a synthetic polyline. Two styles
both must be measured for a fair H9 verdict because they exercise
different hot loops:

  * ``--style dense`` (n input vertices, small per-segment span)
    Every segment is sub-cell, so the inner nested loop is skipped and
    the H9 swap mostly affects the segment-level appends (which were
    cheap to begin with because numpy realloc on small arrays is
    amortised by the allocator). Expected H9 speedup: ~1.2–1.5×.

  * ``--style sparse`` (2 input vertices, wide span)
    The single segment triggers the inner nested cell-walk loop with
    ``arc × span_deg`` iterations, each of which used to do 3 calls to
    ``np.append`` in v0.5.3. This is the asymptotic O(n²) regime the
    plan §8 ≥3× target refers to. Expected H9 speedup: much larger.

Run both styles before and after the H9 refactor. The Phase F table in
TESTING.md records median wall + RSS delta for each style.

Usage:
    uv run python dev2026/scripts/benchmark_long_polyline.py
    uv run python dev2026/scripts/benchmark_long_polyline.py \
        --style sparse --span-deg 20 --warmup 2 --trials 5
    uv run python dev2026/scripts/benchmark_long_polyline.py \
        --style dense --n 5000
"""
from __future__ import annotations

import argparse
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
    try:
        import psutil
    except ImportError:
        return float("nan")
    return psutil.Process().memory_info().rss / (1024 * 1024)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    p.add_argument("--zarr", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    p.add_argument("--style", choices=["dense", "sparse"], default="sparse",
                   help="dense = n input vertices over a small span (segment-level "
                        "appends dominate); sparse = 2 vertices over a wide span "
                        "(inner cell-walk loop dominates — the asymptotic H9 regime)")
    p.add_argument("--n", type=int, default=5000,
                   help="dense-mode: input vertex count")
    p.add_argument("--span-deg", type=float, default=20.0,
                   help="sparse-mode: lon (and lat) span of the 2-vertex segment "
                        "(default 20° → ~4800 nested-loop iterations per segment)")
    p.add_argument("--warmup", type=int, default=2)
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--mode", type=str, default="row",
                   help="zprofile mode string (e.g. 'row', 'zonly', '')")
    args = p.parse_args()

    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    rng = np.random.default_rng(args.seed)
    # Start in the open Pacific so all input points are valid sea cells.
    lat0 = float(rng.uniform(-20, -10))
    lon0 = float(rng.uniform(-160, -130))
    if args.style == "dense":
        # straight-line in lon, gentle slope in lat — exercises the slope-<=1
        # path inside zprofile. Each segment is sub-cell, so the inner loop
        # is skipped and the H9 swap affects segment-level appends only.
        loni = np.linspace(lon0, lon0 + 5.0, args.n)
        lati = np.linspace(lat0, lat0 + 0.5, args.n)
        descriptor = f"dense n={args.n}"
    else:
        # 2-vertex diagonal spanning span_deg in both lon and lat. The
        # inner nested cell-walk loop runs (span_deg × arc) iterations.
        # This is the asymptotic O(n²) → O(n) regime the H9 refactor
        # was designed for; plan §8 ≥ 3× target refers to this style.
        loni = np.array([lon0, lon0 + args.span_deg])
        lati = np.array([lat0, lat0 + args.span_deg])
        descriptor = f"sparse span={args.span_deg}°"

    sys.path.insert(0, str(args.repo_root))
    import src.config as config
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90
    config.ds = _open_zarr(args.zarr)

    from src.zprofile import zprofile

    print(f"[bench] zarr={args.zarr}")
    print(f"[bench] polyline: {descriptor}  lat0={lat0:.3f}  lon0={lon0:.3f}  mode={args.mode!r}")
    print(f"[bench] warmup={args.warmup}  trials={args.trials}")

    # warmup
    for _ in range(args.warmup):
        zprofile(loni.copy(), lati.copy(), args.mode, 1)

    rss_before = _rss_mb()
    walls: list[float] = []
    for trial in range(args.trials):
        t0 = time.monotonic()
        zprofile(loni.copy(), lati.copy(), args.mode, 1)
        walls.append(time.monotonic() - t0)
    rss_after = _rss_mb()

    walls_arr = np.asarray(walls)
    median = float(np.median(walls_arr))
    p95 = float(np.percentile(walls_arr, 95))
    rss_delta = rss_after - rss_before
    print()
    print(f"  median: {median*1000:8.2f} ms")
    print(f"  P95:    {p95*1000:8.2f} ms")
    print(f"  min:    {walls_arr.min()*1000:8.2f} ms")
    print(f"  max:    {walls_arr.max()*1000:8.2f} ms")
    print(f"  rss Δ:  {rss_delta:8.1f} MB")
    print()
    print(f"CSV,style={args.style},n={args.n},span={args.span_deg},mode={args.mode},median_ms={median*1000:.2f},p95_ms={p95*1000:.2f},rss_delta_mb={rss_delta:.1f}")

    config.ds.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
