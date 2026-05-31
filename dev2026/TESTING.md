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

## Phase D. VM37 staging benchmark against current production

Purpose: compare the current production API (2023 data source) and the staged
2026 API on the **same VM** without touching the live `pm2` service.

Important detail: the live production bind on VM37 is **HTTPS on loopback**:
`https://127.0.0.1:8013/gebco` (gunicorn started with `--keyfile` /
`--certfile`). If you benchmark it as plain HTTP you will get a misleading
`RemoteDisconnected` failure. The staged app on `127.0.0.1:18013` is plain
HTTP unless you deliberately add TLS flags.

### D1. Reproducer (VM37, non-production port)

On VM37, from the staging checkout:

```bash
cd ~/python/gebco/.stage_v051

# ensure no stale listener is left on 18013
python3 - <<'PY'
import os, re, signal, subprocess
out = subprocess.check_output(["ss", "-ltnp"], text=True)
for line in out.splitlines():
    if ":18013" not in line:
        continue
    for pid in re.findall(r"pid=(\d+)", line):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
PY

./.venv/bin/gunicorn gebco_app:app \
    -w 2 \
    -k uvicorn.workers.UvicornWorker \
    -b 127.0.0.1:18013 >/tmp/gebco_stage18013.out 2>/tmp/gebco_stage18013.err &
GPID=$!
sleep 6

./.venv/bin/python - <<'PY'
import gc, json, statistics, time, urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ~1,016,064 rows with polygon sample=5
poly = {
    "type": "Polygon",
    "coordinates": [[[140.0, 0.0], [140.0, 21.0],
                     [161.0, 21.0], [161.0, 0.0],
                     [140.0, 0.0]]],
}
params = {"jsonsrc": json.dumps(poly), "mode": "zonly"}

prod = "https://127.0.0.1:8013/gebco"   # current production (TLS on loopback)
stage = "http://127.0.0.1:18013/gebco"  # staging checkout

# small correctness smoke (point mode)
pt_params = {"lon": "122.36", "lat": "25.02", "mode": "point"}
pt_old = requests.get(prod, params=pt_params, timeout=60, verify=False).json()
pt_new = requests.get(stage, params=pt_params, timeout=60).json()
print("[POINT] old=", pt_old)
print("[POINT] new=", pt_new)
print("[POINT] exact_match=", pt_old == pt_new)

def run_once(url, verify):
    t0 = time.perf_counter()
    r = requests.get(url, params=params, timeout=300, verify=verify)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    j = r.json()
    rows = len(j["longitude"])
    del j, r
    gc.collect()
    return elapsed, rows

results = {}
for label, url, verify in (("OLD", prod, False), ("NEW", stage, True)):
    warm, rows = run_once(url, verify)
    times = []
    for _ in range(2):
        t, rows2 = run_once(url, verify)
        assert rows2 == rows
        times.append(t)
    results[label] = {"rows": rows, "warmup": warm, "times": times}
    print(
        f"[{label}] rows={rows} warmup={warm:.3f}s "
        f"measured={times[0]:.3f}s,{times[1]:.3f}s "
        f"mean={statistics.mean(times):.3f}s median={statistics.median(times):.3f}s"
    )

old_mean = statistics.mean(results["OLD"]["times"])
new_mean = statistics.mean(results["NEW"]["times"])
print(f"[RATIO] NEW/OLD mean={new_mean / old_mean:.3f}x")
PY

kill $GPID
wait $GPID 2>/dev/null || true
```

### D2. Result (2026-05-26 08:47, VM37 local loopback)

Benchmark target:

* one polygon request
* `mode=zonly`
* approx `1,016,064` returned rows
* production = `https://127.0.0.1:8013/gebco`
* staging = `http://127.0.0.1:18013/gebco`
* both with gunicorn `-w 2`

Small correctness smoke:

| Case | OLD (2023 prod) | NEW (2026 stage) | verdict |
|------|------------------|------------------|---------|
| point `lon=122.36 lat=25.02` | `z=-1140` | `z=-1173` | expected different (different GEBCO release) |

