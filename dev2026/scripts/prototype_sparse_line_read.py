"""W2 prototype: chunk-aware sparse read for line / MultiLineString cases.

This prototype does NOT modify production code. It compares the current
pipeline against a sparse chunk-grouped read that:

1. reuses the existing line-walk planner to get exact target cells
2. deduplicates target cells
3. groups them by Zarr chunk
4. reads each touched chunk once
5. reconstructs the full output in the original order

Primary purpose:
- validate that sparse chunk reads are byte-equal to the current output
- quantify how much wall/RSS can be saved before integrating into
  ``src.zprofile``

Usage:
    uv run --group dev python dev2026/scripts/prototype_sparse_line_read.py
    uv run --group dev python dev2026/scripts/prototype_sparse_line_read.py \
        --case q5_diag_under_cap --case multiline_aggregate_heavy
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import zarr

_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]


def _load_investigate_module(repo_root: Path):
    path = repo_root / "dev2026" / "scripts" / "investigate_line_path_caps.py"
    spec = importlib.util.spec_from_file_location("investigate_line_path_caps", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _open_xarray(path: Path):
    import xarray as xr
    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


def _start_rss_sampler() -> tuple[threading.Event, dict[str, float], threading.Thread]:
    import psutil

    process = psutil.Process()
    peak = {"rss_mb": process.memory_info().rss / (1024 * 1024)}
    stop = threading.Event()

    def _run() -> None:
        while not stop.is_set():
            rss_mb = process.memory_info().rss / (1024 * 1024)
            if rss_mb > peak["rss_mb"]:
                peak["rss_mb"] = rss_mb
            time.sleep(0.01)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return stop, peak, thread


def _df_columns(df) -> dict[str, list]:
    return {col: df[col].to_list() for col in df.columns}


def _sparse_values_from_idx(z_elev: zarr.Array, uniq_idx: np.ndarray, mlatbase: int, mlonbase: int) -> tuple[np.ndarray, dict[str, Any]]:
    """Read only the touched chunks, once each, and harvest uniq_idx values."""
    lat_chunk, lon_chunk = z_elev.chunks
    global_lat = uniq_idx[:, 0] + mlatbase
    global_lon = uniq_idx[:, 1] + mlonbase
    chunk_row = global_lat // lat_chunk
    chunk_col = global_lon // lon_chunk
    chunk_ids = np.stack((chunk_row, chunk_col), axis=1)

    by_chunk: dict[tuple[int, int], list[int]] = {}
    for pos, cid in enumerate(map(tuple, chunk_ids)):
        by_chunk.setdefault(cid, []).append(pos)

    uniq_values = np.empty((uniq_idx.shape[0],), dtype=z_elev.dtype)
    t_read = 0.0
    t_gather = 0.0

    for (cr, cc), positions in by_chunk.items():
        lat0 = int(cr * lat_chunk)
        lat1 = min(lat0 + lat_chunk, z_elev.shape[0])
        lon0 = int(cc * lon_chunk)
        lon1 = min(lon0 + lon_chunk, z_elev.shape[1])

        t0 = time.monotonic()
        chunk = z_elev[lat0:lat1, lon0:lon1]
        t_read += time.monotonic() - t0

        pos_arr = np.asarray(positions, dtype=np.int32)
        t0 = time.monotonic()
        local_lat = global_lat[pos_arr] - lat0
        local_lon = global_lon[pos_arr] - lon0
        uniq_values[pos_arr] = chunk[local_lat, local_lon]
        t_gather += time.monotonic() - t0

    return uniq_values, {
        "touched_chunks": len(by_chunk),
        "lat_chunk": int(lat_chunk),
        "lon_chunk": int(lon_chunk),
        "projected_chunk_cells": int(len(by_chunk) * lat_chunk * lon_chunk),
        "projected_chunk_bytes": int(len(by_chunk) * lat_chunk * lon_chunk * np.dtype(z_elev.dtype).itemsize),
        "chunk_read_wall_s": t_read,
        "chunk_gather_wall_s": t_gather,
    }


def _prototype_line(repo_root: Path, zarr_path: Path, case: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(repo_root))
    investigate = _load_investigate_module(repo_root)
    import src.config as config
    from src.zprofile import zprofile

    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90
    config.MAX_BBOX_CELLS_LINE = 10**12
    config.ds = _open_xarray(zarr_path)
    root = zarr.open_group(str(zarr_path), mode="r")
    z_elev = root["elevation"]

    lons = np.asarray(case["lons"], dtype=float)
    lats = np.asarray(case["lats"], dtype=float)

    current = investigate._measure_line_current_pipeline(config.ds, lons, lats, config.arc, config.basex, config.basey)

    stop, peak, thread = _start_rss_sampler()
    rss_start = peak["rss_mb"]
    t0 = time.monotonic()
    uniq_idx, inverse = np.unique(current["idx"], axis=0, return_inverse=True)
    uniq_values, sparse_meta = _sparse_values_from_idx(z_elev, uniq_idx, current["mlatbase"], current["mlonbase"])
    full_values = uniq_values[inverse]
    sparse_wall_s = time.monotonic() - t0
    stop.set()
    thread.join(timeout=1)
    sparse_peak = peak["rss_mb"]

    # Byte-equal reference from current production path
    df = zprofile(lons, lats, "zonly,dataframe", 1)
    ref = _df_columns(df)
    out = {
        "longitude": ref["longitude"],
        "latitude": ref["latitude"],
        "z": full_values.tolist(),
    }
    byte_equal = (
        ref["longitude"] == out["longitude"]
        and ref["latitude"] == out["latitude"]
        and ref["z"] == out["z"]
    )

    config.ds.close()
    return {
        "id": case["id"],
        "kind": "line",
        "rows": len(ref["longitude"]),
        "byte_equal": byte_equal,
        "current_values_ms": current["values_wall_s"] * 1000,
        "current_subset_cells": current["bbox_subset_cells"],
        "current_subset_bytes": int(current["bbox_subset_cells"] * np.dtype(z_elev.dtype).itemsize),
        "current_unique_cells": current["unique_cells"],
        "sparse_wall_ms": sparse_wall_s * 1000,
        "sparse_rss_delta_mb": sparse_peak - rss_start,
        **sparse_meta,
    }


def _prototype_multiline(repo_root: Path, zarr_path: Path, case: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(repo_root))
    investigate = _load_investigate_module(repo_root)
    import src.config as config
    from src.polyhandler import polyhandler

    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90
    config.MAX_BBOX_CELLS_LINE = 10**12
    config.ds = _open_xarray(zarr_path)
    root = zarr.open_group(str(zarr_path), mode="r")
    z_elev = root["elevation"]

    current = investigate._measure_multiline_current_pipeline(config.ds, case, config.arc, config.basex, config.basey)

    stop, peak, thread = _start_rss_sampler()
    rss_start = peak["rss_mb"]
    t0 = time.monotonic()
    full_z: list[int] = []
    touched_chunks = 0
    projected_chunk_cells_sum = 0
    projected_chunk_bytes_sum = 0
    chunk_read = 0.0
    chunk_gather = 0.0
    for coords, part in zip(case["geojson"]["coordinates"], current["part_metrics"]):
        uniq_idx, inverse = np.unique(part["idx"], axis=0, return_inverse=True)
        uniq_values, meta = _sparse_values_from_idx(z_elev, uniq_idx, part["mlatbase"], part["mlonbase"])
        full_z.extend(uniq_values[inverse].tolist())
        touched_chunks += meta["touched_chunks"]
        projected_chunk_cells_sum += meta["projected_chunk_cells"]
        projected_chunk_bytes_sum += meta["projected_chunk_bytes"]
        chunk_read += meta["chunk_read_wall_s"]
        chunk_gather += meta["chunk_gather_wall_s"]
    sparse_wall_s = time.monotonic() - t0
    stop.set()
    thread.join(timeout=1)
    sparse_peak = peak["rss_mb"]

    df, _ = polyhandler(case["geojson"], mode="zonly,dataframe", sample=1)
    ref = _df_columns(df)
    out = {
        "longitude": ref["longitude"],
        "latitude": ref["latitude"],
        "z": full_z,
    }
    byte_equal = (
        ref["longitude"] == out["longitude"]
        and ref["latitude"] == out["latitude"]
        and ref["z"] == out["z"]
    )

    config.ds.close()
    return {
        "id": case["id"],
        "kind": "multiline",
        "rows": len(ref["longitude"]),
        "byte_equal": byte_equal,
        "current_values_ms": current["values_wall_s"] * 1000,
        "current_subset_cells_sum": current["bbox_subset_cells_sum"],
        "current_subset_bytes_sum": int(current["bbox_subset_cells_sum"] * np.dtype(z_elev.dtype).itemsize),
        "current_unique_cells_sum": current["unique_cells_sum"],
        "sparse_wall_ms": sparse_wall_s * 1000,
        "sparse_rss_delta_mb": sparse_peak - rss_start,
        "touched_chunks_sum": touched_chunks,
        "projected_chunk_cells_sum": int(projected_chunk_cells_sum),
        "projected_chunk_bytes_sum": int(projected_chunk_bytes_sum),
        "chunk_read_wall_s": chunk_read,
        "chunk_gather_wall_s": chunk_gather,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    parser.add_argument("--zarr", type=Path, default=None)
    parser.add_argument("--case", action="append", default=[],
                        choices=[
                            "q5_diag_under_cap",
                            "q8_diag_over_cap",
                            "diag_under_close_cap",
                            "multiline_aggregate_heavy",
                        ])
    args = parser.parse_args()
    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    investigate = _load_investigate_module(args.repo_root)
    CASE_MAP = investigate.CASE_MAP

    cases = [CASE_MAP[c] for c in args.case] if args.case else [
        CASE_MAP["q5_diag_under_cap"],
        CASE_MAP["q8_diag_over_cap"],
        CASE_MAP["diag_under_close_cap"],
        CASE_MAP["multiline_aggregate_heavy"],
    ]

    print("=== sparse line-read prototype ===")
    for case in cases:
        if case["kind"] == "line":
            r = _prototype_line(args.repo_root, args.zarr, case)
            print(
                f"{r['id']:24s} rows={r['rows']:6d} byte_equal={r['byte_equal']} "
                f"subset={r['current_subset_cells']:,} uniq={r['current_unique_cells']:,} "
                f"values_ms={r['current_values_ms']:.1f} sparse_ms={r['sparse_wall_ms']:.1f} "
                f"chunks={r['touched_chunks']} "
                f"subset_MB={r['current_subset_bytes']/1024/1024:.1f} "
                f"chunk_MB={r['projected_chunk_bytes']/1024/1024:.1f} "
                f"rssΔ={r['sparse_rss_delta_mb']:.1f}MB"
            )
        else:
            r = _prototype_multiline(args.repo_root, args.zarr, case)
            print(
                f"{r['id']:24s} rows={r['rows']:6d} byte_equal={r['byte_equal']} "
                f"subset_sum={r['current_subset_cells_sum']:,} uniq_sum={r['current_unique_cells_sum']:,} "
                f"values_ms={r['current_values_ms']:.1f} sparse_ms={r['sparse_wall_ms']:.1f} "
                f"chunks_sum={r['touched_chunks_sum']} "
                f"subset_MB={r['current_subset_bytes_sum']/1024/1024:.1f} "
                f"chunk_MB={r['projected_chunk_bytes_sum']/1024/1024:.1f} "
                f"rssΔ={r['sparse_rss_delta_mb']:.1f}MB"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
