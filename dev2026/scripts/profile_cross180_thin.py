"""W2 pre-work — cProfile snapshot of the Phase D2 cross180_thin polygon.

Why this script exists (v0.5.4 plan §5 "Pre-work profiling requirement"):

  Phase D2 measured the cross-180 thin polygon at ~`4.65e7` raw bbox
  cells = **112 s wall / 8.3 GB RSS** while estimated returned rows were
  only ~`132k`. Before W2-B (the row-batch redesign) lands in PR2, this
  script records WHERE that 112 s actually goes — `shapely.points()`,
  `shapely.contains()`, `meshgrid`, dask materialisation, or DataFrame
  building. Without that decomposition, the row-batch redesign might
  improve mainly RSS and very little wall-time (e.g. if contains is the
  dominant cost), and the v0.5.4 acceptance threshold for wall time
  should be revisited.

Output:
  * Top 20 hottest frames by cumulative time, printed to stdout.
  * A serialised pstats file under `--out` (default
    `dev2026/.profile_cross180_thin.pstats`) for offline inspection.

Usage:
    uv run python dev2026/scripts/profile_cross180_thin.py
    uv run python dev2026/scripts/profile_cross180_thin.py \
        --out /tmp/p.pstats --width 60

Run-on requirements:
  * canonical 2026 Zarr present at the path resolved from `--repo-root`
  * dev2026 `uv sync` env (polars + shapely + xarray installed)

This is one-shot diagnostic tooling, not a regression test. Do not run
it inside the sandbox (8 GB+ RSS will OOM).
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time
import warnings
from pathlib import Path

import numpy as np

warnings.simplefilter("ignore")

_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]


# D2 cross180_thin archetype, kept here so the spike is reproducible
# even after the original Phase D2 probe script is retired or moved.
# Shape: 1°× ~0.05° thin rectangle straddling the 180° meridian. The
# precise vertices are tuned so the raw bbox sits at ~`4.65e7` cells.
CROSS180_THIN_POLY = {
    "type": "Polygon",
    "coordinates": [[
        [179.5, -0.025],
        [179.5,  0.025],
        [-179.5,  0.025],
        [-179.5, -0.025],
        [179.5, -0.025],
    ]],
}


def _open_zarr(path: Path):
    import xarray as xr
    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


def _setup(repo_root: Path, zarr_path: Path):
    """Prepare the production polygon path, excluding import/open costs from profiling."""
    sys.path.insert(0, str(repo_root))
    import src.config as config
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90
    config.ds = _open_zarr(zarr_path)

    # Set a very generous polygon cap so the H6 guard does not pre-empt
    # the profile.
    config.MAX_POLYGON_CELLS = 10**12

    from src.polyhandler import polyhandler
    return config, polyhandler


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    p.add_argument("--zarr", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    p.add_argument("--out", type=Path,
                   default=_REPO_ROOT_DEFAULT / "dev2026" / ".profile_cross180_thin.pstats")
    p.add_argument("--width", type=int, default=20,
                   help="top-N frames to print (default 20)")
    args = p.parse_args()

    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    print(f"[profile] zarr={args.zarr}")
    print(f"[profile] geometry: cross180 thin polygon (~4.65e7 raw bbox cells)")
    print(f"[profile] sample=5  (matches public endpoint default)")

    config, polyhandler = _setup(args.repo_root, args.zarr)
    # Warm one call so import / one-time GEOS setup do not dominate the snapshot.
    polyhandler(CROSS180_THIN_POLY, 0, "", 1, 5)

    t0 = time.monotonic()
    profiler = cProfile.Profile()
    profiler.enable()
    polyhandler(CROSS180_THIN_POLY, 0, "", 1, 5)
    profiler.disable()
    elapsed = time.monotonic() - t0
    print(f"[profile] total wall: {elapsed:.2f} s")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    profiler.dump_stats(str(args.out))
    print(f"[profile] pstats written to {args.out}")

    print()
    print(f"=== top {args.width} cumulative-time frames ===")
    stats = pstats.Stats(profiler).sort_stats("cumulative")
    stats.print_stats(args.width)
    config.ds.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