Large payload speed result:

| Target | rows | warmup | measured 1 | measured 2 | mean | median |
|--------|------|--------|------------|------------|------|--------|
| OLD production (`8013`, HTTPS) | 1,016,064 | 1.317 s | 1.452 s | 1.250 s | 1.351 s | 1.351 s |
| NEW staging (`18013`, HTTP)    | 1,016,064 | 1.728 s | 1.396 s | 1.259 s | 1.327 s | 1.327 s |

Derived ratio:

* `NEW / OLD mean = 0.983x`

Interpretation:

* On a ~1M-row polygon payload, the staged 2026 API is effectively at parity
  with current production on the same VM.
* In this run the staged API is ~1.7% faster, which is comfortably within the
  "not slower than production" acceptance goal.
* The point-mode value mismatch is **not** a regression signal here — it is
  expected because OLD serves GEBCO_2023 and NEW serves GEBCO_2026.

### D3. VM37 production cutover result (2026-05-26 13:25 +08)

After the staging benchmark passed, production was switched to the staged
checkout by:

1. updating `conf/ecosystem.config.js` so `pm2` launches gunicorn through
   `/bin/bash -lc` (avoids Node trying to parse the Python entrypoint),
2. refreshing `~/python/gebco/.stage_v051` to the latest `gebco_2026_api`,
3. stopping the orphaned old pyenv/gunicorn listener on `127.0.0.1:8013`,
4. restarting `pm2` app `gebco`, which then bound `8013` from the staged
   checkout and served the GEBCO_2026 Zarr.

Verification after cutover:

| Check | Result |
|------|--------|
| `pm2 describe gebco` | `exec cwd=/home/odbadmin/python/gebco/.stage_v051`, `branch=gebco_2026_api`, `revision=573d8ac...` |
| VM37 loopback | `https://127.0.0.1:8013/gebco?lon=122.36&lat=25.02&mode=point` → `z=-1173` |
| Public endpoint | `https://api.odb.ntu.edu.tw/gebco?lon=122.36&lat=25.02&mode=point` → HTTP 200, `z=-1173` |
| Process persistence | `pm2 save` completed successfully |

This was the first production acceptance point for v0.5.1 on VM37: the new
GEBCO_2026 dataset was serving through the root-`uv` runtime and pm2-managed
gunicorn, but still from the temporary staging checkout.

### D4. VM37 post-merge normalisation (2026-05-27)

After `gebco_2026_api` was merged to `main`, VM37 production was moved from the
temporary staging checkout back to the root repo:

1. `~/python/gebco` was updated to `origin/main`
2. root `.venv` was synced again and `polars-lts-cpu` reinstalled via the
   helper
3. `pm2 gebco` was restarted from `~/python/gebco`
4. `.env` in the repo root was used to publish `API_SERVERS=https://api.odb.ntu.edu.tw`

Verification after the normalisation:

| Check | Result |
|------|--------|
| `pm2 describe gebco` | `exec cwd=/home/odbadmin/python/gebco`, `branch=main`, `revision=c530d01...` |
| VM37 loopback | `https://127.0.0.1:8013/gebco?lon=122.36&lat=25.02&mode=point` → `z=-1173` |
| Public endpoint | `https://api.odb.ntu.edu.tw/gebco?lon=122.36&lat=25.02&mode=point` → HTTP 200, `z=-1173` |
| Public OpenAPI server | `/gebco/openapi.json` contains `https://api.odb.ntu.edu.tw` |

This was the production state after the v0.5.2 merge on `2026-05-27`: VM37
served GEBCO_2026 from the root checkout on `main`, not from `.stage_v051`.
See Phase I below for the later v0.5.5 rollout state on both VMs.



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
* The reported sandbox versions (`zarr==3.2.1`, `numcodecs==0.16.5`) deviate
  from the canonical `uv sync` resolution (`zarr==2.18.7`, `numcodecs==0.15.1`).
  See the environment table above for the reason. Re-running the suite on a
  Mac with the canonical versions should reproduce identical numerical results
  (the Zarr was written with `zarr_format=2` and lossless codecs).

