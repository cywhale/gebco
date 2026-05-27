"""Strict NetCDF↔Zarr equality verification — token-aware.

Reads the same (lat_idx, lon_idx) coordinates from BOTH the source NetCDF and
the converted Zarr (using identical decode flags: decode_cf=False,
decode_times=False) and asserts byte-for-byte equality of `elevation`.

Why this design:
  - The source `elevation` is int16; the Zarr we produce stores raw int16; with
    decode_cf=False, no scaling happens on read. Therefore the only honest
    tolerance is **exact equality** (default tolerance=0).
  - With K random points spread across a 43200x86400 grid, naive sampling will
    touch nearly every Zarr chunk. To keep I/O cheap we sample in small
    *windows* (W x W contiguous pixels at random origins); the user explicitly
    allowed continuous-or-discontinuous sampling. Set --window 1 for pure
    point-wise random.
  - Token budget: 1 line per passing round; only on failure do we print up to
    --max-mismatch-print offending points. With 10 rounds × 10000 points the
    success output is ~15 lines.

Usage:
    uv run python scripts/verify_against_netcdf.py \
        ../data/GEBCO_2026_sub_ice_topo.zarr \
        ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc
        # optional: --rounds 10 --points-per-round 10000 --window 10
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import xarray as xr


def _open_zarr(path: Path) -> xr.Dataset:
    return xr.open_zarr(
        str(path), chunks="auto", decode_cf=False, decode_times=False
    )


def _open_netcdf(path: Path) -> xr.Dataset:
    """Open NetCDF with explicit chunks via netcdf4 engine.

    Two important details for the verification workload:
      * `engine="netcdf4"` — has OUTER_1VECTOR indexing support, so xarray can
        translate isel(lat=DataArray, lon=DataArray) to per-chunk reads
        without materialising the whole array. (h5netcdf engine OOMs here.)
      * Explicit `chunks={"lat":675, "lon":2700}` — matches the Zarr chunking
        so reads are well-aligned. Without explicit chunks, xarray sees the
        source NetCDF as a single contiguous dataset and tries to load all
        6.95 GiB at once.

    When BOTH index DataArrays share the same single dim ("p" in our case),
    xarray's advanced indexing is POINTWISE — i.e. the result is shape (K,)
    not (K, K). That's the equivalence semantics we need.
    """
    return xr.open_dataset(
        str(path),
        engine="netcdf4",
        chunks={"lat": 675, "lon": 2700},
        decode_cf=False,
        decode_times=False,
    )


def _generate_indices(
    rng: np.random.Generator,
    n_lat: int,
    n_lon: int,
    points: int,
    window: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate (lat_idx, lon_idx) arrays of length `points`.

    With window=W>1, samples ceil(points/W^2) random window origins and
    enumerates each W×W tile of pixels — trims to exactly `points` at the end.
    Output indices are always 1-D, length == `points`.
    """
    if window <= 1:
        return (
            rng.integers(0, n_lat, size=points, dtype=np.int64),
            rng.integers(0, n_lon, size=points, dtype=np.int64),
        )

    per_tile = window * window
    n_tiles = (points + per_tile - 1) // per_tile
    # Origins must keep the window fully inside the grid
    lat0 = rng.integers(0, n_lat - window + 1, size=n_tiles, dtype=np.int64)
    lon0 = rng.integers(0, n_lon - window + 1, size=n_tiles, dtype=np.int64)

    di = np.arange(window, dtype=np.int64)
    dj = np.arange(window, dtype=np.int64)
    # Build the full (n_tiles, W, W) index grid for each axis. The previous
    # implementation only broadcast to (n_tiles, W, 1) / (n_tiles, 1, W) and
    # the reshape collapsed to n_tiles*W (not n_tiles*W*W), so K was 100x off.
    lat_grid = (lat0[:, None, None] + di[None, :, None]).astype(np.int64)
    lon_grid = (lon0[:, None, None] + dj[None, None, :]).astype(np.int64)
    target_shape = (n_tiles, window, window)
    lat_idx = np.broadcast_to(lat_grid, target_shape).reshape(-1).copy()
    lon_idx = np.broadcast_to(lon_grid, target_shape).reshape(-1).copy()
    return lat_idx[:points], lon_idx[:points]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("zarr_path", type=Path, help="Path to the produced Zarr store")
    p.add_argument("nc_path", type=Path, help="Path to the source NetCDF file")
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument(
        "--points-per-round",
        type=int,
        default=10_000,
        help="Sample points per round (default 10000, total = rounds × this).",
    )
    p.add_argument(
        "--window",
        type=int,
        default=10,
        help=(
            "Sample contiguous W×W tiles to keep chunk I/O bounded "
            "(default 10 → ~100 tiles/round). Set 1 for pure point-wise random."
        ),
    )
    p.add_argument(
        "--seed",
        type=int,
        default=2026,
        help="Master seed; per-round seed = derived from this.",
    )
    p.add_argument(
        "--tolerance",
        type=float,
        default=0.0,
        help="Absolute tolerance on |Δ|. Default 0 (require exact equality).",
    )
    p.add_argument(
        "--max-mismatch-print",
        type=int,
        default=20,
        help="When a round fails, print at most this many offending points.",
    )
    p.add_argument("--var", default="elevation", help="Variable name to compare.")
    args = p.parse_args()

    if args.window < 1:
        print("--window must be >= 1", file=sys.stderr)
        return 2
    for fp in (args.zarr_path, args.nc_path):
        if not fp.exists():
            print(f"not found: {fp}", file=sys.stderr)
            return 2

    t_open = time.time()
    ds_z = _open_zarr(args.zarr_path)
    ds_n = _open_netcdf(args.nc_path)
    print(f"opened both in {time.time() - t_open:.2f}s")

    # --- structural pre-checks (cheap) -------------------------------------
    if args.var not in ds_z.data_vars or args.var not in ds_n.data_vars:
        print(
            f"variable {args.var!r} missing — zarr.vars={list(ds_z.data_vars)} "
            f"nc.vars={list(ds_n.data_vars)}",
            file=sys.stderr,
        )
        return 2

    z_shape = ds_z[args.var].shape
    n_shape = ds_n[args.var].shape
    if z_shape != n_shape:
        print(
            f"shape mismatch: zarr={z_shape} nc={n_shape} — aborting before sampling",
            file=sys.stderr,
        )
        return 3
    if ds_z[args.var].dtype != ds_n[args.var].dtype:
        print(
            f"dtype mismatch: zarr={ds_z[args.var].dtype} nc={ds_n[args.var].dtype} "
            "(continuing — the diff will catch any actual value drift)"
        )
    # Coord alignment check on a few sample positions (cheap)
    for c in ("lat", "lon"):
        if c in ds_z.coords and c in ds_n.coords:
            zv = ds_z[c].values
            nv = ds_n[c].values
            if zv.shape != nv.shape:
                print(
                    f"coord {c!r} shape mismatch: zarr={zv.shape} nc={nv.shape}",
                    file=sys.stderr,
                )
                return 3
            # spot-check first/middle/last
            for i in (0, zv.size // 2, zv.size - 1):
                if not np.isclose(zv[i], nv[i], atol=1e-9):
                    print(
                        f"coord {c!r} value drift at idx {i}: zarr={zv[i]} nc={nv[i]}",
                        file=sys.stderr,
                    )
                    return 3

    n_lat, n_lon = z_shape  # dims order should be (lat, lon)

    # --- sampling rounds ---------------------------------------------------
    master = np.random.default_rng(args.seed)
    round_seeds = master.integers(0, 2**31 - 1, size=args.rounds, dtype=np.int64)

    total_pts = 0
    total_bad = 0
    overall_max_diff = 0.0
    failing_rounds: list[int] = []

    for r, s in enumerate(round_seeds, start=1):
        rng = np.random.default_rng(int(s))
        lat_idx, lon_idx = _generate_indices(
            rng, n_lat, n_lon, args.points_per_round, args.window
        )
        K = lat_idx.size
        # xarray advanced indexing → vectorised point read, returns shape (K,)
        lat_da = xr.DataArray(lat_idx, dims="p")
        lon_da = xr.DataArray(lon_idx, dims="p")

        t0 = time.time()
        zv = ds_z[args.var].isel(lat=lat_da, lon=lon_da).values
        t_zarr = time.time() - t0

        t0 = time.time()
        nv = ds_n[args.var].isel(lat=lat_da, lon=lon_da).values
        t_nc = time.time() - t0

        diff = zv.astype(np.float64) - nv.astype(np.float64)
        abs_diff = np.abs(diff)
        max_d = float(abs_diff.max()) if abs_diff.size else 0.0
        bad_mask = abs_diff > args.tolerance
        n_bad = int(bad_mask.sum())

        total_pts += K
        total_bad += n_bad
        if max_d > overall_max_diff:
            overall_max_diff = max_d

        if n_bad == 0:
            print(
                f"[{r:>2d}/{args.rounds}] seed={int(s):>10d} K={K:>6d} "
                f"OK  max|Δ|={max_d:g}  read_t(zarr/nc)={t_zarr:.2f}/{t_nc:.2f}s"
            )
            continue

        # --- failure path ---------------------------------------------------
        failing_rounds.append(r)
        print(
            f"[{r:>2d}/{args.rounds}] seed={int(s):>10d} K={K:>6d} "
            f"FAIL n_bad={n_bad}/{K} max|Δ|={max_d:g} "
            f"read_t(zarr/nc)={t_zarr:.2f}/{t_nc:.2f}s"
        )
        bad_positions = np.where(bad_mask)[0][: args.max_mismatch_print]
        # Pull lat/lon values for the offending indices (cheap, just coord lookup)
        lat_vals = ds_z["lat"].values[lat_idx[bad_positions]]
        lon_vals = ds_z["lon"].values[lon_idx[bad_positions]]
        for k, b in enumerate(bad_positions):
            print(
                f"  #{k:02d} [lat_i={int(lat_idx[b]):>5d} lon_i={int(lon_idx[b]):>5d}] "
                f"lat={lat_vals[k]:.6f} lon={lon_vals[k]:.6f} "
                f"zarr={zv[b]} nc={nv[b]} Δ={int(zv[b]) - int(nv[b]):+d}"
            )

    ds_z.close()
    ds_n.close()

    # --- final summary -----------------------------------------------------
    print()
    print(
        f"TOTAL  points={total_pts:,}  rounds={args.rounds}  "
        f"window={args.window}  tolerance={args.tolerance:g}"
    )
    if total_bad == 0:
        print(f"✓ ALL MATCH  (overall max|Δ| = {overall_max_diff:g})")
        return 0
    print(
        f"✗ MISMATCH  total_bad={total_bad:,}/{total_pts:,}  "
        f"max|Δ|={overall_max_diff:g}  failing rounds: {failing_rounds}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
