"""P-Bench-2 — polygon-shape benchmark for v0.5.4 W2-B.

Drives the **real** ``src.polyhandler.polyhandler()`` on rectangles
sized to the Phase D2 archetype bbox cell counts:

  * ``dense_rect``    — ~6.7M raw bbox cells  (D2 safe-up-to point)
  * ``thin_ribbon``   — ~1.05M raw bbox cells (D2 first-bad knee)
  * ``coastal``       — ~1.0M raw bbox cells, aspect 6:1 (D2 baseline)
  * ``cross180_thin`` — ~46M raw bbox cells, polygon crosses 180°
                        (D2 probe size that produced the 112 s number)

Why rectangles (not the probe's exact diagonal ribbons): the probe's
``_cross180_parts`` post-split lon-translation has a vertex-at-180
quirk that produces wider-than-intended bboxes when consumed by
``polyhandler.process_polygon`` (which has its own split-at-180 path).
Rectangles in -180..180 space avoid the cross-implementation foot-gun
while still hitting the same raw-bbox-cell counts. The bbox-bound cost
that W2-B targets (meshgrid + shapely.points + shapely.contains over the
full bbox) is independent of the polygon's interior shape — so a
rectangle is, if anything, a slightly **harder** benchmark for harvest
cost (mask ratio ≈ 1.0 instead of D2's 0.07).

The benchmark therefore defaults to ``--sample 1`` (no downsampling) to
match the D2 conditions and the v0.5.4 plan §12 acceptance thresholds.
At the production endpoint default (``sample=5``), every bbox cell
count drops 25× and W2-B's row-batch effect tapers off; pass
``--sample 5`` for production-endpoint numbers.

Plan §12 acceptance (sample=1, post-W2-B):

  * thin_ribbon   ≤ 0.5 s
  * coastal       ≤ 1.5 s
  * cross180_thin ≤ 30 s  AND  ≤ 2000 MB

Usage:
    uv run python dev2026/scripts/benchmark_polygon_shapes.py
    uv run python dev2026/scripts/benchmark_polygon_shapes.py --sample 5
    uv run python dev2026/scripts/benchmark_polygon_shapes.py --shape cross180_thin --trials 1
"""
from __future__ import annotations

import argparse
import math
import sys
import time
import warnings
from pathlib import Path

import numpy as np

warnings.simplefilter("ignore")

_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
_ARC = 240


# v0.5.4 plan §12 acceptance thresholds (sample=1, post-W2-B).
ACCEPTANCE = {
    "thin_ribbon":   {"wall_s": 0.5,  "rss_mb": None},
    "coastal":       {"wall_s": 1.5,  "rss_mb": None},
    "cross180_thin": {"wall_s": 30.0, "rss_mb": 2000.0},
}


def _rect(center_lon: float, center_lat: float,
          width_deg: float, height_deg: float) -> list[list[float]]:
    """Return the 5-vertex GeoJSON ring of an axis-aligned rectangle.

    Vertex order is CCW; the ring closes by repeating the first vertex.
    """
    x0 = center_lon - width_deg / 2.0
    x1 = center_lon + width_deg / 2.0
    y0 = center_lat - height_deg / 2.0
    y1 = center_lat + height_deg / 2.0
    return [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]


def _bbox_cells(coords: list[list[float]]) -> int:
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return int(round((max(xs) - min(xs)) * (max(ys) - min(ys)) * _ARC * _ARC))


def _to_geojson_polygon(shapely_poly) -> dict:
    """Convert a shapely Polygon (exterior only) to a GeoJSON Polygon
    dict the production polyhandler accepts."""
    coords = [[x, y] for x, y in shapely_poly.exterior.coords]
    return {"type": "Polygon", "coordinates": [coords]}


def _build_shapes(repo_root) -> dict:
    """Build the four D2 archetype shapes by reusing the probe's exact
    constructors. The probe shapes are diagonal ribbons / coastal zig-zags
    with **mask ratio ~0.04-0.07**, NOT rectangles — this matches the
    Phase D2 numbers that plan §12 thresholds were derived from.

    For cross180_thin we DO NOT feed the probe's two split pieces directly
    (the probe's `_cross180_parts` has a vertex-at-180 conversion quirk
    that produces wrong-side bboxes when consumed by polyhandler).
    Instead we wrap them as a MultiPolygon of pre-split halves in
    -180..180 space; polyhandler processes each part separately via
    `process_polygon_part`, which is exactly the function W2-B
    redesigned. The split-at-180 path inside polyhandler is exercised
    independently by `verify_polygon_meridian.py` T4 / T7.
    """
    sys.path.insert(0, str(repo_root / "dev2026" / "scripts"))
    import probe_polygon_cap_shapes as ppc

    shapes = {
        "dense_rect": {
            "label": "Dense rectangle (D2 safe-up-to ~6.73M raw cells)",
            "geojson": _to_geojson_polygon(ppc._dense_rect(6_728_836)),
        },
        "thin_ribbon": {
            "label": "Diagonal thin ribbon (D2 knee ~1.05M raw cells, mask~0.07)",
            "geojson": _to_geojson_polygon(ppc._thin_rect(1_052_676)),
        },
        "coastal": {
            "label": "Coastal jagged (D2 baseline ~1.0M raw cells, aspect 6:1)",
            "geojson": _to_geojson_polygon(ppc._coastal_jagged(999_600)),
        },
    }

    # cross180_thin — feed both halves as a MultiPolygon. Each half is a
    # shapely Polygon already in -180..180 space (the probe converts).
    # polyhandler.polyhandler() iterates `MultiPolygon.geoms` and calls
    # `process_polygon` for each, which in turn calls process_polygon_part.
    # That bypasses polyhandler's own split-at-180, but the cost path
    # that W2-B targets (meshgrid + contains + harvest per half) is the
    # same as the full split-driven flow.
    #
    # The probe's `_cross180_parts` constructor has a lon-translation
    # quirk: after `split(...) + (lon - 360 if lon > 180)` translation,
    # one piece spans ~165° to 180° while the OTHER spans both 180° AND
    # some negative range (because vertex-at-180 stays at +180 instead
    # of mapping to -180). Net effect: actual measured bbox cells ≈ 46×
    # the constructor's `target`. D2's recorded `cross180_thin first_bad
    # raw_cells = 46,497,188` corresponds to `target=1_000_000`.
    CROSS180_TARGET = 1_000_000
    parts = ppc._cross180_parts(CROSS180_TARGET)
    poly_coords = []
    for part in parts:
        poly_coords.append([[[x, y] for x, y in part.exterior.coords]])
    shapes["cross180_thin"] = {
        "label": "Cross-180 thin ribbon (D2 probe ~46M raw cells, two halves)",
        "geojson": {"type": "MultiPolygon", "coordinates": poly_coords},
    }
    return shapes