---

## v0.5.3 Hardening — Phase D / E / F

These three phases were added on the v0.5.3 hardening branch and stand
alongside the v0.5.0 phases. They cover bbox sizing, dask-pool ablation,
and the np.append → list speedup (deferred).

### Phase D — bbox sizing probe (informs `MAX_BBOX_CELLS_LINE` / `MAX_POLYGON_CELLS`)

**Reproducer (on Mac, canonical 2026 Zarr present):**

```bash
cd ~/proj/gebco/dev2026
uv sync --group dev   # idempotent
uv run python scripts/probe_bbox_limits.py
```

The script measures wall time + peak RSS delta for both the line/point
materialise path (`ds.sel(...).elevation.values`) and the polygon path
(`meshgrid → shapely.points → shapely.contains` mask + masked
`elevation`) at increasing bbox cell counts.

Mac run completed on the canonical 2026 Zarr. One important observation:
the polygon path crosses the heuristic threshold extremely early. By
`1e7` raw bbox cells it already exceeded both the `2.0 s` and `1200 MB`
polygon limits, and by `5e7` raw cells the probe hit ~10 GB RSS. That
means the heuristic in the spec is much stricter than the current
frontend "1M rows" expectation for thin polygons; treat the numbers
below as a risk signal, not as an automatic instruction to lower the
production cap to `1e7`.

Measured table:

| target raw cells | side (°) | line cells | line s | line RSS MB | poly cells | poly s | poly RSS MB |
|------------------|----------|------------|--------|-------------|------------|--------|-------------|
| 1e5              | 1.318    | 99,856     | 0.133  | 23.2        | 99,856     | 0.044  | 23.2        |
| 1e6              | 4.167    | 1,000,000  | 0.008  | 8.1         | 1,000,000  | 0.412  | 221.1       |
| 1e7              | 13.176   | 9,998,244  | 0.015  | 93.4        | 9,998,244  | 3.262  | 2,225.7     |
| 5e7              | 29.463   | 50,013,184 | 0.027  | 259.1       | 50,013,184 | 14.037 | 10,379.9    |
| 1e8              | 41.667   | 100,000,000| 0.160  | 494.2       | not run    | n/a    | n/a         |
| 2.5e8            | 65.881   | 238,476,584| 0.091  | 283.8       | not run    | n/a    | n/a         |
| 5e8              | 93.169   | 410,440,160| 0.145  | 349.8       | not run    | n/a    | n/a         |

Cap-picking heuristic (from `specs/v0.5.3_hardening_checklist_v2.md` §3):
* `MAX_BBOX_CELLS_LINE` = lowest cell count where line_s > 1.0 s OR line_RSS > 800 MB.
* `MAX_POLYGON_CELLS`   = lowest cell count where poly_s > 2.0 s OR poly_RSS > 1200 MB.

Observed from this run:
* The line-path heuristic was not hit even at the largest tested square
  bbox; current fallback `MAX_BBOX_CELLS_LINE=2e8` remains conservative.
* The polygon-path heuristic would point to `MAX_POLYGON_CELLS≈1e7`,
  but that directly conflicts with the spec's requirement to keep large
  thin frontend polygons working. Do not lower the polygon cap solely
  from this square-bbox probe; reconcile it against the real frontend
  archetypes before changing the production default.

### Phase D2 — polygon cap probe for frontend-like shapes

Phase D2 was added specifically because `frontend rows` and
`raw_bbox_cells` are different quantities. The script probes a few
frontend-like polygon archetypes and records only scalar metrics:
`raw_bbox_cells`, `mask_ratio`, estimated rows after `sample=5`,
wall-time, and RSS delta.

**Reproducer (on Mac, canonical 2026 Zarr present):**

```bash
cd ~/proj/gebco
./.venv/bin/python dev2026/scripts/probe_polygon_cap_shapes.py
```

Measured summary:

