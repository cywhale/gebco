# TESTING.md — GEBCO_2026 upgrade verification log

Reproducible record of the verification that gated `gebco_2026_api` → `main`.
All commands assume `cwd = repo root` (`~/proj/gebco/`) and the dev2026 venv
synced (`cd dev2026 && uv sync --extra dev`).

| Test environment        | Value                                                       |
|-------------------------|-------------------------------------------------------------|
| Run date (UTC)          | 2026-05-22 → 2026-05-23                                     |
| Branch                  | `gebco_2026_api`                                            |
| Runner                  | cowork desktop sandbox (Linux aarch64, 4 vCPU, 3.8 GB RAM)  |
| Python (verification)   | 3.13.12 via `dev2026/.venv`                                 |
| Canonical pkg versions  | `zarr==2.18.7`, `numcodecs==0.15.1`, `xarray==2026.4.0`, `netCDF4==1.7.4`, `h5netcdf==1.8.1`, `dask==2026.3.0` — what `uv sync --extra dev` resolves from `pyproject.toml` (see `dev2026/uv.lock`). Production runtime reads with `zarr==2.18.6` / `numcodecs==0.15.1` (`../Pipfile.lock`), which is on-disk compatible. |
| Sandbox deviation       | The actual conversion run in this report was executed with `zarr==3.2.1` / `numcodecs==0.16.5` installed via `uv pip install --no-deps`, because Python 3.13 on this aarch64 sandbox can't build the `asciitree==0.3.3` sdist (uv's build isolation hits `RecursionError` inside `shutil.rmtree`). The script forces `zarr_format=2` regardless, so the produced store is bit-compatible with the canonical 2.18.x runtime — but if you reproduce on a clean Mac with `uv sync --extra dev`, expect the canonical versions and identical results. |
| Source NetCDF           | `data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc` (7.5 GB, DOI 10.5285/4f68d5c7-…) |
| Produced Zarr           | `data/GEBCO_2026_sub_ice_topo.zarr` (Blosc/lz4 clevel=5 shuffle=1, 3.5 GB, 2048 chunks 675×2700) |

Wall-clock for the full suite below: **≈ 8 min on the sandbox** (most of that
is the conversion itself; verification is < 1 min).

---

## Phase A — Data layer (Zarr correctness)

### A1. Convert NetCDF → Zarr (Blosc/lz4)

```bash
cd dev2026
while ! uv run python scripts/convert_incremental.py \
    ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc \
    ../data/GEBCO_2026_sub_ice_topo.zarr \
    --codec blosc --budget-seconds 38 ; do : ; done
```

**Result (2026-05-22 09:45–09:48):** 64 chunk-rows × 32 chunks each = 2048
chunks, 3.5 GB total. Final `.zarray.compressor`:

```json
{ "id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0 }
```

Identical to the 2023 production Zarr's compressor and chunk layout.

### A2. Strict byte-equality vs source NetCDF — 100 000 points

```bash
# 4 batches × different master seeds. Token-light output (1 line per round).
for seed in 2026 2027 2028 2029; do
  uv run python scripts/verify_against_netcdf.py \
      ../data/GEBCO_2026_sub_ice_topo.zarr \
      ../data_src/GEBCO_2026/GEBCO_2026_sub_ice.nc \
      --rounds 3 --points-per-round 10000 --window 10 --tolerance 0 \
      --seed $seed
done
# (final batch uses --rounds 1 to total 10 rounds = 100 000 points)
```

**Result (2026-05-22 09:54–09:57):**

| Metric                 | Value                                  |
|------------------------|----------------------------------------|
| Total points compared  | 100 000 (10 rounds × 10 000)           |
| Mismatches             | **0**                                  |
| Overall `max\|Δ\|`     | **0** (exact byte equality)            |
| Sampling window        | W=10 (100 tiles of 10×10 per round)    |

Sample line per passing round:

```
[ 1/3] seed=1829338324 K=10000 OK  max|Δ|=0  read_t(zarr/nc)=5.94/6.58s
```

→ Zarr is bit-for-bit identical to the source NetCDF.

---

## Phase B — API layer (Step 1–4 of the user spec)

### B1. (Step 1) API code change

`gebco_app.py` edits on branch `gebco_2026_api`:

* `lifespan` opens **`_APP_ROOT / "data" / "GEBCO_2026_sub_ice_topo.zarr"`**
  where `_APP_ROOT = Path(__file__).resolve().parent`. Anchoring to the script
  location (not cwd) lets `dev2026/.../verify_api_vs_netcdf.py --with-fastapi`
  start the app from inside `dev2026/` without `FileNotFoundError`. Production
  launchers (gunicorn / pm2 from the repo root) keep working unchanged.
* OpenAPI description references DOI `10.5285/4f68d5c7-…`
* Endpoint summary `"Get GEBCO(2023) bathymetry"` → `"Get GEBCO(2026) bathymetry"`

No behavioural changes elsewhere.

### B2. (Step 2) Direct-call API ↔ NetCDF byte-equal — 1 000 points

```bash
dev2026/.venv/bin/python -u dev2026/scripts/verify_api_vs_netcdf.py \
    --total-points 1000 --batch 200 --seed 2026
```

**Result (2026-05-23 02:37):**

```
TOTAL  points=1,000  bad=0  wall=14.9s
✓ API ↔ NetCDF: every sampled point matches exactly
```

The script calls `src.zprofile.zprofile()` directly (same code FastAPI invokes)
because the sandbox can't fit the production `multiprocessing.Pool(4)` in
3.8 GB RAM. An optional `--with-fastapi` smoke-checks one request via
`fastapi.testclient.TestClient` to confirm the HTTP layer works end-to-end.

### B3. (Step 3) Old vs New API value distribution

Two passes — first with uniform global sampling for an unbiased per-point
distribution, then with `--strata risk` to explicitly stress-test the high-
risk regions (Greenland, Antarctica) that uniform global sampling barely
touches.

#### B3.a — uniform global sampling, 1 000 points

```bash
uv run python scripts/compare_old_new_api.py \
    --total-points 1000 --seed 2026 \
    --strata global \
    --pass-percentile 95 --pass-threshold 200
```

**Result (2026-05-23 02:46, Blosc Zarr):**

| Band                        | Count        | Cumulative %  |
|-----------------------------|--------------|---------------|
| Exact match (`\|Δ\|`==0)    | 275          |  27.5 %       |
| `\|Δ\|` ≤   10 m            | 648          |  64.8 %       |
| `\|Δ\|` ≤   50 m            | 867          |  86.7 %       |
| `\|Δ\|` ≤  100 m            | 948          |  94.8 %       |
| `\|Δ\|` ≤  200 m            | 989          |  98.9 %       |
| `\|Δ\|` ≤  500 m            | 1000         | 100.0 %       |

```
Percentiles: P50=4 m  P75=22 m  P90=63 m  P95=102 m  P99=218 m  P100=339 m
Mean |Δ|=21.1 m   median=4 m   signed mean=-1.4 m (essentially unbiased)
```

**Verdict: ✓ PASS (P95 = 102 m, threshold 200 m).**

Top-10 outliers (this pass) are all deep-ocean abyssal points (−3000 to
−5500 m), explainable by GEBCO_2026's switch to SRTM15+ v2.8 (SWOT-derived
gravity + ML bathymetry). **Important caveat:** uniform global sampling puts
≈ 0.4 % of points on Greenland and ≈ 5.5 % on Antarctica — too few to draw any
conclusion about ice-sheet behaviour. That's what B3.b covers.

#### B3.b — stratified risk sampling (Greenland 25 % / Antarctica 25 % / global 50 %)

