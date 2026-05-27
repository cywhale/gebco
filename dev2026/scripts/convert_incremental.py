"""Incremental NetCDF → Zarr converter.

Each invocation does as much work as it can within a soft time budget
(--budget-seconds, default 40). Detects already-written chunk-rows and skips
them, so you can call it in a loop:

    while ! .venv/bin/python scripts/convert_incremental.py \
        ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc \
        ../data/GEBCO_2026_sub_ice_topo.zarr ; do : ; done

Exit codes:
    0  fully done
    2  bad usage
    3  more work remaining (call again)

Designed for sandboxed environments where:
  * each shell invocation has a hard wall-clock limit
  * background processes get reaped between invocations
  * the filesystem disallows unlink / rmdir (virtiofs) — so we never delete,
    only create + write
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# Reuse the same virtiofs-tolerant shims as convert_to_zarr.py
_orig_unlink = os.unlink
_orig_rmdir = os.rmdir
_orig_rmtree = shutil.rmtree


def _safe_unlink(path, *, dir_fd=None):
    try:
        return _orig_unlink(path, dir_fd=dir_fd)
    except PermissionError:
        return


def _safe_rmdir(path, *, dir_fd=None):
    try:
        return _orig_rmdir(path, dir_fd=dir_fd)
    except (PermissionError, OSError):
        return


def _safe_rmtree(path, ignore_errors=False, onerror=None, onexc=None, dir_fd=None):
    try:
        return _orig_rmtree(
            path,
            ignore_errors=ignore_errors,
            onerror=onerror,
            onexc=onexc,
            dir_fd=dir_fd,
        )
    except (PermissionError, OSError):
        return


os.unlink = _safe_unlink
os.remove = _safe_unlink
os.rmdir = _safe_rmdir
shutil.rmtree = _safe_rmtree

import numpy as np  # noqa: E402
import numcodecs  # noqa: E402
import xarray as xr  # noqa: E402
import zarr  # noqa: E402


DEFAULT_CHUNKS = {"lat": 675, "lon": 2700}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("src_nc", type=Path)
    p.add_argument("dst_zarr", type=Path)
    p.add_argument("--chunk-lat", type=int, default=DEFAULT_CHUNKS["lat"])
    p.add_argument("--chunk-lon", type=int, default=DEFAULT_CHUNKS["lon"])
    p.add_argument("--codec", choices=["zlib", "blosc"], default="zlib",
                   help="compressor: 'zlib' (level=zlib-level) or 'blosc' (lz4 clevel=5, "
                        "matches the 2023 production Zarr for read-speed parity)")
    p.add_argument("--zlib-level", type=int, default=1)
    p.add_argument("--budget-seconds", type=float, default=40.0)
    args = p.parse_args()

    if not args.src_nc.exists():
        print(f"src not found: {args.src_nc}", file=sys.stderr)
        return 2

    args.dst_zarr.mkdir(parents=True, exist_ok=True)

    # Open source lazily
    src = xr.open_dataset(
        str(args.src_nc),
        decode_cf=False,
        decode_times=False,
        chunks={"lat": args.chunk_lat, "lon": args.chunk_lon},
        engine="netcdf4",
    )
    n_lat = int(src.sizes["lat"])
    n_lon = int(src.sizes["lon"])
    cl = args.chunk_lat
    co = args.chunk_lon
    n_chunk_rows = (n_lat + cl - 1) // cl
    n_chunk_cols = (n_lon + co - 1) // co

    elev_path = args.dst_zarr / "elevation"
    already_present = (args.dst_zarr / ".zgroup").exists() and elev_path.exists()

    if not already_present:
        # First-time init: write metadata + coords + crs scalar via xarray,
        # but compute=False so no chunks are materialized. This gives us a
        # template Zarr we can then fill chunk-by-chunk.
        print(f"[init] creating template at {args.dst_zarr}")
        if args.codec == "blosc":
            # Match the 2023 production Zarr: blosc/lz4, clevel=5, shuffle=1
            compressor = numcodecs.Blosc(cname="lz4", clevel=5, shuffle=1)
            print(f"[init] codec = Blosc(cname='lz4', clevel=5, shuffle=1)")
        else:
            compressor = numcodecs.Zlib(level=args.zlib_level)
            print(f"[init] codec = Zlib(level={args.zlib_level})")
        encoding = {var: {"compressor": compressor} for var in src.data_vars}
        # IMPORTANT for zarr v2 layout
        try:
            src.to_zarr(
                str(args.dst_zarr),
                mode="w",
                compute=False,
                consolidated=False,  # consolidate at the very end
                encoding=encoding,
                zarr_format=2,
            )
        except TypeError:
            src.to_zarr(
                str(args.dst_zarr),
                mode="w",
                compute=False,
                consolidated=False,
                encoding=encoding,
            )
        print("[init] template written")

    # Open the on-disk Zarr for chunk-by-chunk writes
    z_group = zarr.open_group(str(args.dst_zarr), mode="r+", zarr_format=2)
    z_elev = z_group["elevation"]

    # Determine which chunk-rows are already populated. A chunk-row is "done"
    # if all (n_chunk_cols) chunk files exist for that row. Use raw filesystem
    # check (fast, no zarr overhead).
    done_rows = []
    todo_rows = []
    for r in range(n_chunk_rows):
        all_present = all(
            (elev_path / f"{r}.{c}").exists() for c in range(n_chunk_cols)
        )
        if all_present:
            done_rows.append(r)
        else:
            todo_rows.append(r)

    print(
        f"[status] chunk-rows: total={n_chunk_rows} done={len(done_rows)} "
        f"todo={len(todo_rows)}"
    )
    if not todo_rows:
        # Finalize: write consolidated metadata
        print("[final] writing consolidated metadata")
        zarr.consolidate_metadata(str(args.dst_zarr))
        # Also stamp lat/lon/crs in case they were skipped (compute=False)
        for var in src.data_vars:
            if var == "elevation":
                continue  # already handled chunk-wise
            print(f"[final] materialising scalar/coord-shaped var: {var}")
            z_var = z_group[var]
            arr = src[var].values
            z_var[...] = arr
        for coord in src.coords:
            z_var = z_group[coord]
            z_var[...] = src[coord].values
        zarr.consolidate_metadata(str(args.dst_zarr))
        print("[done] conversion complete")
        return 0

    # Plough through todo_rows until budget exhausted
    t_start = time.time()
    rows_done_this_call = 0
    elev_src = src["elevation"]
    for r in todo_rows:
        if time.time() - t_start > args.budget_seconds:
            break
        lat_start = r * cl
        lat_end = min(lat_start + cl, n_lat)
        # Read full lon span for this lat slice (one chunk-row).
        # Pull as a single contiguous numpy block — sources have no on-disk
        # chunking, so a single contiguous read is most efficient.
        t0 = time.time()
        block = elev_src.isel(lat=slice(lat_start, lat_end)).values
        # Write to zarr in one shot
        z_elev[lat_start:lat_end, :] = block
        dt = time.time() - t0
        rows_done_this_call += 1
        print(
            f"[row {r:>3d}/{n_chunk_rows}] lat[{lat_start}:{lat_end}] "
            f"shape={block.shape} dt={dt:.2f}s",
            flush=True,
        )

    remaining = [r for r in todo_rows if not all(
        (elev_path / f"{r}.{c}").exists() for c in range(n_chunk_cols)
    )]
    print(
        f"[wallclock] this call: {time.time()-t_start:.1f}s  "
        f"rows_written_this_call={rows_done_this_call}  "
        f"rows_remaining={len(remaining)}"
    )
    return 3 if remaining else 0


if __name__ == "__main__":
    sys.exit(main())
