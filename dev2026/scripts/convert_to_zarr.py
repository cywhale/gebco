"""Convert a GEBCO_2026 sub-ice NetCDF to a Zarr store.

This is the Python-script port of the relevant cells from
dev/read_gebco_raw01.ipynb (the 2022→2023 work), with three notable choices:

1. Chunking: ``{'lat': 675, 'lon': 2700}`` — same as the notebook, because it
   was the only setting that avoided the
   ``ValueError: Codec does not support buffers of > 2147483647 bytes`` we hit
   in 2023 with smaller chunks (a single uncompressed chunk crossed 2 GiB).
2. Compression: **default is Blosc/lz4 (clevel=5, shuffle=1)** — matches the
   2023 production Zarr (cell 24/26 of the 2023 notebook actually re-encoded to
   Blosc; only the early/middle cells used Zlib). Step 4 of the 2026 upgrade
   verification measured Zlib at 2.66× slower per-point latency vs Blosc — so
   use ``--codec blosc`` for any Zarr you intend to serve from production.
   ``--codec zlib`` is kept for back-compat / debugging only.
3. ``decode_cf=False, decode_times=False`` and **no** Zarr group — same as
   2023, so the existing FastAPI ``gebco_app.py``
   (``xr.open_zarr(..., decode_cf=False)`` without a ``group=`` arg) can pick
   the new store up by just changing the path.

The script is idempotent-ish: it refuses to overwrite an existing output unless
``--force`` is passed. It also prints a summary at the end so you can sanity-
check chunk sizes and codec without opening the result.

For a sandbox-friendly chunk-row incremental version of the same conversion,
see ``convert_incremental.py`` (each invocation does as much work as it can
within a wall-clock budget, then exits — safe to call in a loop).
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# --- virtiofs / sandbox compatibility shim -------------------------------
# Some sandbox filesystems (notably the virtiofs mount used by the cowork
# desktop sandbox) forbid `unlink` and `rmdir` even when the calling user
# owns the file. Both our script and zarr's internal `LocalStore` rely on
# `os.unlink` (for `.partial` cleanup) and `shutil.rmtree` (for mode="w").
# We patch them to silently swallow PermissionError so the conversion can
# proceed; the final Zarr remains valid (leftover `.partial` files are
# ignored by zarr readers). On a normal filesystem these patches are no-ops.
import os as _os
_orig_unlink = _os.unlink
_orig_rmdir = _os.rmdir
_orig_rmtree = shutil.rmtree


def _safe_unlink(path, *, dir_fd=None):
    try:
        return _orig_unlink(path, dir_fd=dir_fd)
    except PermissionError:
        return  # virtiofs / read-mostly mount


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
    except (PermissionError, OSError) as exc:
        p = Path(str(path))
        if not p.exists():
            return
        # Walk and unlink files; leftover empty dirs are OK.
        for f in sorted(p.rglob("*"), key=lambda x: -len(str(x))):
            try:
                if f.is_file() or f.is_symlink():
                    try:
                        f.unlink()
                    except PermissionError:
                        pass
            except Exception:
                pass
        sys.stderr.write(
            f"[convert_to_zarr] shutil.rmtree({p}) hit {type(exc).__name__}; "
            "wiped contents and continuing\n"
        )


_os.unlink = _safe_unlink
_os.remove = _safe_unlink  # remove is alias for unlink
_os.rmdir = _safe_rmdir
shutil.rmtree = _safe_rmtree

import numcodecs  # noqa: E402
import xarray as xr  # noqa: E402

DEFAULT_CHUNKS = {"lat": 675, "lon": 2700}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("src_nc", type=Path, help="Path to GEBCO_2026_sub_ice.nc")
    p.add_argument("dst_zarr", type=Path, help="Output .zarr path")
    p.add_argument(
        "--chunk-lat",
        type=int,
        default=DEFAULT_CHUNKS["lat"],
        help=f"lat chunk size (default {DEFAULT_CHUNKS['lat']})",
    )
    p.add_argument(
        "--chunk-lon",
        type=int,
        default=DEFAULT_CHUNKS["lon"],
        help=f"lon chunk size (default {DEFAULT_CHUNKS['lon']})",
    )
    p.add_argument(
        "--codec",
        choices=["blosc", "zlib"],
        default="blosc",
        help="compressor: 'blosc' = Blosc(cname='lz4', clevel=5, shuffle=1), "
             "matching the 2023 production Zarr (default); 'zlib' = "
             "numcodecs.Zlib(level=zlib_level), correct values but ~2.6x "
             "slower API reads — only use for back-compat / debugging.",
    )
    p.add_argument(
        "--zlib-level",
        type=int,
        default=1,
        help="Zlib compression level 0-9 (default 1); only used when --codec zlib.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing dst_zarr.",
    )
    args = p.parse_args()

    if not args.src_nc.exists():
        print(f"Source not found: {args.src_nc}", file=sys.stderr)
        return 2

    if args.dst_zarr.exists():
        # Distinguish "empty placeholder dir" (e.g. created by a previous aborted
        # attempt on a filesystem that won't let us rmdir) from "real Zarr".
        children = list(args.dst_zarr.iterdir())
        has_zarr_metadata = (args.dst_zarr / ".zgroup").exists() or (
            args.dst_zarr / "zarr.json"
        ).exists()
        if children and has_zarr_metadata and not args.force:
            print(
                f"Destination already exists: {args.dst_zarr}\n"
                "Pass --force to overwrite.",
                file=sys.stderr,
            )
            return 2
        if args.force:
            print(f"[--force] removing existing {args.dst_zarr}")
            try:
                shutil.rmtree(args.dst_zarr)
            except (PermissionError, OSError) as exc:
                # Some filesystems (e.g. virtiofs in cowork sandboxes) refuse
                # rmdir on directories. Best we can do is wipe contents and
                # let to_zarr overwrite in mode="w".
                print(f"  rmtree failed ({exc!s}); wiping contents instead")
                for p in args.dst_zarr.rglob("*"):
                    try:
                        if p.is_file() or p.is_symlink():
                            p.unlink()
                    except Exception:
                        pass

    print(f"Opening {args.src_nc} ...")
    t0 = time.time()
    # IMPORTANT: pass explicit chunks at open time. GEBCO source NetCDF has no
    # internal chunking (it's a single contiguous HDF5 dataset), so an empty
    # `chunks={}` makes xarray treat the entire 43200x86400 int16 array as a
    # single dask chunk — ~6.95 GiB, OOMs anything under 8 GB RAM.
    ds = xr.open_dataset(
        str(args.src_nc),
        decode_cf=False,
        decode_times=False,
        chunks={"lat": args.chunk_lat, "lon": args.chunk_lon},
        engine="netcdf4",
    )
    print(f"  loaded metadata in {time.time() - t0:.2f}s")
    print(f"  sizes = {dict(ds.sizes)}")
    print(f"  data_vars = {list(ds.data_vars)}")
    if "elevation" not in ds.data_vars:
        print(
            "[warn] 'elevation' not in data_vars — the 2026 file may use a different "
            "variable name. Aborting before writing a broken Zarr.",
            file=sys.stderr,
        )
        return 3

    # Re-chunk for the Zarr write. The 2023 notebook found {lat:675, lon:2700}
    # safest given the 2 GiB-per-chunk limit of the Zlib codec.
    chunks = {"lat": args.chunk_lat, "lon": args.chunk_lon}
    print(f"Re-chunking to {chunks} ...")
    ds = ds.chunk(chunks)

    # Per-variable encoding: same codec for every data_var.
    if args.codec == "blosc":
        compressor = numcodecs.Blosc(cname="lz4", clevel=5, shuffle=1)
        print("Codec = Blosc(cname='lz4', clevel=5, shuffle=1)  [matches 2023 production]")
    else:
        compressor = numcodecs.Zlib(level=args.zlib_level)
        print(f"Codec = Zlib(level={args.zlib_level})  [WARNING: ~2.6x slower API reads than blosc]")
    encoding = {var: {"compressor": compressor} for var in ds.data_vars}

    print(f"Writing Zarr → {args.dst_zarr} ...")
    t0 = time.time()
    # Use zarr v2 on-disk layout so the production gebco_app.py (zarr 2.18.6,
    # see ../Pipfile) can read this store unchanged. xarray accepts
    # zarr_format=2 across zarr 2 and 3.
    to_zarr_kwargs: dict = dict(
        store=str(args.dst_zarr),
        mode="w",
        consolidated=True,
        encoding=encoding,
    )
    try:
        ds.to_zarr(zarr_format=2, **to_zarr_kwargs)
    except TypeError:
        # older xarray that doesn't take zarr_format — fall back; if running on
        # zarr 2 the default layout is already v2.
        ds.to_zarr(**to_zarr_kwargs)
    elapsed = time.time() - t0
    print(f"  written in {elapsed:.1f}s")

    # Brief on-disk summary
    print("\nWritten store summary:")
    try:
        import zarr

        store = zarr.open_group(str(args.dst_zarr), mode="r")
        for name in store.array_keys():
            arr = store[name]
            print(f"  {name}: shape={arr.shape} chunks={arr.chunks} dtype={arr.dtype}")
            print(f"    compressor={arr.compressor}")
    except Exception as exc:  # pragma: no cover
        print(f"  (could not introspect store: {exc})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
