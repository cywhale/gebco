# AGENTS.md — handover notes for AI coding agents

This file is the single source of truth for any AI agent (Claude Code, Cursor,
Cody, …) picking up this repo. It complements `README.md` (which is for human
end-users of the API) by covering everything an agent needs to maintain and
extend the project safely.

If you only have time to read one section, read **Project map** and **Critical
gotchas**.

---

## Project map

```
gebco/
├── gebco_app.py          # FastAPI app. Opens a Zarr in lifespan, exposes /gebco.
├── src/
│   ├── config.py         # Module-global `ds` (xarray Dataset) + grid constants
│   ├── zprofile.py       # Core elevation/distance lookup for point & line modes
│   ├── polyhandler.py    # GeoJSON polygon handling (native Shapely 2 APIs)
│   └── xmeridian.py      # 180°/0° meridian crossing helpers
├── data/                 # Zarr stores (NOT committed, see "Data sources")
├── data_src/             # Source NetCDF files (NOT committed)
├── dev/                  # Historical: 2022→2023 conversion notebook
├── dev2026/              # Current: 2023→2026 conversion + verification tooling
│   ├── README.md         # How to run the dev2026 scripts
│   ├── TESTING.md        # Latest verification run (reproducible)
│   ├── pyproject.toml    # Separate uv venv: Python 3.13, zarr>=2.18,<3 …
│   ├── scripts/          # convert / verify / compare / benchmark
│   └── tests/            # pytest sanity checks
├── conf/                 # gunicorn/pm2 config
├── scripts/              # deployment/runtime helper scripts
├── simu/                 # legacy experiment scripts (ignore unless asked)
├── Pipfile / Pipfile.lock  # Production runtime (Python 3.11, Pipenv)
├── requirements.txt      # Mirror of Pipfile (for non-Pipenv deployments)
├── change_log.md
└── README.md             # End-user facing
```

## Branch model

* **main** — production. Touch only via reviewed PR.
* **gebco_2026_api** — current upgrade branch (data source 2023 → 2026).
  Modifies only `gebco_app.py` and adds `dev2026/`. **Do not delete after
  merge** until at least one full GEBCO release cycle (it's the only place the
  conversion recipe is captured outside the changelog).
* Historical: `dev/` keeps the 2022→2023 Jupyter record as-is. Don't port it
  forward; mirror the pattern in `dev2026/` (new dir per data-version bump).

## Two parallel Python environments — keep them straight

| Concern           | Production (`./`)                                  | Tooling (`./dev2026/`)                       |
|-------------------|----------------------------------------------------|----------------------------------------------|
| Manager           | uv (`pyproject.toml`) for v0.5.1+ deployment       | uv (`pyproject.toml`)                        |
| Python            | 3.11 for current staging/deploy plan               | 3.13                                         |
| zarr              | 2.18.6                                             | 2.18 ≤ x < 3 (or 3.x writing v2 layout)      |
| numcodecs         | 0.15.1 (see gotcha)                                | ≥ 0.15.1 (Blosc/Zlib codecs are stable)      |
| Used by           | gunicorn/uvicorn FastAPI runtime on VMs            | offline conversion + offline verification    |

The production app reads the Zarr; the dev2026 tooling writes it. As long as
the on-disk Zarr is **v2 layout** with a codec the production env has
(`Blosc/lz4` works in both), versions on either side can drift independently.

## Data sources

The repo ignores `data/` and `data_src/`. Each new GEBCO release goes through:

```
data_src/GEBCO_YYYY/<file>.nc   ← downloaded NetCDF (~7 GB unzipped)
data/GEBCO_YYYY_sub_ice_topo.zarr  ← converted, served by gebco_app.py
```

For GEBCO_2026 the canonical files are:

* Source: `data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc`
  (from https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/sub_ice_topography_bathymetry/netcdf/GEBCO_2026_sub_ice.zip)
* Served: `data/GEBCO_2026_sub_ice_topo.zarr`
  (Blosc/lz4 clevel=5 shuffle=1, chunks 675×2700, zarr v2)
* DOI: `10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa`

