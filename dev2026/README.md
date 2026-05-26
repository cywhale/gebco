# dev2026/ — GEBCO_2026 conversion + verification tooling

Offline tooling used to upgrade the production API from `GEBCO_2023_sub_ice_topo.zarr`
to `GEBCO_2026_sub_ice_topo.zarr`. Lives in its own `uv` venv so it doesn't
touch the production Pipenv (`../Pipfile`).

For the latest verification run with reproducible commands, see
[`TESTING.md`](./TESTING.md). For project-wide handover info aimed at AI
agents, see [`../AGENTS.md`](../AGENTS.md).

## Layout

```
dev2026/
├── pyproject.toml                 # uv project: Python 3.13, zarr<3, …
├── main.py                        # tiny entrypoint listing scripts
├── scripts/
│   ├── compare_structure.py       # diff GEBCO_2023.nc vs GEBCO_2026.nc
│   ├── convert_to_zarr.py         # one-shot NetCDF → Zarr (full grid)
│   ├── convert_incremental.py     # chunk-row incremental conversion (use this
│   │                              #   when each invocation has a wall-clock limit,
│   │                              #   e.g. inside the cowork sandbox)
│   ├── verify_zarr.py             # interactive sanity check of the Zarr
│   ├── verify_against_netcdf.py   # Zarr ↔ NetCDF byte-equal (used at 100k pts)
│   ├── verify_api_vs_netcdf.py    # gebco_app code path ↔ NetCDF byte-equal
│   ├── compare_old_new_api.py     # 2023 API vs 2026 API value diff distribution
│   └── benchmark_old_new_api.py   # 2023 API vs 2026 API per-point latency
│   ├── verify_polygon_meridian.py # polygon-MASK consistency + cross-meridian
│   │                              #   line tests (full resolution, no sample=5;
│   │                              #   polars-free, runnable in lean envs)
│   └── verify_polyhandler_endpoint.py
│                                  # polygon ENDPOINT regression — calls the
│                                  #   real src.polyhandler.polyhandler() with
│                                  #   sample=5
└── tests/
    └── test_zarr.py               # pytest sanity checks (skip if no Zarr yet)
```

## Expected data layout (in the repo root)

```
../data_src/
├── GEBCO_2023/
│   └── GEBCO_2023.nc                  # optional, only for schema diff
└── GEBCO_2026/
    └── GEBCO_2026_sub_ice.nc          # ← source for the conversion
../data/
├── GEBCO_2023_sub_ice_topo.zarr/      # current production Zarr (left untouched)
└── GEBCO_2026_sub_ice_topo.zarr/      # ← produced here; Blosc/lz4 clevel=5
```

## Setup

```bash
cd dev2026
uv sync --extra dev          # creates .venv with Python 3.13 + all deps
```

If Python 3.13 isn't on the machine, `uv` will fetch it (`uv python install 3.13`).

## Workflow

### 1. Optional — sanity-diff NetCDF structure

```bash
# Use whichever 2026 NetCDF you have to hand. The schema is identical between
# the two variants so cross-variant diffs are valid for structural checks.
uv run python scripts/compare_structure.py \
    ../data_src/GEBCO_2023/GEBCO_2023.nc \
    ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc
```

Exits 0 if dims / coord names / data_var names / `elevation` dtype all match.
For 2023→2026 the schema is identical; you can skip this on minor releases.

### 2. Convert NetCDF → Zarr

Pick **one** of the two converters:

* **One-shot** (10–30 min on M1, requires unrestricted filesystem):

    ```bash
    uv run python scripts/convert_to_zarr.py \
        ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc \
        ../data/GEBCO_2026_sub_ice_topo.zarr
    ```

* **Incremental** (split into ≤ 40 s passes; survives sandbox time limits;
  always picks up where the previous run left off):

    ```bash
    while ! uv run python scripts/convert_incremental.py \
        ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc \
        ../data/GEBCO_2026_sub_ice_topo.zarr \
        --codec blosc --budget-seconds 38 ; do : ; done
    ```

  The `while` loop is only needed if your shell kills long-running jobs;
  otherwise the script returns 0 immediately when there's no work left.

> **Codec default.** ``convert_to_zarr.py`` defaults to ``--codec blosc``
> (matches the 2023 production Zarr → API read-speed parity). The
> ``convert_incremental.py`` default is still ``zlib`` for back-compat with
> the original 2022→2023 notebook; **always pass `--codec blosc` to it.** See
> `TESTING.md` Step 4 for the 2.66× slowdown measurement that motivated this.

Defaults match the 2023 production layout:
* chunks `{lat: 675, lon: 2700}` (smaller hits the 2 GiB-per-codec ceiling)
* `Blosc(cname="lz4", clevel=5, shuffle=1)` when `--codec blosc`
* `consolidated=True`, no Zarr group, `decode_cf=False`, zarr v2 layout