def _open_zarr(path: Path):
    import xarray as xr
    return xr.open_zarr(str(path), chunks="auto", decode_cf=False, decode_times=False)


def _rss_mb() -> float:
    try:
        import psutil
    except ImportError:
        return float("nan")
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _run_one(archetype: dict, sample: int, trials: int) -> dict:
    from src.polyhandler import polyhandler
    geojson = archetype["geojson"]

    rss0 = _rss_mb()
    walls: list[float] = []
    rows: int = 0
    for _ in range(trials):
        t0 = time.monotonic()
        df, _ = polyhandler(geojson, 0, "", 1, sample)
        walls.append(time.monotonic() - t0)
        rows = int(df.height)
    rss_after = _rss_mb()

    walls_arr = np.asarray(walls)
    return {
        "label": archetype["label"],
        "rows": rows,
        "median_s": float(np.median(walls_arr)),
        "p95_s": float(np.percentile(walls_arr, 95)),
        "max_s": float(walls_arr.max()),
        "rss_delta_mb": rss_after - rss0,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT)
    p.add_argument("--zarr", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    p.add_argument("--shape", default="all",
                   choices=["dense_rect", "thin_ribbon", "coastal",
                            "cross180_thin", "all"])
    p.add_argument("--sample", type=int, default=1,
                   help="poly_sample (default 1 to match D2 / plan §12 acceptance; "
                        "pass 5 for production-endpoint numbers)")
    p.add_argument("--trials", type=int, default=3)
    args = p.parse_args()

    if args.zarr is None:
        args.zarr = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"

    sys.path.insert(0, str(args.repo_root))
    import src.config as config
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90
    # Very generous polygon cap so the H6 guard never pre-empts the probe.
    config.MAX_POLYGON_CELLS = 10**12
    config.ds = _open_zarr(args.zarr)

    shapes = _build_shapes(args.repo_root)
    targets = list(shapes) if args.shape == "all" else [args.shape]

    print(f"[bench] zarr={args.zarr}  sample={args.sample}  trials={args.trials}")
    batch_knob = getattr(config, "POLYGON_BATCH_CELLS", None)
    print(f"[bench] POLYGON_BATCH_CELLS={batch_knob}")
    if args.sample != 1:
        print("[bench] NOTE: sample != 1, plan §12 thresholds were measured at sample=1")
    print()

    header = f"{'shape':14s}  {'rows':>9s}  {'median_s':>10s}  {'p95_s':>8s}  {'rss_Δ_MB':>10s}  {'verdict':>9s}"
    print(header)
    print("-" * len(header))

    overall_ok = True
    for shape_id in targets:
        res = _run_one(shapes[shape_id], args.sample, args.trials)
        thr = ACCEPTANCE.get(shape_id, {})
        if args.sample == 1:
            wall_ok = thr.get("wall_s") is None or res["median_s"] <= thr["wall_s"]
            rss_ok = thr.get("rss_mb") is None or res["rss_delta_mb"] <= thr["rss_mb"]
            ok = wall_ok and rss_ok
            verdict = "PASS" if ok else "FAIL"
        else:
            ok = True
            verdict = "n/a"
        overall_ok = overall_ok and ok
        print(f"{shape_id:14s}  {res['rows']:9d}  "
              f"{res['median_s']:10.3f}  {res['p95_s']:8.3f}  "
              f"{res['rss_delta_mb']:10.1f}  {verdict:>9s}")

    print()
    if args.sample == 1:
        if overall_ok:
            print("=> all archetypes within v0.5.4 plan §12 acceptance thresholds")
        else:
            print("=> at least one archetype EXCEEDS its v0.5.4 plan §12 threshold;")
            print("   see specs/v0.5.4_performance_hardening_plan.md §12.")
    else:
        print(f"=> sample={args.sample} run; no acceptance verdict applied")

    config.ds.close()
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