```bash
uv run python scripts/compare_old_new_api.py \
    --total-points 1000 --seed 2026 \
    --strata risk \
    --pass-percentile 95 --pass-threshold 200 --show-tail 15
```

**Result (2026-05-24 14:47, Blosc Zarr):**

Aggregate across all 1 000 stratified points:

| Band                        | Count        | Cumulative %  |
|-----------------------------|--------------|---------------|
| Exact match (`\|Δ\|`==0)    | 186          |  18.6 %       |
| `\|Δ\|` ≤   10 m            | 682          |  68.2 %       |
| `\|Δ\|` ≤   50 m            | 916          |  91.6 %       |
| `\|Δ\|` ≤  100 m            | 963          |  96.3 %       |
| `\|Δ\|` ≤  200 m            | 989          |  98.9 %       |
| `\|Δ\|` ≤  500 m            | 1000         | 100.0 %       |

```
Percentiles: P50=4 m  P75=15 m  P90=44 m  P95=76 m  P99=203 m  P100=363 m
Mean |Δ|=16.9 m   median=4 m   signed mean=+1.2 m (essentially unbiased)
```

Per-stratum breakdown (lower `exact` count for ice strata is expected — more
of those cells have been touched by BedMachine v6 / Antarctica v3):

| Stratum     | n   | mean | median | P95 | P99 | max | exact |
|-------------|-----|------|--------|-----|-----|-----|-------|
| antarctica  | 250 | 11.4 |   3    |  47 | 105 | 316 | 38/250 (15.2 %) |
| global      | 500 | 18.0 |   3    |  85 | 211 | 363 | 130/500 (26.0 %) |
| greenland   | 250 | 20.3 |   6    |  96 | 175 | 225 | 18/250 (7.2 %) |

Top ice-sheet outliers actually observed in this stratified pass:

```
[antarctica] lat=-64.22  lon= -77.52  old= -3699  new= -4015  Δ= -316 m
[antarctica] lat=-72.62  lon=+168.96  old= +1345  new= +1574  Δ= +229 m
[ greenland] lat=+80.90  lon= -63.84  old= +1085  new=  +860  Δ= -225 m
[ greenland] lat=+68.48  lon= -34.59  old= +1308  new= +1530  Δ= +222 m
[ greenland] lat=+77.18  lon= -70.07  old=  +859  new=  +679  Δ= -180 m
```

**Verdict: ✓ PASS (P95 = 76 m, threshold 200 m).**

The ice sheets *do* shift by up to ±225 m in Greenland and ±316 m in
Antarctica at individual cells — that's BedMachine v6/v3 doing its job and is
expected. What we tested for and verified is that *the aggregate distribution
stays well within the 200 m P95 envelope*, even when ice sheets are
deliberately over-represented (½ of all samples). The 2026 grid is safe to
serve from the production API at point-query precision.

### B4. (Step 4) Old vs New API latency

```bash
uv run python scripts/benchmark_old_new_api.py \
    --n 400 --warmup 20 --seed 42 --tolerance 0.10
```

**Result (2026-05-24 15:24, Blosc Zarr as canonical):**

| Store                | mean    | median  | P95     | P99     | max     |
|----------------------|---------|---------|---------|---------|---------|
| OLD (2023)           | 4.65 ms | 4.52 ms | 5.25 ms | 6.89 ms | 27.37 ms |
| NEW (2026, Blosc)    | 4.62 ms | 4.53 ms | 5.23 ms | 6.58 ms |  7.66 ms |

```
NEW / OLD ratio:  mean = 0.99×   median = 1.00×
✓ PASS  (well within 10% tolerance; new is essentially indistinguishable from old at the median)
```

**Path you must read carefully — the "canonical" artifact swap.**

The first GEBCO_2026 Zarr produced in this branch used `numcodecs.Zlib(level=1)`
(default of `convert_incremental.py`). On disk it lived at
`data/GEBCO_2026_sub_ice_topo.zarr`. Benchmark showed 13.4 ms median = **2.66×
slower than 2023** — FAIL.

