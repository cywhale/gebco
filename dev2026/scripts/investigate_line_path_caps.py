"""Investigate line / multiline near-cap behaviour for v0.5.x.

This script collects the evidence we need before retuning
``GEBCO_MAX_BBOX_CELLS_LINE`` or redesigning the non-polygon read path.

It supports two complementary modes:

1. direct path (default)
   Imports ``src.zprofile`` / ``src.polyhandler`` against the local Zarr
   and measures:
   - estimated bbox cell count used by the current guard
   - output rows
   - wall time
   - sampled peak RSS

   Each case runs in a fresh subprocess so the RSS peak is isolated.

2. HTTP target
   Sends the same cases to a live endpoint (e.g. VM37 loopback
   ``https://127.0.0.1:8013/gebco``) and measures:
   - HTTP status
   - returned rows
   - serial median wall time
   - concurrent per-request median and round wall time

Examples:
    uv run --group dev python dev2026/scripts/investigate_line_path_caps.py
    uv run --group dev python dev2026/scripts/investigate_line_path_caps.py \\
        --case q5_diag_under_cap --case q8_diag_over_cap
    uv run --group dev python dev2026/scripts/investigate_line_path_caps.py \\
        --http-target https://127.0.0.1:8013/gebco --insecure
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_DEFAULT))

from src.line_planner import plan_line_cells


def _line_case(
    case_id: str,
    label: str,
    lons: list[float],
    lats: list[float],
    *,
    sample: int = 3,
    mode: str = "zonly",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "kind": "line",
        "label": label,
        "query_sample": sample,
        "mode": mode,
        "lons": lons,
        "lats": lats,
    }


def _multiline_case(
    case_id: str,
    label: str,
    coordinates: list[list[list[float]]],
    *,
    sample: int = 3,
    mode: str = "zonly",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "kind": "multiline",
        "label": label,
        "query_sample": sample,
        "mode": mode,
        "geojson": {"type": "MultiLineString", "coordinates": coordinates},
    }


CASES: list[dict[str, Any]] = [
    _line_case(
        "q5_diag_under_cap",
        "Diagonal polyline under current 2e8 line-cap (~0.93x cap)",
        [-150, -138, -126, -114, -102, -90],
        [-28, -16, -4, 8, 18, 26],
    ),
    _line_case(
        "q8_diag_over_cap",
        "Diagonal polyline just over current 2e8 line-cap (~1.05x cap)",
        [-155, -143, -131, -118, -105, -92],
        [-30, -18, -6, 6, 18, 28],
    ),
    _line_case(
        "diag_under_close_cap",
        "Diagonal polyline just under cap (~0.97x cap)",
        [-155, -143, -131, -119, -107, -95],
        [-30, -18, -6, 6, 16, 26],
    ),
    _line_case(
        "horiz_wide_lowbbox",
        "Very wide near-horizontal transect; many rows, small bbox",
        [-170, 20],
        [0, 1],
        sample=1,
    ),
    _line_case(
        "vert_wide_lowbbox",
        "Very wide near-vertical transect; many rows, small bbox",
        [-10, -9],
        [-70, 70],
        sample=1,
    ),
    _multiline_case(
        "multiline_aggregate_heavy",
        "Large MultiLineString aggregate-heavy case; each part under cap",
        [
            [[-170.0, -25.0], [-145.0, -10.0], [-120.0, 5.0], [-95.0, 18.0]],
            [[-20.0, -30.0], [5.0, -10.0], [28.0, 8.0], [42.0, 24.0]],
        ],
    ),
]
CASE_MAP = {case["id"]: case for case in CASES}


def _bbox_cells_for_coords(lons: list[float], lats: list[float], arc: int = 240) -> int:
    mlon0 = max(min(lons) - 1.5 / arc, -180 + 0.00001)
    mlon1 = min(max(lons) + 1.5 / arc, 180 - 0.00001)
    mlat0 = max(min(lats) - 1.5 / arc, -90 + 0.00001)
    mlat1 = min(max(lats) + 1.5 / arc, 90 - 0.00001)
    return int(round((mlon1 - mlon0) * (mlat1 - mlat0) * arc * arc))


def _case_bbox_summary(case: dict[str, Any]) -> dict[str, Any]:
    if case["kind"] == "line":
        bbox_cells = _bbox_cells_for_coords(case["lons"], case["lats"])
        return {"guard_bbox_cells": bbox_cells}

    part_cells = []
    for coords in case["geojson"]["coordinates"]:
        lons = [pt[0] for pt in coords]
        lats = [pt[1] for pt in coords]
        part_cells.append(_bbox_cells_for_coords(lons, lats))
    return {
        "part_bbox_cells_max": max(part_cells),
        "part_bbox_cells_sum": sum(part_cells),
        "n_parts": len(part_cells),
    }


def _case_params(case: dict[str, Any]) -> dict[str, str]:
    if case["kind"] == "line":
        return {
            "lon": ",".join(str(v) for v in case["lons"]),
            "lat": ",".join(str(v) for v in case["lats"]),
            "mode": case["mode"],
            # Matches the public API query shape even though line/point
            # paths currently force sample=1 internally.
            "sample": str(case["query_sample"]),
        }
    return {
        "mode": case["mode"],
        # Matches the public API query shape even though MultiLineString
        # also ends up on the line path with sample forced to 1.
        "sample": str(case["query_sample"]),
        "jsonsrc": json.dumps(case["geojson"], separators=(",", ":")),
    }


def _open_zarr(path: Path):
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


def _row_count_from_result(result: Any) -> int:
    if hasattr(result, "height"):  # polars DataFrame
        return int(result.height)
    if hasattr(result, "body"):
        body = json.loads(result.body)
        return len(body.get("longitude", []))
    raise TypeError(f"unsupported result type: {type(result)!r}")


def _chunk_touch_summary(idx: np.ndarray, mlatbase: int, mlonbase: int, lat_chunk: int, lon_chunk: int) -> dict[str, int]:
    if idx.size == 0:
        return {
            "unique_cells": 0,
            "touched_chunks": 0,
            "projected_chunk_cells": 0,
        }
    uniq = np.unique(idx, axis=0)
    global_lat = uniq[:, 0] + mlatbase
    global_lon = uniq[:, 1] + mlonbase
    chunk_ids = np.stack((global_lat // lat_chunk, global_lon // lon_chunk), axis=1)
    uniq_chunks = np.unique(chunk_ids, axis=0)
    return {
        "unique_cells": int(uniq.shape[0]),
        "touched_chunks": int(uniq_chunks.shape[0]),
        "projected_chunk_cells": int(uniq_chunks.shape[0] * lat_chunk * lon_chunk),
    }


def _measure_line_current_pipeline(ds, lons: np.ndarray, lats: np.ndarray, arc: int, basex: int, basey: int) -> dict[str, Any]:
    t0 = time.monotonic()
    plan = plan_line_cells(lons, lats, arc, basex, basey)
    plan_wall_s = time.monotonic() - t0
    idx = plan.idx
    mlonbase = plan.mlonbase
    mlatbase = plan.mlatbase
    mlon1_idx = mlonbase + plan.bbox_width - 1
    mlat1_idx = mlatbase + plan.bbox_height - 1
    mlon0x = ds["lon"][mlonbase].item()
    mlat0x = ds["lat"][mlatbase].item()
    mlon1x = ds["lon"][mlon1_idx].item()
    mlat1x = ds["lat"][mlat1_idx].item()

    t0 = time.monotonic()
    ds_s1 = ds.sel(lon=slice(mlon0x, mlon1x, 1), lat=slice(mlat0x, mlat1x, 1))
    ds_sel_wall_s = time.monotonic() - t0

    t0 = time.monotonic()
    elev_full = ds_s1["elevation"].values
    values_wall_s = time.monotonic() - t0

    t0 = time.monotonic()
    selected = elev_full[tuple(idx.T)] if idx.size else np.empty((0,), dtype=elev_full.dtype)
    gather_wall_s = time.monotonic() - t0
    ds_s1.close()

    lat_chunk, lon_chunk = ds["elevation"].data.chunksize
    return {
        **plan.__dict__,
        **_chunk_touch_summary(idx, mlatbase, mlonbase, lat_chunk, lon_chunk),
        "planner_wall_s": plan_wall_s,
        "ds_sel_wall_s": ds_sel_wall_s,
        "values_wall_s": values_wall_s,
        "gather_wall_s": gather_wall_s,
        "materialized_rows": int(selected.shape[0]),
        "lat_chunk": int(lat_chunk),
        "lon_chunk": int(lon_chunk),
    }


def _measure_multiline_current_pipeline(ds, case: dict[str, Any], arc: int, basex: int, basey: int) -> dict[str, Any]:
    part_metrics = []
    total_rows = 0
    planner_wall = ds_sel_wall = values_wall = gather_wall = 0.0
    bbox_subset_cells_sum = 0
    unique_cells_sum = 0
    touched_chunks_sum = 0
    projected_chunk_cells_sum = 0
    inner_iters_sum = 0
    lat_chunk, lon_chunk = ds["elevation"].data.chunksize
    global_chunk_ids: set[tuple[int, int]] = set()
    for coords in case["geojson"]["coordinates"]:
        lons = np.asarray([pt[0] for pt in coords], dtype=float)
        lats = np.asarray([pt[1] for pt in coords], dtype=float)
        part = _measure_line_current_pipeline(ds, lons, lats, arc, basex, basey)
        part_metrics.append(part)
        total_rows += part["materialized_rows"]
        planner_wall += part["planner_wall_s"]
        ds_sel_wall += part["ds_sel_wall_s"]
        values_wall += part["values_wall_s"]
        gather_wall += part["gather_wall_s"]
        bbox_subset_cells_sum += part["bbox_subset_cells"]
        unique_cells_sum += part["unique_cells"]
        touched_chunks_sum += part["touched_chunks"]
        projected_chunk_cells_sum += part["projected_chunk_cells"]
        inner_iters_sum += part["inner_iters"]
        idx = part["idx"]
        if idx.size:
            global_lat = idx[:, 0] + part["mlatbase"]
            global_lon = idx[:, 1] + part["mlonbase"]
            for a, b in zip(global_lat // lat_chunk, global_lon // lon_chunk):
                global_chunk_ids.add((int(a), int(b)))
    return {
        "rows": total_rows,
        "planner_wall_s": planner_wall,
        "ds_sel_wall_s": ds_sel_wall,
        "values_wall_s": values_wall,
        "gather_wall_s": gather_wall,
        "bbox_subset_cells_sum": int(bbox_subset_cells_sum),
        "unique_cells_sum": int(unique_cells_sum),
        "touched_chunks_sum": int(touched_chunks_sum),
        "touched_chunks_union": int(len(global_chunk_ids)),
        "projected_chunk_cells_sum": int(projected_chunk_cells_sum),
        "projected_chunk_cells_union": int(len(global_chunk_ids) * lat_chunk * lon_chunk),
        "inner_iters_sum": int(inner_iters_sum),
        "lat_chunk": int(lat_chunk),
        "lon_chunk": int(lon_chunk),
        "part_metrics": part_metrics,
    }


def _run_direct_case(case: dict[str, Any], repo_root: Path, zarr: Path) -> dict[str, Any]:
    sys.path.insert(0, str(repo_root))
    import src.config as config
    from src.polyhandler import polyhandler
    from src.zprofile import BboxTooLarge, zprofile

    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90
    config.ds = _open_zarr(zarr)
    arc = config.arc
    basex = config.basex
    basey = config.basey

    bbox_summary = _case_bbox_summary(case)
    stop, peak, thread = _start_rss_sampler()
    rss_start = peak["rss_mb"]
    t0 = time.monotonic()
    status = 200
    rows = 0
    error = None
    sha256 = None
    stage_metrics: dict[str, Any] = {}
    try:
        if case["kind"] == "line":
            stage_metrics = _measure_line_current_pipeline(
                config.ds,
                np.asarray(case["lons"], dtype=float),
                np.asarray(case["lats"], dtype=float),
                arc,
                basex,
                basey,
            )
            result = zprofile(
                np.asarray(case["lons"], dtype=float),
                np.asarray(case["lats"], dtype=float),
                case["mode"] + ",dataframe",
                1,
            )
        else:
            stage_metrics = _measure_multiline_current_pipeline(
                config.ds, case, arc, basex, basey
            )
            result, _ = polyhandler(
                case["geojson"],
                mode=case["mode"] + ",dataframe",
                sample=1,
            )
        rows = _row_count_from_result(result)
        if hasattr(result, "write_json"):
            sha256 = hashlib.sha256(result.write_json().encode("utf-8")).hexdigest()
    except BboxTooLarge as exc:
        status = 413
        error = str(exc)
    finally:
        wall_s = time.monotonic() - t0
        stop.set()
        thread.join(timeout=1)
        config.ds.close()

    return {
        "id": case["id"],
        "label": case["label"],
        "kind": case["kind"],
        "status": status,
        "rows": rows,
        "wall_s": wall_s,
        "peak_rss_mb": peak["rss_mb"],
        "rss_delta_mb": peak["rss_mb"] - rss_start,
        "error": error,
        "sha256": sha256,
        **bbox_summary,
        **stage_metrics,
    }


def _worker_main(args: argparse.Namespace) -> int:
    case = CASE_MAP[args.worker_case]
    result = _run_direct_case(case, args.repo_root, args.zarr)
    def _strip(obj):
        if isinstance(obj, dict):
            return {k: _strip(v) for k, v in obj.items() if k != "idx"}
        if isinstance(obj, list):
            return [_strip(v) for v in obj]
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj
    print(json.dumps(_strip(result), separators=(",", ":")))
    return 0


def _run_worker(case_id: str, repo_root: Path, zarr: Path) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--repo-root",
        str(repo_root),
        "--zarr",
        str(zarr),
        "--worker-case",
        case_id,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout.strip())


def _http_call(url: str, *, timeout_s: float = 60.0, insecure: bool = False) -> dict[str, Any]:
    ctx = ssl._create_unverified_context() if insecure else None
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout_s, context=ctx) as resp:
            raw = resp.read()
            wall_s = time.monotonic() - t0
            body = json.loads(raw)
            return {
                "status": resp.status,
                "wall_s": wall_s,
                "rows": len(body.get("longitude", [])) if isinstance(body, dict) else None,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "n_bytes": len(raw),
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return {
            "status": exc.code,
            "wall_s": time.monotonic() - t0,
            "rows": None,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "n_bytes": len(raw),
            "error": raw.decode("utf-8", errors="replace")[:160],
        }


def _build_url(base: str, case: dict[str, Any]) -> str:
    qs = urllib.parse.urlencode(_case_params(case), quote_via=urllib.parse.quote)
    return f"{base}?{qs}"


def _http_case(case: dict[str, Any], base: str, trials: int, concurrency_levels: list[int], insecure: bool) -> dict[str, Any]:
    url = _build_url(base, case)
    serial = [_http_call(url, insecure=insecure) for _ in range(trials)]
    serial_median_ms = statistics.median(r["wall_s"] for r in serial) * 1000
    out = {
        "id": case["id"],
        "label": case["label"],
        "kind": case["kind"],
        "url": url,
        "serial_status": serial[-1]["status"],
        "serial_rows": serial[-1]["rows"],
        "serial_median_ms": serial_median_ms,
        "serial_sha256": serial[-1]["sha256"],
        **_case_bbox_summary(case),
    }

    for level in concurrency_levels:
        if level <= 1:
            continue
        round_walls = []
        req_walls = []
        statuses = []
        for _ in range(trials):
            t0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=level) as pool:
                results = list(pool.map(lambda _: _http_call(url, insecure=insecure), range(level)))
            round_walls.append(time.monotonic() - t0)
            req_walls.extend(r["wall_s"] for r in results)
            statuses.extend(r["status"] for r in results)
        out[f"c{level}_req_median_ms"] = statistics.median(req_walls) * 1000
        out[f"c{level}_round_median_ms"] = statistics.median(round_walls) * 1000
        out[f"c{level}_statuses"] = sorted(set(statuses))
    return out


def _print_direct(results: list[dict[str, Any]], line_cap: int) -> None:
    print("=== direct path ===")
    print("case                           status rows     wall_ms peak_rss rss_delta cap_ratio cells/chunks")
    for r in results:
        if "guard_bbox_cells" in r:
            cells = r["guard_bbox_cells"]
            cap_ratio = cells / line_cap
            metric = (
                f"bbox={cells:,};subset={r.get('bbox_subset_cells',0):,};"
                f"uniq={r.get('unique_cells',0):,};chunks={r.get('touched_chunks',0)};"
                f"chunk_cells={r.get('projected_chunk_cells',0):,}"
            )
        else:
            cells = r["part_bbox_cells_max"]
            cap_ratio = cells / line_cap
            metric = (
                f"part_max={r['part_bbox_cells_max']:,};"
                f"part_sum={r['part_bbox_cells_sum']:,};"
                f"uniq_sum={r.get('unique_cells_sum',0):,};"
                f"chunk_union={r.get('touched_chunks_union',0)};"
                f"chunk_cells_union={r.get('projected_chunk_cells_union',0):,}"
            )
        print(
            f"{r['id'][:28]:28s} {r['status']:>6} {r['rows']:>6} "
            f"{r['wall_s']*1000:9.1f} {r['peak_rss_mb']:8.1f} {r['rss_delta_mb']:9.1f} "
            f"{cap_ratio:8.2f} {metric}"
        )
        if "planner_wall_s" in r:
            if "guard_bbox_cells" in r:
                print(
                    f"  stages: plan={r['planner_wall_s']*1000:.1f}ms "
                    f"sel={r['ds_sel_wall_s']*1000:.1f}ms "
                    f"values={r['values_wall_s']*1000:.1f}ms "
                    f"gather={r['gather_wall_s']*1000:.1f}ms "
                    f"inner_iters={r['inner_iters']:,} chunk={r['lat_chunk']}x{r['lon_chunk']}"
                )
            else:
                print(
                    f"  stages(sum): plan={r['planner_wall_s']*1000:.1f}ms "
                    f"sel={r['ds_sel_wall_s']*1000:.1f}ms "
                    f"values={r['values_wall_s']*1000:.1f}ms "
                    f"gather={r['gather_wall_s']*1000:.1f}ms "
                    f"inner_iters={r['inner_iters_sum']:,} chunk_union={r['touched_chunks_union']}"
                )
        if r.get("error"):
            print(f"  error: {r['error']}")


def _print_http(results: list[dict[str, Any]]) -> None:
    print("=== http target ===")
    print("case                           status rows     serial_ms  c2_req/c2_round  c4_req/c4_round")
    for r in results:
        c2 = "-"
        if "c2_req_median_ms" in r:
            c2 = f"{r['c2_req_median_ms']:.1f}/{r['c2_round_median_ms']:.1f}"
        c4 = "-"
        if "c4_req_median_ms" in r:
            c4 = f"{r['c4_req_median_ms']:.1f}/{r['c4_round_median_ms']:.1f}"
        print(
            f"{r['id'][:28]:28s} {r['serial_status']:>6} {str(r['serial_rows'] or '-'):>6} "
            f"{r['serial_median_ms']:11.1f} {c2:>16s} {c4:>16s}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    parser.add_argument("--zarr", type=Path, default=None,
                        help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    parser.add_argument("--case", action="append", choices=sorted(CASE_MAP), default=[],
                        help="limit to one or more case ids")
    parser.add_argument("--http-target", type=str, default=None,
                        help="if set, run HTTP mode against this base URL")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (useful for https://127.0.0.1:8013)")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--concurrency", type=str, default="1,2,4",
                        help="comma-separated concurrency levels for HTTP mode")
    parser.add_argument("--worker-case", choices=sorted(CASE_MAP), default=None,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    if args.worker_case:
        return _worker_main(args)

    cases = [CASE_MAP[c] for c in args.case] if args.case else CASES
    sys.path.insert(0, str(args.repo_root))
    import src.config as config
    line_cap = config.MAX_BBOX_CELLS_LINE

    if args.http_target:
        levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
        results = [
            _http_case(case, args.http_target, args.trials, levels, args.insecure)
            for case in cases
        ]
        _print_http(results)
        return 0

    results = [_run_worker(case["id"], args.repo_root, args.zarr) for case in cases]
    _print_direct(results, line_cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
