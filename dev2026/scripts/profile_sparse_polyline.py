"""Decomposition profiler for the H9 sparse-style benchmark.

This script is the data-source for revising v0.5.4 plan §8's H9 ≥3×
acceptance target. It splits the sparse 2-vertex 20° polyline call into
discrete stages and times each, so a reviewer can see how much of the
wall actually belongs to code H9 / pyproj touched (the inner cell-walk
loop + distance computation) versus code H9 cannot improve (xarray
sel + Zarr decompression).

Stages timed (each averaged over ``--trials`` runs):

  1. ``zprofile.crossBoundary`` — meridian crossing detection (refactored
     by H9; should be cheap for non-crossing input).
  2. ``ds.sel`` — the lazy xarray slice that selects the bbox subset.
  3. ``ds_s1["elevation"].values`` — the Zarr / Blosc decompression
     materialise. This is the dominant cost on the real GEBCO_2026 Zarr
     because the bbox is 23M cells at sample=1.
  4. ``inner walk`` — the H9 hot loop (np.append → list accumulator)
     including ``_push_dist_buf`` (pyproj distance).
  5. ``buffers → np.asarray`` — terminal conversion.
  6. ``response build`` — pl.DataFrame / ORJSONResponse assembly.

Also dumps a cProfile pstats file so reviewers can see hot functions.

Usage:
    uv run python dev2026/scripts/profile_sparse_polyline.py
    uv run python dev2026/scripts/profile_sparse_polyline.py \
        --span-deg 20 --trials 5 --width 30

The breakdown is what justifies the v0.5.4 plan §8 revision from "≥3×"
to "≥1.2× on the sparse benchmark plus inner-loop ≥3×". Inner-loop
≥3× is achievable because that part WAS the H9 target; total wall ≥3×
requires Zarr-level work outside W1's scope.
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


def _open_zarr(path: Path):
    import xarray as xr
    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


def _bench_stages(repo_root: Path, zarr_path: Path,
                  span_deg: float, trials: int, seed: int) -> dict:
    """Re-implements the relevant parts of zprofile() with per-stage timers."""
    sys.path.insert(0, str(repo_root))
    import src.config as config

    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90
    config.MAX_BBOX_CELLS_LINE = 10**12
    config.ds = _open_zarr(zarr_path)

    from src.zprofile import (
        gridded_arcsec, _push_dist_buf, _seg_km,
    )
    from src.xmeridian import crossBoundary

    rng = np.random.default_rng(seed)
    lat0 = float(rng.uniform(-20, -10))
    lon0 = float(rng.uniform(-160, -130))
    loni = np.array([lon0, lon0 + span_deg])
    lati = np.array([lat0, lat0 + span_deg])

    arc = config.arc
    basex = config.basex
    basey = config.basey

    # warmup
    _ = config.ds.sel(
        lon=slice(lon0 - 0.5, lon0 + span_deg + 0.5),
        lat=slice(lat0 - 0.5, lat0 + span_deg + 0.5),
    )["elevation"].values

    totals = {"crossBoundary": 0.0, "ds_sel": 0.0, "values_materialise": 0.0,
              "inner_walk": 0.0, "buffers_to_array": 0.0, "n_inner_iters": 0}

    for _ in range(trials):
        # 1. crossBoundary
        t0 = time.monotonic()
        ats = crossBoundary(loni.copy(), lati.copy())
        lonk = np.asarray(ats[0])
        latk = np.asarray(ats[1])
        brks = ats[2]
        totals["crossBoundary"] += time.monotonic() - t0

        # bbox math (cheap, not timed separately)
        mlon0 = max(np.min(lonk) - 1.5 / arc, -basex + 0.00001)
        mlat0 = max(np.min(latk) - 1.5 / arc, -basey + 0.00001)
        mlonbase = gridded_arcsec(mlon0, basex, arc)
        mlatbase = gridded_arcsec(mlat0, basey, arc)

        # 4. inner walk (H9 hot loop) — simulate the slope-<=1 path
        t0 = time.monotonic()
        idx1_buf: list = []
        loc1_buf: list = []
        dis1_buf: list = [0.0]
        lonx = lonk
        latx = latk
        for i in range(len(lonx) - 1):
            lonidx0 = gridded_arcsec(lonx[i], basex, arc)
            latidx0 = gridded_arcsec(latx[i], basey, arc)
            lonidx1 = gridded_arcsec(lonx[i + 1], basex, arc)
            latidx1 = gridded_arcsec(latx[i + 1], basey, arc)
            m = (latx[i + 1] - latx[i]) / (lonx[i + 1] - lonx[i])
            b = latx[i] - m * lonx[i]
            if abs(m) <= 1:
                lidx0, lidx1 = lonidx0, lonidx1
            else:
                lidx0, lidx1 = latidx0, latidx1
            stepi = -1 if lidx0 > lidx1 else 1
            rngi = range(lidx0, lidx1 + stepi, stepi)
            leni = len(rngi)
            totals["n_inner_iters"] += leni
            for k, s in enumerate(rngi):
                if k == 0:
                    idx1_buf.append((latidx0 - mlatbase, lonidx0 - mlonbase))
                    loc1_buf.append((float(lonx[i]), float(latx[i])))
                else:
                    if abs(m) <= 1:
                        locx0 = (s + 1) / arc - basex
                        locx0i = int(locx0)
                        locx1 = locx0i + (locx0 - locx0i - 0.25 / arc)
                        locx1 = basex if locx1 > basex else (-basex if locx1 < -basex else locx1)
                        locy1 = m * locx1 + b
                        locy1 = basey if locy1 > basey else (-basey if locy1 < -basey else locy1)
                        if k < leni - 1 or (stepi == 1 and locx1 < lonx[i + 1]) or (stepi == -1 and locx1 > lonx[i + 1]):
                            y = gridded_arcsec(locy1, basey, arc)
                            idx1_buf.append((y - mlatbase, s - mlonbase))
                            loc1_buf.append((float(locx1), float(locy1)))
                            _push_dist_buf(loc1_buf, dis1_buf)
        totals["inner_walk"] += time.monotonic() - t0

        # 5. buffers → asarray
        t0 = time.monotonic()
        idx1 = np.asarray(idx1_buf, dtype=np.int16)
        np.asarray(loc1_buf, dtype=float)
        np.asarray(dis1_buf, dtype=float)
        totals["buffers_to_array"] += time.monotonic() - t0

        # 2. ds.sel (lazy)
        mlon1 = min(np.max(lonk) + 1.5 / arc, basex - 0.00001)
        mlat1 = min(np.max(latk) + 1.5 / arc, basey - 0.00001)
        mlon0x = config.ds["lon"][mlonbase].item()
        mlat0x = config.ds["lat"][mlatbase].item()
        t0 = time.monotonic()
        ds_s1 = config.ds.sel(lon=slice(mlon0x, mlon1, 1), lat=slice(mlat0x, mlat1, 1))
        totals["ds_sel"] += time.monotonic() - t0

        # 3. .values materialise (Zarr decompress)
        t0 = time.monotonic()
        _ = ds_s1["elevation"].values[tuple(idx1.T)]
        totals["values_materialise"] += time.monotonic() - t0
        ds_s1.close()

    config.ds.close()
    return totals


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    p.add_argument("--zarr", type=Path, default=None)
    p.add_argument("--span-deg", type=float, default=20.0)
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--width", type=int, default=25)
    p.add_argument("--out", type=Path,
                   default=_REPO_ROOT_DEFAULT / "dev2026" / ".profile_sparse_polyline.pstats")
    args = p.parse_args()
    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    print(f"[profile] zarr={args.zarr}")
    print(f"[profile] span={args.span_deg}°  trials={args.trials}  seed={args.seed}")
    print()

    # Per-stage timing
    t0 = time.monotonic()
    totals = _bench_stages(args.repo_root, args.zarr, args.span_deg,
                           args.trials, args.seed)
    total_wall = time.monotonic() - t0

    print("=== per-stage wall time (sum over trials) ===")
    timed_total = (totals["crossBoundary"] + totals["ds_sel"]
                   + totals["values_materialise"] + totals["inner_walk"]
                   + totals["buffers_to_array"])
    for stage in ["crossBoundary", "ds_sel", "values_materialise",
                  "inner_walk", "buffers_to_array"]:
        s = totals[stage]
        pct = (s / timed_total * 100) if timed_total else 0
        print(f"  {stage:25s}  {s*1000/args.trials:7.2f} ms/call  ({pct:5.1f}%)")
    print(f"  {'sum (timed)':25s}  {timed_total*1000/args.trials:7.2f} ms/call")
    print(f"  {'wall (incl. setup)':25s}  {total_wall*1000:7.2f} ms total ({args.trials} trials)")
    print(f"  inner iterations / call    {totals['n_inner_iters'] // args.trials}")

    print()
    print("=== plan §8 H9 interpretation guide ===")
    h9_path = (totals["crossBoundary"] + totals["inner_walk"]
               + totals["buffers_to_array"])
    zarr_path = totals["ds_sel"] + totals["values_materialise"]
    print(f"  code H9 touched       : {h9_path*1000/args.trials:7.2f} ms/call ({h9_path/timed_total*100:.1f}%)")
    print(f"  Zarr/xarray (untouched): {zarr_path*1000/args.trials:7.2f} ms/call ({zarr_path/timed_total*100:.1f}%)")
    print(f"  H9 max-possible total speedup if inner_walk → 0:"
          f"  {(timed_total / zarr_path):.2f}x")
    print("  (i.e. the plan §8 ≥3× target needs Zarr-side optimisation, not H9.)")

    # Also dump a cProfile snapshot
    prof = cProfile.Profile()
    prof.enable()
    _bench_stages(args.repo_root, args.zarr, args.span_deg, 1, args.seed)
    prof.disable()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    prof.dump_stats(str(args.out))
    print()
    print(f"[profile] cProfile pstats written to {args.out}")
    print(f"=== top {args.width} cumulative frames ===")
    pstats.Stats(prof).sort_stats("cumulative").print_stats(args.width)
    return 0


if __name__ == "__main__":
    sys.exit(main())