Output Zarr name: **`GEBCO_2026_sub_ice_topo.zarr`** (canonical, matches
`gebco_app.py:lifespan`). Don't shorten to `GEBCO_2026_sub_ice.zarr` — the
production API and the pytest defaults both look for the `_topo` suffix.

### 3. Verify the Zarr against the source NetCDF

```bash
# Quick interactive overview
uv run python scripts/verify_zarr.py \
    ../data/GEBCO_2026_sub_ice_topo.zarr \
    --reference ../data/GEBCO_2023_sub_ice_topo.zarr

# Strict byte-for-byte equality — 100k pts across 10 random rounds
uv run python scripts/verify_against_netcdf.py \
    ../data/GEBCO_2026_sub_ice_topo.zarr \
    ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc \
    --rounds 10 --points-per-round 10000 --window 10 --tolerance 0

# pytest sanity checks
uv run pytest
```

`verify_against_netcdf.py` reports per-round `max|Δ|` and only prints mismatch
details when something goes wrong — safe to run with large N.

### 4. Verify the API code path against the NetCDF

```bash
uv run python scripts/verify_api_vs_netcdf.py \
    --zarr-path ../data/GEBCO_2026_sub_ice_topo.zarr \
    --nc-path   ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc \
    --total-points 1000 --batch 200 --seed 2026
```

Exercises `src.zprofile.zprofile()` directly (the same function FastAPI calls)
and asserts every returned z matches the NetCDF cell. Single-point queries are
issued in a loop on purpose — see `AGENTS.md` gotcha #7.

### 5. Compare values 2023 vs 2026 (geophysical sanity)

Default uniform-global sampling — fine for an unbiased per-point picture:

```bash
uv run python scripts/compare_old_new_api.py \
    --total-points 1000 --seed 2026 \
    --pass-percentile 95 --pass-threshold 200
```

**Use `--strata risk`** to deliberately oversample Greenland and Antarctica
(25 % / 25 % / 50 % global) — uniform global sampling only hits ≈ 6 % ice-
sheet cells, too few to draw conclusions about BedMachine-updated regions.
The script prints a per-stratum breakdown when stratified:

```bash
uv run python scripts/compare_old_new_api.py \
    --total-points 1000 --seed 2026 --strata risk \
    --pass-percentile 95 --pass-threshold 200 --show-tail 15
```

Both modes print `|Δ|` percentiles (P50/P75/P90/P95/P99/max), counts within
several threshold bands, and the top outliers with coordinates. Pass criterion
is configurable.

### 6. Benchmark latency (2023 vs 2026)

```bash
uv run python scripts/benchmark_old_new_api.py \
    --zarr-old ../data/GEBCO_2023_sub_ice_topo.zarr \
    --zarr-new ../data/GEBCO_2026_sub_ice_topo.zarr \
    --n 400 --warmup 20 --tolerance 0.10
```

Warms up each store, then measures `--n` random queries with the OLD/NEW order
randomised per iteration. Pass criterion: NEW median latency ≤ OLD × (1+tol).

### 7. Polygon mode + cross-meridian lines (ancillary features)

Polygon mode (`jsonsrc`) and the cross-meridian polyline path are covered
by two complementary scripts:

```bash
# (a) Polygon MASK consistency at full resolution + cross-meridian lines.
#     Lean dependencies (shapely + xarray only) — runs anywhere.
uv run python scripts/verify_polygon_meridian.py \
    --pass-percentile 95 --pass-threshold 200

# (b) Polygon ENDPOINT regression with the production sample=5 default.
#     Imports the REAL src.polyhandler.polyhandler(); requires polars
#     (installed by `uv sync`).
uv run python scripts/verify_polyhandler_endpoint.py \
    --sample 5 --pass-percentile 95 --pass-threshold 200
```

(a) covers six fixed test cases (T1–T6):

* Polygon mode (`jsonsrc` GeoJSON, including FeatureCollection and a polygon
  that crosses the 180° meridian) — exercises the data flow that
  `src/polyhandler.py` implements. The script bypasses polyhandler itself
  (it imports polars; keeping the script polars-free makes it runnable in
  lean / sandboxed envs) and re-implements the data path with shapely 2.x.
  It runs at FULL grid resolution, so row counts here are ~25× larger than
  the public endpoint's at `sample=5`. The cell mask depends only on the
  grid + polygon, so this is a useful geophysical consistency test, but
  NOT an end-to-end endpoint regression.
* Cross-meridian polyline (`/gebco?lon=A,B,C&lat=...`) — calls
  `src.zprofile.zprofile()` directly, which is the exact function the
  FastAPI endpoint dispatches to. Exercises `src.xmeridian.crossBoundary`'s
  break-point insertion at 0° and 180°.

