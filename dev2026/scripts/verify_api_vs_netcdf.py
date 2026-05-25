"""Step 2 verification: gebco_app's data path ↔ source NetCDF.

Strategy:
  1. Wire up the SAME data-access stack gebco_app.py uses (xr.open_zarr with
     decode_cf=False, decode_times=False, chunks="auto") and the SAME
     zprofile() function from src/. We skip FastAPI's TestClient because the
     app's lifespan registers a multiprocessing.Pool(4) that OOMs the 3.8 GB
     cowork sandbox; the data-correctness question we're testing here is
     identical either way (FastAPI just JSON-serialises the same return).
  2. Optionally do a TestClient smoke-check (--with-fastapi) of one request to
     prove the HTTP layer also works end-to-end.
  3. Pick N random (lat, lon) points uniformly within the grid bounds.
  4. Call zprofile(lons, lats, "point", 1) for batches → get z values.
  5. Read the SAME (api_lat, api_lon) from the source NetCDF using nearest-
     cell lookup on the coord arrays (the API rounds inputs to 15-arcsec cell
     centres and returns the cell centre in the response — sidesteps any
     tie-breaking ambiguity).
  6. Assert exact int16 equality (Zarr is already proven byte-equal to NetCDF,
     so any difference here would indicate the API code path corrupts values).

Output is token-aware: one line per batch, mismatch details only on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import netCDF4

# Silence the boot-time noise
import warnings; warnings.simplefilter("ignore")
import dask
dask.config.set(scheduler="single-threaded")


_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=_REPO_ROOT_DEFAULT,
                   help=f"repo root containing data/ and data_src/ "
                        f"(default: auto-derived from this script's path → {_REPO_ROOT_DEFAULT})")
    p.add_argument("--zarr-path", type=Path, default=None,
                   help="default = <repo-root>/data/GEBCO_2026_sub_ice_topo.zarr")
    p.add_argument("--nc-path", type=Path, default=None,
                   help="default = <repo-root>/data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc")
    p.add_argument("--total-points", type=int, default=1000)
    p.add_argument("--batch", type=int, default=200)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--max-mismatch-print", type=int, default=20)
    p.add_argument("--with-fastapi", action="store_true",
                   help="Also run a single TestClient request as HTTP smoke check.")
    args = p.parse_args()

    if args.zarr_path is None:
        args.zarr_path = args.repo_root / "data" / "GEBCO_2026_sub_ice_topo.zarr"
    if args.nc_path is None:
        args.nc_path = args.repo_root / "data_src" / "GEBCO_2026" / "GEBCO_2026_sub_ice.nc"

    sys.path.insert(0, str(args.repo_root))
    import xarray as xr
    import src.config as config

    print(f"opening zarr {args.zarr_path}")
    config.ds = xr.open_zarr(
        str(args.zarr_path), chunks="auto",
        decode_cf=False, decode_times=False,
    )
    config.arc = int(3600 / 15)
    config.basex = 180
    config.basey = 90

    from src.zprofile import zprofile

    # --- optional FastAPI sanity smoke ----------------------------------------
    if args.with_fastapi:
        # `gebco_app.py` imports `src.polyhandler`, which top-level imports
        # `pygeos`. pygeos has no cp313 wheel and we don't exercise polygon
        # endpoints from dev2026 — inject a minimal stub into sys.modules so
        # the import succeeds. Any code that actually calls a pygeos function
        # (only the polygon code path) will raise; the point/line endpoints we
        # test here don't go anywhere near it.
        import types, sys as _sys
        if "pygeos" not in _sys.modules:
            stub = types.ModuleType("pygeos")
            class _StubGeometry:  # for pygeos.lib.Geometry isinstance checks
                pass
            stub_lib = types.ModuleType("pygeos.lib")
            stub_lib.Geometry = _StubGeometry
            stub.lib = stub_lib
            def _unsupported(*_a, **_kw):
                raise NotImplementedError(
                    "pygeos is stubbed in the dev2026 venv (no cp313 wheels). "
                    "Polygon-mode endpoints can only be exercised in the "
                    "production Pipenv env."
                )
            for name in ("get_coordinates", "bounds", "from_shapely",
                         "points", "contains"):
                setattr(stub, name, _unsupported)
            _sys.modules["pygeos"] = stub
            _sys.modules["pygeos.lib"] = stub_lib

        # Neuter multiprocessing.Pool BEFORE importing gebco_app so the
        # dask.config.set(pool=Pool(4)) at module top doesn't try to fork.
        import multiprocessing.pool as _mpp
        class _DummyPool:
            def __init__(self, *a, **k): pass
            def apply_async(self, *a, **k): raise RuntimeError("dummy pool")
            def close(self): pass
            def join(self): pass
        _mpp.Pool = _DummyPool  # type: ignore
        import gebco_app
        from fastapi.testclient import TestClient
        with TestClient(gebco_app.app) as client:
            r = client.get("/gebco?lon=122.36&lat=25.02&mode=point")
            assert r.status_code == 200, r.text
            print("[fastapi] /gebco?lon=122.36&lat=25.02&mode=point → "
                  f"{r.json()}")

    # --- ground-truth NetCDF -------------------------------------------------
    print(f"opening NetCDF {args.nc_path}")
    nc = netCDF4.Dataset(str(args.nc_path), "r")
    elev = nc.variables["elevation"]
    lat_arr = nc.variables["lat"][:]
    lon_arr = nc.variables["lon"][:]
    n_lat, n_lon = elev.shape

    rng = np.random.default_rng(args.seed)
    lats = rng.uniform(-89.9, 89.9, size=args.total_points)
    lons = rng.uniform(-179.9, 179.9, size=args.total_points)

    total_bad = 0
    bad_examples: list[tuple] = []
    t_total = time.time()

    for batch_start in range(0, args.total_points, args.batch):
        batch_end = min(batch_start + args.batch, args.total_points)
        K = batch_end - batch_start
        b_lats = lats[batch_start:batch_end]
        b_lons = lons[batch_start:batch_end]

        # IMPORTANT: zprofile(..., "point") still calls zdata_bbox() which
        # slices ds across the input min/max in lat AND lon. If the input
        # batch spans the globe, that bbox is the whole 7 GB grid → OOM in
        # a 3.8 GB sandbox. So we call zprofile one point at a time. This
        # is also the most common real-world API pattern (single-point
        # bathymetry lookup) and gives us the cleanest 1:1 comparison.
        api_lons_list, api_lats_list, api_z_list = [], [], []
        t0 = time.time()
        for i in range(K):
            r = zprofile(np.array([b_lons[i]]), np.array([b_lats[i]]), "point", 1)
            d = json.loads(r.body)
            api_lons_list.extend(d["longitude"])
            api_lats_list.extend(d["latitude"])
            api_z_list.extend(d["z"])
        t_api = time.time() - t0
        api_lons = np.asarray(api_lons_list, dtype=np.float64)
        api_lats = np.asarray(api_lats_list, dtype=np.float64)
        api_z = np.asarray(api_z_list, dtype=np.int64)
        if api_z.size != K:
            print(f"[batch {batch_start//args.batch+1}] size mismatch: K={K} got {api_z.size}",
                  file=sys.stderr)
            return 3

        # Nearest cell-centre lookup on the NetCDF coord arrays
        lat_idx = np.searchsorted(lat_arr, api_lats)
        lon_idx = np.searchsorted(lon_arr, api_lons)
        lat_idx = np.clip(lat_idx, 0, n_lat - 1)
        lon_idx = np.clip(lon_idx, 0, n_lon - 1)
        for i in range(K):
            if lat_idx[i] > 0 and abs(lat_arr[lat_idx[i]-1] - api_lats[i]) < abs(lat_arr[lat_idx[i]] - api_lats[i]):
                lat_idx[i] -= 1
            if lon_idx[i] > 0 and abs(lon_arr[lon_idx[i]-1] - api_lons[i]) < abs(lon_arr[lon_idx[i]] - api_lons[i]):
                lon_idx[i] -= 1

        t0 = time.time()
        nc_z = np.fromiter(
            (int(elev[lat_idx[i], lon_idx[i]]) for i in range(K)),
            dtype=np.int64, count=K,
        )
        t_nc = time.time() - t0

        bad_mask = api_z != nc_z
        n_bad = int(bad_mask.sum())
        total_bad += n_bad

        if n_bad == 0:
            print(f"[batch {batch_start//args.batch+1:>2d}] K={K:>4d} OK   "
                  f"api_t={t_api:.2f}s  nc_t={t_nc:.2f}s")
        else:
            for j in np.where(bad_mask)[0][: max(0, args.max_mismatch_print - len(bad_examples))]:
                bad_examples.append((
                    float(b_lats[j]), float(b_lons[j]),
                    float(api_lats[j]), float(api_lons[j]),
                    int(api_z[j]), int(nc_z[j]),
                ))
            print(f"[batch {batch_start//args.batch+1:>2d}] K={K:>4d} FAIL n_bad={n_bad}/{K}  "
                  f"api_t={t_api:.2f}s  nc_t={t_nc:.2f}s")

    nc.close()
    config.ds.close()

    print()
    print(f"TOTAL  points={args.total_points:,}  bad={total_bad:,}  "
          f"wall={time.time()-t_total:.1f}s")
    if total_bad == 0:
        print("✓ API ↔ NetCDF: every sampled point matches exactly")
        return 0
    print("✗ MISMATCHES — first examples:")
    for i, (orig_la, orig_lo, api_la, api_lo, api_v, nc_v) in enumerate(bad_examples):
        print(f"  #{i:02d} input ({orig_la:.6f},{orig_lo:.6f}) → API cell "
              f"({api_la:.6f},{api_lo:.6f})  api_z={api_v}  nc_z={nc_v}  Δ={api_v-nc_v:+d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