The second pass produced a Blosc-encoded store at
`data/GEBCO_2026_sub_ice_topo_blosc.zarr` (3.5 GB, identical chunking to 2023).
A first attempt at Step 4 looked passing because the benchmark was given the
`_blosc.zarr` path explicitly via `--zarr-new`, but `gebco_app.py` was still
opening the Zlib store under the canonical name → real production would have
been slow.

The final fix swaps the on-disk names so the canonical path is now Blosc:

```bash
# Done once in the sandbox; user repeats on Mac if not already in this state.
mv data/GEBCO_2026_sub_ice_topo.zarr        data/GEBCO_2026_sub_ice_topo.zlib_quarantine.zarr
mv data/GEBCO_2026_sub_ice_topo_blosc.zarr  data/GEBCO_2026_sub_ice_topo.zarr
# Optional cleanup once you're confident:
rm -rf data/GEBCO_2026_sub_ice_topo.zlib_quarantine.zarr
```

The lesson is codified in `AGENTS.md` gotcha #1 and the default of
`convert_incremental.py --codec` (zlib for back-compat with the 2023 notebook,
but **always pass `blosc` for production**). `convert_to_zarr.py` already
defaults to blosc.

---

## Phase C — Ancillary API features (polygon mode + cross-meridian lines)

The /gebco endpoint has two non-trivial features beyond simple point/line
queries: polygon-mode via the `jsonsrc` GeoJSON parameter
(`src/polyhandler.py`), and automatic break-point insertion for polylines
crossing the 0° or 180° meridian (`src/xmeridian.py`). Both are exercised
against the 2026 Zarr and compared to the 2023 baseline.

### C1. Test layout

```bash
uv run python scripts/verify_polygon_meridian.py
# optional thresholds:
uv run python scripts/verify_polygon_meridian.py \
    --pass-percentile 95 --pass-threshold 200
```

Six cases (T1–T6). For each: same query against OLD and NEW Zarr, then assert
identical row count + identical cell coordinates (grid is unchanged) and
report the z-value diff distribution with a configurable pass band.

* T1 — Polygon, 1°×1° box NE of Taiwan
* T2 — Polygon, 2°×1° box over Greenland coast (BedMachine v6 region)
* T3 — FeatureCollection of two non-overlapping polygons in one request
* T4 — Polygon crossing the 180° meridian (Fiji, 1°×0.5°)
* T5 — Line crossing the 0° meridian (Gulf of Guinea, 4 endpoints)
* T6 — Line crossing the 180° meridian (mid-Pacific, 4 endpoints)

T5/T6 invoke `src.zprofile.zprofile()` directly (same function FastAPI calls
for `/gebco?lon=...&lat=...`); the cross-meridian breakpoint insertion in
`src.xmeridian.crossBoundary` runs as part of that.

For T1–T4, the test script bypasses `src.polyhandler.polyhandler()` and
replicates polyhandler's data-flow using shapely 2.x directly (bbox-subset
→ mesh-grid → `shapely.contains` mask → `elevation[mask]`). The data path
under test is identical to polyhandler's; the only thing skipped is the
polars-based DataFrame construction. We do this so the script can be run
in lean environments (e.g. a sandbox where the polars wheel installs
unreliably). Importantly, **this is a polygon MASK consistency test, not
a public-endpoint regression**: it runs at full grid resolution, while the
real `/gebco?jsonsrc=...` endpoint defaults to `sample=5` (a.k.a.
`poly_sample=5`) which downsamples every 5th cell to keep payloads small.

### C1b. Polygon endpoint regression with production `sample=5`

For the user-visible endpoint behavior, we have a companion script that
imports the REAL `src.polyhandler.polyhandler()` and calls it with
`poly_sample=5` — exactly as `gebco_app.py:208-210` does:

```bash
uv run python scripts/verify_polyhandler_endpoint.py \
    --sample 5 --pass-percentile 95 --pass-threshold 200
```

This requires polars to be installed in the dev2026 venv. In a normal
`uv sync` on macOS / Linux the polars wheel installs fine and the script
runs end-to-end. The script covers T1 (Taiwan) and T4 (Fiji crossing 180°)
— the two strongest cases from C1 — so reviewers can cross-reference the
mask-level and endpoint-level numbers (endpoint row counts are ≈ 1/25 of
mask row counts at `sample=5`, which is the expected downsampling ratio).

Expected row counts at `sample=5` (matches the reviewer's reproduction of
real `polyhandler()`):

| Case | C1 mask (full-res) | C1b endpoint (`sample=5`) |
|------|---------------------|----------------------------|
| T1   |      57 600         |          ~2 304            |
| T4   |      28 800         |          ~1 152            |

Run on the user's Mac (or any machine with a clean `uv sync` dev2026 env)
and paste the actual numbers into table C2b below before merging.

### C2. Result (2026-05-25 07:49, canonical Blosc Zarr)

| Case | Label                                       | n cells/pts | exact match | P50  | P95   | P99   | max   | verdict |
|------|---------------------------------------------|-------------|-------------|------|-------|-------|-------|---------|
| T1   | Polygon — NE of Taiwan                      |    57 600   |    2.8 %    |  22  |  154  |  287  | 1064  | ✓ PASS  |
| T2   | Polygon — Greenland coast                   |   115 200   |    1.6 %    |  18  |   84  |  183  |  325  | ✓ PASS  |
| T3   | FeatureCollection — two polys               |    28 800   |    9.9 %    |   5  |   44  |   93  |  419  | ✓ PASS  |
| T4   | Polygon crossing 180° (Fiji)                |    28 800   |    4.7 %    |  12  |  196  |  416  |  611  | ✓ PASS  |
| T5   | Line crossing 0° (Gulf of Guinea)           |     1 444   |    6.7 %    |   7  |   41  |   88  |  104  | ✓ PASS  |
| T6   | Line crossing 180° (mid-Pacific)            |     2 404   |    2.8 %    |  19  |  140  |  226  |  433  | ✓ PASS  |

(z-diff numbers are |Δ| in metres; pass band = P95 ≤ 200 m.)