| shape | safe up to raw cells | first bad raw cells | mask ratio at knee | est. rows at knee (`sample=5`) | limiting factor |
|-------|----------------------|---------------------|--------------------|-------------------------------:|-----------------|
| dense_rect | 6,728,836 | 7,333,264 | 1.000 | 293,331 | rss/time |
| thin_rect (diagonal ribbon) | none | 1,052,676 | 0.048 | 2,041 | time |
| coastal (jagged) | none | 999,600 | 0.874 | 34,964 | time |
| cross180_thin | none | 46,497,188 | 0.071 | 131,965 | rss/time |

Key takeaway:

* `frontend rows` is **not** a safe proxy for backend polygon cost.
* The diagonal-ribbon thin polygon crossed the `2.0 s` threshold at only
  ~`1.05e6` raw bbox cells while estimating just ~`2k` returned rows
  after `sample=5`.
* The cross-180 thin case was much worse: ~`4.65e7` raw bbox cells,
  mask ratio only `0.071`, estimated rows only ~`132k`, yet the split +
  mask path took `112 s` and ~`8.3 GB` RSS.

Interpretation:

* The Phase D square-bbox result (`~1e7`) was **not** just pessimism.
* Large sparse polygons can still be backend-expensive because the
  current implementation allocates arrays over the full bbox before the
  mask eliminates most cells.
* Therefore the existing frontend "1M rows" expectation must **not** be
  used to justify a large `MAX_POLYGON_CELLS` default.

### Phase E — dask multiprocessing pool ablation (`GEBCO_DASK_POOL_SIZE`)

**v0.5.3 ships with the env knob only**; default `4` preserves v0.5.2
behaviour byte-for-byte. The flip to `0` (off) is a separate small
follow-up commit gated on these two stages.

#### Stage A — dev2026 micro-benchmark

```bash
cd ~/proj/gebco/dev2026
GEBCO_DASK_POOL_SIZE=4 uv run python scripts/benchmark_old_new_api.py --n 400 --warmup 20 --seed 42
GEBCO_DASK_POOL_SIZE=0 uv run python scripts/benchmark_old_new_api.py --n 400 --warmup 20 --seed 42
```

Mac micro-benchmark results:

* `GEBCO_DASK_POOL_SIZE=4`
  * OLD median `2.41 ms`, P95 `4.10 ms`
  * NEW median `2.40 ms`, P95 `4.11 ms`
* `GEBCO_DASK_POOL_SIZE=0`
  * OLD median `2.40 ms`, P95 `3.76 ms`
  * NEW median `2.38 ms`, P95 `3.48 ms`
* NEW median ratio `(off / on)` = `0.99×`
* NEW P95 delta `(off - on)` = `-0.63 ms`

Method caveat: `benchmark_old_new_api.py` measures the direct
`src.zprofile.zprofile()` point path, not the full FastAPI import path
that configures the dask pool in `gebco_app.py`. So Stage A is useful as
an upper-bound sanity check, but Stage B soak on VM37 remains the real
decision gate for changing the default.

#### Stage B — VM37 staging soak

Plan: run with `GEBCO_DASK_POOL_SIZE=4` for one week, capture PM2
latency + RSS baseline; switch to `GEBCO_DASK_POOL_SIZE=0` for one week
and compare. Decision rule: flip default to `0` if both stages within
5 % P95.

### Phase F — long-polyline benchmark (v0.5.4 W1 / H9)

H9 (`np.append` → list accumulation in `src/zprofile.py` +
`src/xmeridian.py`, ~70 call sites) lands in v0.5.4 PR1 together with
the H13 follow-up (`gebco_app.py` consumes a polars DataFrame from
zprofile in dataframe mode internally — no JSON round-trip for the
request log row count).

**Reproducer (run on Mac, canonical 2026 Zarr present):**