**Variants matter**: GEBCO publishes `ice_surface_elevation` and
`sub_ice_topography_bathymetry`. The production API serves the **sub_ice**
variant. They have identical shape/dtype/dim-names; only pixel values over
Greenland/Antarctica differ. Always download the sub_ice variant for the
production Zarr; the ice_surface variant is only useful for schema checks.

## Critical gotchas (read before touching anything)

1. **Zarr compressor must be Blosc/lz4, not Zlib.**
   `numcodecs.Zlib(level=1)` decompresses ~2.5× slower than `Blosc(cname="lz4",
   clevel=5, shuffle=1)` for this workload. The 2023 production Zarr uses
   Blosc; the 2026 Zarr must too. See `dev2026/TESTING.md` Step 4 for measured
   numbers (2.66× slowdown if you forget; 1.01× if you don't).
   *Asymmetric defaults to watch:* `convert_to_zarr.py` defaults to
   `--codec blosc` (correct for production). `convert_incremental.py`
   still defaults to `--codec zlib` for back-compat with the 2022→2023
   notebook — **always pass `--codec blosc` to the incremental script.**

2. **2 GiB-per-buffer ceiling on numcodecs codecs.**
   A single uncompressed chunk going through a codec must be < 2 GiB. The
   pixel grid is 43200×86400 int16 (~7 GiB) — chunks **must** be subdivided.
   Default `{lat: 675, lon: 2700}` (3.6 MiB/chunk → 2048 chunks) is the
   battle-tested value carried over from the 2023 upgrade.

3. **Source NetCDF has no internal chunking.**
   `xr.open_dataset(..., chunks={})` will treat the entire 7 GiB grid as one
   dask chunk and OOM on any machine with less than 8 GB RAM. **Always pass
   `chunks={"lat": 675, "lon": 2700}` (or similar) at open time.**

4. **numcodecs 0.16+ broke Blosc Zarr reads under numcodecs 0.15.1.**
   The error is `cannot import name cbuffer_sizes from numcodecs.blosc`. The
   production Pipfile pins 0.15.1. The dev2026 venv uses 0.16+ because cp313
   has no 0.15.x aarch64 wheels — and that's fine: dev2026 is write-only,
   production reads with 0.15.1. Don't unify these without testing.

5. **`pygeos` is dead** (last release 0.14, no cp312/3.13 wheels). This branch
   has already ported `src/polyhandler.py` to native Shapely 2 APIs, so
   production no longer depends on `pygeos`. Keep it that way: do not
   reintroduce `pygeos` in production dependencies. `polars` remains in the
   data path and is not considered a blocker — the reviewer confirmed it
   installs cleanly in dev2026's `uv sync` envs on macOS/Linux.

6. **Production Polars wheel must be chosen per VM CPU capability.**
   On older x86-64 hosts, the normal `polars` wheel warns about missing CPU
   features (`avx2`, `bmi1`, `bmi2`, `lzcnt`) and may crash. The supported
   deployment pattern is:
   * run root `uv sync` first (base env does **not** include polars)
   * then run `./scripts/install_polars_variant.sh auto`
   * the helper installs `polars==1.26.0` on modern CPUs, otherwise
     `polars-lts-cpu==1.26.0`
   Do **not** install both in one venv; they expose the same `polars` module
   and conflict. If a VM needs an override, use `POLARS_PACKAGE=polars` or
   `POLARS_PACKAGE=polars-lts-cpu` explicitly.

7. **`config.ds` is a module-level global** mutated by `lifespan`. Any unit
   test that bypasses FastAPI must set `src.config.ds`, `.arc`, `.basex`,
   `.basey` manually. See `dev2026/scripts/verify_api_vs_netcdf.py` for the
   minimal setup recipe.

8. **`mode=point` still calls `zdata_bbox()`** which slices a single bbox
   covering all input points. Querying many globally-scattered points in one
   call can trigger a near-full-grid materialise → OOM. For verification
   workloads, prefer single-point queries in a loop (see the same script).

9. **Cowork desktop sandbox specific** (only relevant if you're running there):
   the virtiofs mount disallows `unlink` and `rmdir` on user files. `zarr`'s
   `LocalStore` and `xarray`'s `to_zarr(mode="w")` both internally call
   `shutil.rmtree`, which fails. `dev2026/scripts/convert_to_zarr.py` and
   `convert_incremental.py` install a compatibility shim
   (`os.unlink`/`os.rmdir`/`shutil.rmtree` → no-op on `PermissionError`).
   Final cleanup of leftover `.partial` files happens on the user's Mac side.

10. **Data paths in `gebco_app.py` are anchored to `__file__`, not cwd.**
   Production gunicorn/pm2 always launches from repo root so a bare
   `"data/GEBCO_2026_sub_ice_topo.zarr"` would have worked; but `dev2026`
   verification scripts run from inside `dev2026/` and need
   `_APP_ROOT / "data" / "..."`. Don't revert to a bare relative path. If you
   add new on-disk artefacts (e.g. logs, caches), anchor those to `__file__`
   too for the same reason.

11. **`data/GEBCO_2026_sub_ice_topo.zlib_quarantine.zarr` may still exist on
    disk** as a leftover from the rename-swap that promoted Blosc to canonical
    inside the cowork sandbox (which forbids `rm`). It's safe to ignore — the
    app only reads the canonical name — but you can `rm -rf` it on a normal
    filesystem to reclaim ~3.8 GB.

## How to add a new GEBCO release (recipe)

Used for 2026; reuse for 2027+.

1. Branch from `main`: `git checkout -b gebco_YYYY_api`.
2. Download `GEBCO_YYYY_sub_ice.zip` from CEDA → unzip to
   `data_src/GEBCO_YYYY/GEBCO_YYYY_sub_ice.nc`.
3. In `dev2026/` (or copy to `devYYYY/`), update the source/dest paths in
   `scripts/convert_incremental.py` invocations.
4. Convert with `--codec blosc` (not zlib — see gotcha 1):
   `uv run python scripts/convert_incremental.py <src.nc> <dst.zarr> --codec blosc`
5. Verify:
   * `verify_against_netcdf.py` — Zarr byte-for-byte equals NetCDF (≥100k pts).
   * `verify_api_vs_netcdf.py` — `zprofile()` returns the same z values.
   * `compare_old_new_api.py` — distribution of |Δ| vs previous year. Set
     `--pass-threshold` based on expected geophysical change.
   * `benchmark_old_new_api.py` — new API median latency ≤ old × 1.1.
   * `verify_polygon_meridian.py` — polygon MASK consistency (full-res) +
     cross-meridian line breakpoint regression.
   * `verify_polyhandler_endpoint.py` — real `polyhandler()` with the
     production `sample=5` default. Requires polars in the dev2026 venv
     (`uv sync` handles it on macOS/Linux).
6. Update `gebco_app.py`: `lifespan` path, OpenAPI description (new DOI), and
   the endpoint summary string. Three string edits.
7. Bump version in `change_log.md`.
8. Re-record verification numbers in `dev2026/TESTING.md` (or
   `devYYYY/TESTING.md`).
9. On Mac: clean leftover `.partial` files, rename `_blosc.zarr` → canonical
   name if you went through the sandbox flow.

## API contract (don't break these silently)

* `/gebco?lon=L1,L2,…&lat=L1,L2,…[&mode=…][&sample=N][&jsonsrc=…]`
* Default response keys: `longitude`, `latitude`, `z`, `distance`.
  `z` is int (metres, negative = depth). `distance` is float (km).
* `mode=point` returns only the input endpoints (no interpolation).
* `mode=row` returns records instead of column-per-key.
* `mode=zonly` drops `distance`.
* `mode=lon360` reports longitude in [0, 360].
* `jsonsrc` accepts a URL or inline JSON of a `Polygon` / `FeatureCollection`.
* `decode_cf=False` everywhere on read — z is raw int16 from the grid. Don't
  let xarray rescale or fillna; the production code assumes raw integers.

## Where to ask "what did the last agent do?"

* `change_log.md` for one-line summaries of each release.
* `dev2026/TESTING.md` for the most recent verification run (timestamps,
  exact commands, key numbers). Reproduce by running the same commands.
* `dev2026/README.md` for tooling layout and how-to.
* Git log on `gebco_YYYY_api` branches for fine-grained history.