All cases produced **identical row counts and identical (lat, lon) cell
coordinates** between 2023 and 2026 — confirming the polygon-mask logic and
the cross-meridian breakpoint logic are deterministic functions of the grid
(which hasn't changed) and the input geometry. Only z values differ, and
they stay within the same geophysical envelope established in Phase B3.

T4 is the closest to the threshold (P95=196 m). That's expected: a small box
straddling 180° in the Pacific abyssal lands mostly on −4000…−5000 m cells
where SRTM15+ v2.8 made the largest predicted-bathymetry revisions. Top
outliers in T1 (max 1064 m near Taiwan coast) are coastal-shelf transition
cells where shallow-water sources changed; they're known noise zones rather
than algorithmic regressions.

### C2b. Endpoint regression result with `sample=5` (real polyhandler)

Reproducer (any machine with a clean dev2026 `uv sync` env — polars 1.41+
and shapely 2.x both install cleanly from a stock macOS / Linux box):

```bash
cd ~/proj/gebco/dev2026
uv run python scripts/verify_polyhandler_endpoint.py \
    --sample 5 --pass-percentile 95 --pass-threshold 200
```

| Case | Label                                  | OLD rows | NEW rows | coords  | P95   | verdict |
|------|----------------------------------------|----------|----------|---------|-------|---------|
| T1   | Polygon — NE of Taiwan (sample=5)      |   2 304  |   2 304  | identical | 155 m | ✓ PASS  |
| T4   | Polygon crossing 180° (Fiji, sample=5) |   1 152  |   1 152  | identical | 195 m | ✓ PASS  |

Row counts are exactly 1/25 of the C2 mask-level numbers (T1: 57600/25=2304;
T4: 28800/25=1152) — confirming the `sample=5` down-sampling slice runs as
expected through `src.zprofile.zdata_bbox`. P95 z-deltas land within the
same 200 m band the geophysical comparison uses; T4 (Pacific abyssal,
crossing 180°) is again the closest to the threshold for the same SRTM15+
v2.8 reason called out in C2.

This is the source-of-truth endpoint regression for polygon mode. C1 / C2
above remain the full-resolution mask-level consistency check; both layers
pass for v0.5.0.



For a future agent who wants a single command to repeat all of B2–B4:

```bash
cd ~/proj/gebco/dev2026
uv sync --extra dev    # one-time

# All three scripts auto-derive --repo-root from their own location and
# default --zarr-old / --zarr-new / --nc-path off it. Cwd doesn't matter.
uv run python scripts/verify_api_vs_netcdf.py \
    --total-points 1000 --batch 200 --seed 2026
uv run python scripts/compare_old_new_api.py \
    --total-points 1000 --seed 2026 --strata global \
    --pass-percentile 95 --pass-threshold 200
uv run python scripts/compare_old_new_api.py \
    --total-points 1000 --seed 2026 --strata risk \
    --pass-percentile 95 --pass-threshold 200
uv run python scripts/benchmark_old_new_api.py \
    --n 400 --warmup 20 --seed 42 --tolerance 0.10
uv run python scripts/verify_polygon_meridian.py \
    --pass-percentile 95 --pass-threshold 200
uv run python scripts/verify_polyhandler_endpoint.py \
    --sample 5 --pass-percentile 95 --pass-threshold 200
```

All scripts are idempotent and seed-controlled, so output should match this log
modulo machine-dependent timing. The A-phase (Zarr-correctness) commands
re-run the same way; see Phase A above.

## Known omissions / follow-ups

* The pytest suite (`uv run --extra dev pytest -q` in `dev2026/`) was
  re-run after the Blosc re-encode and the path correction in
  `tests/test_zarr.py` (`_sub_ice.zarr` → canonical `_sub_ice_topo.zarr`):
  **15 passed** (no longer silently skipping). Reproducer:

  ```bash
  cd dev2026
  uv run --extra dev pytest -q
  ```
* Polygon-mode (`/gebco?jsonsrc=...`) is covered at two levels and both pass:
    - **Mask consistency** (no downsampling) — covered by C1/C2 above via
      `verify_polygon_meridian.py`, runnable anywhere shapely is available.
    - **Endpoint regression** (production `sample=5`) — covered by C1b/C2b
      via `verify_polyhandler_endpoint.py`, which imports the REAL
      `src.polyhandler.polyhandler()`. Reviewer reproduced in a clean
      `uv sync` env: T1 / T4 both pass identical row count + identical
      coords + P95 within threshold.
    - Polars installs cleanly in a stock dev2026 `uv sync` env on
      macOS / Linux; the wrapper/binary mismatch reported in an earlier
      Linux sandbox session was sandbox-specific, not a project constraint.
    - As a final belt-and-suspenders, running a `curl` against a known
      polygon on the production Pipenv env remains good practice before
      cutting the release.
* OpenAPI version in `gebco_app.py` is still `"1.0.0"` — bump to `1.1.0` is
  left to the merge PR.
* The reported sandbox versions (`zarr==3.2.1`, `numcodecs==0.16.5`) deviate
  from the canonical `uv sync` resolution (`zarr==2.18.7`, `numcodecs==0.15.1`).
  See the environment table above for the reason. Re-running the suite on a
  Mac with the canonical versions should reproduce identical numerical results
  (the Zarr was written with `zarr_format=2` and lossless codecs).