```bash
cd ~/proj/gebco

# Sparse mode — 2-vertex line spanning 20° (the asymptotic H9 regime).
# Exercises the inner cell-walk loop with ~4800 nested-loop iterations
# per segment; this is where the v0.5.3 np.append cost was O(n²).
uv run python dev2026/scripts/benchmark_long_polyline.py \
    --style sparse --span-deg 20 --warmup 2 --trials 5 --seed 2026

# Dense mode — 5000 input vertices over a small span. Every segment is
# sub-cell so the inner loop is skipped; H9 only affects segment-level
# appends. Expected speedup is modest (~1.2–1.5×) because numpy
# realloc on small growing arrays is well-amortised by the allocator.
uv run python dev2026/scripts/benchmark_long_polyline.py \
    --style dense --n 5000 --warmup 2 --trials 5 --seed 2026
```

Acceptance (v0.5.4 plan §8 / §12, final Mac run):

* **sparse-style total wall ≥ 3×** vs v0.5.3 baseline
* **dense-style total wall ≥ 3×** vs v0.5.3 baseline
* decomposition profile on the final branch should still show that the
  remaining wall is mostly Zarr/xarray rather than the H9 inner loop
* no regression on single-point latency
* `verify_polygon_meridian.py` T5/T6 numbers byte-equal across the
  H9 + pyproj swap (pyproj.Geod.inv numerical output matches geopy to
  3.6 pm — both use WGS84/Karney geographiclib internally)

Final decomposition snapshot (post-fix branch):

  `profile_sparse_polyline.py` on the final branch reports:
  `crossBoundary 0.02 ms`, `ds_sel 0.58 ms`, `values_materialise 13.98 ms`,
  `inner_walk 4.96 ms`, `buffers_to_array 0.98 ms` per call
  (timed total `20.52 ms`). So after H9 lands, the remaining wall is
  indeed mostly the Zarr materialisation stage. That decomposition is
  now a description of the *post-fix* bottleneck, not a reason to relax
  the total-wall target: the final Mac benchmark exceeds the original
  ≥3× goal by a large margin.

Reproducer for the decomposition (run on Mac, real Zarr):

```bash
cd ~/proj/gebco
uv run python dev2026/scripts/profile_sparse_polyline.py \
    --span-deg 20 --trials 5
```

| style  | param        | impl   | median ms | P95 ms | RSS Δ MB |
|--------|--------------|--------|-----------|--------|----------|
| sparse | span=20°     | v0.5.3 |   206.26  | 207.20 |   30.2   |
| sparse | span=20°     | v0.5.4 |    22.94  |  23.00 |   25.8   |
| dense  | n=5000       | v0.5.3 |   237.44  | 242.01 |    0.3   |
| dense  | n=5000       | v0.5.4 |    15.71  |  15.98 |    0.2   |

Derived total-wall speedups on Mac:

* sparse: `206.26 / 22.94 = 8.99×`
* dense:  `237.44 / 15.71 = 15.11×`

H13 follow-up check: `verify_polyhandler_endpoint.py` row counts
unchanged across the dataframe-mode switch in `gebco_app.py` (the
handler now reads `df.height` instead of decoding the response body).

### Phase G — polygon redesign benchmark (v0.5.4 W2-B)

W2-B redesigns `src.polyhandler.process_polygon_part`:

1. **Cell-budget batching** — `GEBCO_POLYGON_BATCH_CELLS` (default
   `1_000_000`) caps per-batch cells regardless of bbox aspect.
   `batch_lats = max(1, BATCH_CELLS // n_lons)`.
2. **`shapely.contains_xy`** instead of `shapely.points + shapely.contains`
   — vectorises into GEOS directly from numpy arrays without one Python
   `Point` object per cell. Round-3 sandbox measurement on a 1M-point
   batch: 29.5 ms vs 358.6 ms (~12× faster), byte-equal mask output,
   and ~10× less RSS per batch.
3. **`gc.collect()` per batch** — `del` alone doesn't always trigger
   immediate cycle collection on GEOS / numpy internals; forcing
   `gc.collect()` after each batch keeps peak RSS bounded to one
   batch's working set. Without this, ~24 batches' worth of shapely
   temporaries accumulated to ~2 GB on the D2 cross180_thin case
   (codex measured 2172 MB RSS pre-round-3).