(b) is the public-endpoint regression. It calls the real polyhandler with
`poly_sample=5` — the same call `gebco_app.py:208-210` makes when a request
hits `/gebco?jsonsrc=...` — for T1 (Taiwan) and T4 (Fiji crossing 180°), so
reviewers can cross-reference mask-level and endpoint-level numbers.

For each case both scripts assert identical row count and identical cell
coordinates between 2023 and 2026 (the grid is unchanged), then report the
z-value `|Δ|` distribution and pass/fail at a configurable percentile/threshold.
See `TESTING.md` Phase C for the latest numbers — for v0.5.0, both layers
pass: mask-level T1 P95=154 m / T4 P95=196 m, endpoint-level (`sample=5`)
T1 2304 rows P95=155 m / T4 1152 rows P95=195 m.

> **polars in dev2026.** `polyhandler` requires polars; on a normal `uv sync`
> on macOS / Linux the polars wheel installs fine and (b) runs end-to-end.
> An earlier session reported a wrapper/binary mismatch inside a restricted
> sandbox; that is a sandbox-specific artefact, not a general project
> constraint. If you only have shapely available, (a) still gives you a
> meaningful consistency signal.
>
> **Shapely 2 in production code.** `src/polyhandler.py` now uses native
> Shapely 2 APIs, so polygon endpoint regression in (b) no longer depends on
> a `pygeos` shim. Keep production dependencies aligned with that design:
> `pygeos` should stay out of the runtime env.

## What stays out of scope (intentionally)

* `../Pipfile` / `../requirements.txt` are **not** modified — production
  runtime stays on Python 3.11 + zarr 2.18.6 + Pipenv. The dev2026 venv exists
  so we can use Python 3.13 + uv for writing without touching production.
* The old `../dev/read_gebco_raw01.ipynb` is kept verbatim as the historical
  record of the 2022→2023 upgrade. New releases get a new directory (this
  one), not edits to the old one.
* `../README.md` is end-user facing and only gets touched once the release
  ships.

## Production deployment notes (v0.5.1 staging plan)

The production deployment path is now:

```bash
cd ..
uv sync --python 3.11
./scripts/install_polars_variant.sh auto
```

Why the second step exists:

* `src/polyhandler.py` uses `polars` only in the polygon endpoint path.
* On older x86-64 CPUs, the normal `polars` wheel can warn about missing CPU
  features (`avx2`, `bmi1`, `bmi2`, `lzcnt`) and may be unsafe to run.
* `polars-lts-cpu` is the compatible fallback, but it cannot coexist with
  `polars` in the same venv because both provide the same `polars` module.

So the supported deployment pattern is:

1. Build the base root `.venv` with `uv sync`.
2. Run `./scripts/install_polars_variant.sh auto`.
3. Let that helper choose:
   * `polars==1.26.0` on modern CPUs
   * `polars-lts-cpu==1.26.0` on older CPUs

You can override the selection explicitly if needed:

```bash
POLARS_PACKAGE=polars ./scripts/install_polars_variant.sh
POLARS_PACKAGE=polars-lts-cpu ./scripts/install_polars_variant.sh
POLARS_VERSION=1.26.0 ./scripts/install_polars_variant.sh auto
```

### VM37 no-downtime staging recipe

To avoid touching the live `pm2` process (`gebco` on `127.0.0.1:8013`),
stage the upgrade in a separate checkout and run a loopback-only test port:

```bash
cd ~/python/gebco
git clone --branch gebco_2026_api --single-branch \
  https://github.com/cywhale/gebco.git .stage_v051

cd .stage_v051
uv sync --python 3.11
./scripts/install_polars_variant.sh auto

# copy or rsync the canonical Zarr into ./data first:
#   data/GEBCO_2026_sub_ice_topo.zarr

./.venv/bin/gunicorn gebco_app:app \
  -w 1 \
  -k uvicorn.workers.UvicornWorker \
  -b 127.0.0.1:18013
```

Then smoke-test locally on the VM, for example:

```bash
python - <<'PY'
import json, requests
base = "http://127.0.0.1:18013/gebco"
print(requests.get(base, params={"lon":"122.36","lat":"25.02","mode":"point"}).json())
print(requests.get(base, params={"lon":"179.5,-179.5","lat":"-17.25,-17.25","mode":"zonly"}).json()["z"][:3])
poly = {"type":"Polygon","coordinates":[[[121.5,23.0],[121.5,24.0],[122.5,24.0],[122.5,23.0],[121.5,23.0]]]}
print(len(requests.get(base, params={"jsonsrc": json.dumps(poly), "mode":"zonly"}).json()["longitude"]))
PY
```

VM37 staging was verified this way on `2026-05-26` without restarting `pm2`
or touching `127.0.0.1:8013`.