> **Sample-aware reality check.** At the production endpoint default
> (`sample=5`) the strided bbox is 25× smaller than at `sample=1`, so
> the W2-B fragmentation often doesn't bite — small / coastal /
> dense-rect archetypes still fit in one batch and W2-B is effectively
> a no-op. The win is most pronounced for the D2 cross180_thin case at
> `sample=1` (raw bbox 46 M cells → ~24 batches per half instead of
> 1). That's why `benchmark_polygon_shapes.py` defaults to `--sample 1`
> — it measures the worst-case path the plan §12 acceptance numbers
> were derived from.

#### G.0 cProfile snapshot (pre-W2-B) — MUST land before PR2

The v0.5.4 plan §5 pre-work paragraph requires capturing where the v0.5.3
112 s for `cross180_thin` actually goes (shapely.points vs
shapely.contains vs meshgrid vs DataFrame build) before the redesign
ships. This sets the reviewer's expectation for whether W2-B should
improve wall time, RSS, or both.

```bash
# Run on Mac. Do NOT run inside the sandbox — 8 GB RSS will OOM.
cd ~/proj/gebco
uv run python dev2026/scripts/profile_cross180_thin.py \
    --width 25
```

| rank | function                                       | cum %  |
|------|------------------------------------------------|--------|
| 1    | `src.polyhandler.polyhandler`                  | ~100%  |
| 2    | `src.polyhandler.process_polygon`              | ~100%  |
| 3    | `src.polyhandler.process_polygon_part`         |  ~91%  |
| 4    | `xarray.DataArray.values` / `Variable.values`  |  ~73%  |
| 5    | `dask.array.__array__` / `dask.base.compute`   |  ~73%  |

Note: after fixing the profiling script to exclude import/open overhead,
the pre-W2-B `sample=5` snapshot is small enough that the hottest frames
are the actual polygon call stack plus xarray/dask materialisation. The
very large `sample=1` failure mode is quantified by Phase D2 and G.1.

#### G.1 P-Bench-2 archetype benchmark — before / after

```bash
cd ~/proj/gebco
uv run python dev2026/scripts/benchmark_polygon_shapes.py --trials 3
```

The script enforces the v0.5.4 plan §12 acceptance thresholds; if any
archetype median exceeds, the script returns non-zero and prints which.

| archetype     | impl       | rows | median s | P95 s | RSS Δ MB | verdict |
|---------------|------------|------|----------|-------|----------|---------|
| thin_ribbon   | v0.5.3     | 54358 |  2.988  | 2.988 |  180.3   | FAIL    |
| thin_ribbon   | v0.5.4 W2B | 54358 |  0.121  | 0.122 |    5.8   | PASS    |
| coastal       | v0.5.3     | 873191 | 3.288  | 3.288 |  204.9   | FAIL    |
| coastal       | v0.5.4 W2B | 873191 | 0.127  | 0.129 |   51.8   | PASS    |
| cross180_thin | v0.5.3     | 3299125 | 114.477 | 114.477 | 6304.0 | FAIL |
| cross180_thin | v0.5.4 W2B | 3299125 | 4.115 | 4.129 | 203.6 | PASS |

Acceptance (v0.5.4 plan §12):

* `thin_ribbon`   median ≤ 0.5 s
* `coastal`       median ≤ 1.5 s
* `cross180_thin` median ≤ 30 s AND RSS Δ ≤ 2000 MB

#### G.2 T7 cross-180 thin regression

```bash
uv run python dev2026/scripts/verify_polygon_meridian.py
```

T7 was added in v0.5.4 (covers the D2 cross180_thin geometry).
Acceptance: identical row count + identical cell coordinates between
2023 and 2026, z|Δ| P95 within the same band as T1–T6.

| Case | Label                                 | n cells | exact | P50 | P95 | P99 | max | verdict |
|------|---------------------------------------|---------|-------|-----|-----|-----|-----|---------|
| T7   | Polygon — cross-180 thin (D2 archetype) | 2880 | 407 | 3 | 16 | 33 | 88 | PASS |

### Phase H — `MAX_POLYGON_CELLS` revisit after PR2

Per v0.5.4 plan §6 PR3 derived target:

> if post-W2 `cross180_thin` at ~5e7 raw bbox cells is under
> `5.0 s / 1 GB RSS`, `MAX_POLYGON_CELLS` should be re-evaluated in the
> `5e7–1e8` range instead of left at `2e9`.

Decision (Mac final run):

* `cross180_thin` at the D2-sized benchmark now lands at `4.115 s /
  203.6 MB`, comfortably under the derived `5.0 s / 1 GB RSS` target.
* Therefore `GEBCO_MAX_POLYGON_CELLS` can be tightened from the
  provisional `2e9` to **`5e7`** by default.
* The env knob remains overrideable for operational tuning, but the
  default is now a real guard rather than a dormant placeholder.

---

## v0.5.3 Hardening — sandbox unit-test run (this branch)

Run on the Linux sandbox where polars-lts-cpu can't reliably install
(35 MB download keeps timing out). Excludes the four polars-dependent
test files; those run on a Mac.

```bash
.venv/bin/python -m pytest \
    tests/test_modes.py tests/test_validation.py tests/test_lon360_input.py \
    tests/test_jsonsrc.py tests/test_logger_lifecycle.py tests/test_xmeridian.py \
    -p no:cacheprovider --basetemp=/tmp/pytt
# 64 passed, 4 warnings in 0.15s
```

On Mac (with polars present), the full suite is:

```bash
cd ~/proj/gebco
uv run --group dev pytest tests/ -q
# expected: 100+ passed (modes / validation / lon360 / jsonsrc / logger /
#           xmeridian / zprofile / polyhandler / bbox_guard)
```

---

## Phase I — v0.5.5 line sparse-read rollout and public parity

This phase covers the post-v0.5.4 line-path redesign (`gebco_2026_perf_v054`)
that changed the effective line cap behaviour. The important operational fact
is that **both public VMs now run the same v0.5.5 code path**:

* VM37 / `api.odb.ntu.edu.tw`
* VM34 / `ecodata.odb.ntu.edu.tw`

Both serve from the root checkout (`~/python/gebco`), branch
`gebco_2026_perf_v054`, revision `ad4179c`, via pm2 → root `.venv` gunicorn.

### I1. Production reality check that caught a real rollout hazard

The first VM34 cutover looked healthy for simple point smoke, but `Q4 lon360`
still returned `500`. Root cause was **not** the branch code. The VM was still
serving an old `pyenv`/gunicorn process on `127.0.0.1:8013`; pm2 metadata had
not actually taken over the listener yet. Once pm2 was restarted explicitly via
`conf/ecosystem.config.js`, loopback `Q4` immediately became `200`.

Practical lesson for future agents:

* do **not** trust git branch state or file contents alone after deployment
* always verify the live listener with both:
  * `pm2 describe gebco`
  * `ps -ef | grep gunicorn | grep 8013`
* if you still see `/home/odbadmin/.pyenv/.../gunicorn`, you are not testing
  the intended runtime even if the repo checkout is correct

### I2. Public A/B after VM34 cutover (v0.5.4 baseline vs v0.5.5 candidate)

Harness:

```bash
cd ~/proj/gebco
uv run python dev2026/scripts/api_compare_v054_vs_v052.py
```

At this moment the filename was historical. The actual comparison was:

* `api.odb` = v0.5.4 baseline
* `ecodata` = v0.5.5 candidate

Key result:

* `Q1–Q7` normal-success cases returned byte-identical bodies between the two
  public sites where behaviour was expected to match
* `Q5` large line and `Q6` large `MultiLineString` were dramatically faster on
  the v0.5.5 site
* `Q8` near-cap diagonal transect returned `200` on v0.5.5 while the v0.5.4
  site still returned `413`

This is the evidence that v0.5.5 is not merely "not slower"; it resolves a
practical breaking change introduced by the v0.5.4-era line bbox proxy.

### I3. VM34 stability soak before VM37 rollout

Loopback-only stability script (10 repeats each for Q1/Q4/Q5/Q6, then small
concurrency, then `pm2 restart gebco --update-env` and re-check):

| Case | repeated median | repeated P95 | bytes |
|------|------------------|--------------|-------|
| Q1 cross-0 line | 12.6 ms | 13.1 ms | 42,675 |
| Q4 lon360       | 15.6 ms | 72.8 ms | 62,895 |
| Q5 large line   | 90.4 ms | 95.3 ms | 647,512 |
| Q6 MultiLineString | 165.4 ms | 165.9 ms | 1,252,806 |

Small concurrency:

| Case | concurrency | ok | round wall | median req |
|------|-------------|----|------------|------------|
| Q5 | 4 | 4/4 | 210.1 ms | 202.9 ms |
| Q5 | 6 | 6/6 | 211.8 ms | 199.6 ms |
| Q6 | 4 | 4/4 | 365.2 ms | 358.7 ms |
| Q6 | 6 | 6/6 | 479.3 ms | 457.9 ms |

Restart safety (`pm2 restart gebco --update-env`):

| Case | status | wall |
|------|--------|------|
| Q1 | 200 | 208.8 ms |
| Q4 | 200 | 99.0 ms |
| Q5 | 200 | 101.9 ms |
| Q6 | 200 | 166.4 ms |

No new traceback appeared in `tmp/err.log`; only normal gunicorn shutdown /
startup lines were observed. VM34 staging listener `.stage_v055:18014` was
removed after the soak to avoid future confusion.

### I4. VM37 rollout to v0.5.5

VM37 was then updated to the same branch / revision and restarted through pm2.
The first smoke immediately confirmed:

* point mode `200`
* `Q1` cross-0 line `200`
* `Q4` lon360 `200`

The important follow-up was `Q8`. An early public check still saw `413`; after
completing the real pm2 cutover and confirming the root `.venv` gunicorn was
bound to `8013`, both loopback and public checks returned `200`.

This means the earlier `413` was a deployment-state artefact, not a remaining
code bug.

### I5. Final public parity (both VMs on v0.5.5)

Final direct public check for the original `Q8` near-cap transect:

```text
https://api.odb.ntu.edu.tw/gebco?...Q8...      -> 200, ~134.7 ms
https://ecodata.odb.ntu.edu.tw/gebco?...Q8...  -> 200, ~108.1 ms
```

So the final deployed state is:

* both public VMs are on v0.5.5
* `Q8` is accepted on both
* the practical v0.5.4 breaking change (reasonable long transect rejected by
  early bbox cap) is resolved in production

### I6. Temporary VM34 polygon-cap compatibility override

After both VMs were on v0.5.5, a separate frontend integration issue appeared:
the ODB map frontend behind `service.oc.ntu.edu.tw/data/gebco` did not handle
backend `413` gracefully for very large polygon requests. One real user query
returned:

* `413` with branch default `GEBCO_MAX_POLYGON_CELLS=5e7`
* `200` when the same code was run with `GEBCO_MAX_POLYGON_CELLS=2e9`

Measured on VM34 with the exact polygon payload in a temporary loopback probe:

| cap | status | wall | bytes | rows |
|-----|--------|------|-------|------|
| `5e7` | 413 | immediate | n/a | n/a |
| `2e9` | 200 | ~6.3 s | ~279 MB | 10,729,265 |

This is **not** a claim that the payload is frontend-safe — it is far beyond
the frontend's own 1M-point rendering limit. The temporary override exists
only so the frontend can receive data (or apply its own client-side cap path)
instead of crashing on an unhandled `413`.

Operational decision taken on `2026-05-31`:

* VM34 / `ecodata`: set `.env` `GEBCO_MAX_POLYGON_CELLS=2000000000`
* VM37 / `api.odb`: keep the branch default `5e7`

This is an intentionally asymmetric policy and should be revisited after the
frontend adds explicit `413` handling.
