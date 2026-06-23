# Bug report — cross-180° depth discontinuity (int16 cell-index overflow)

- **Date investigated:** 2026-06-23
- **Branch:** `gebco_2026_perf_v054` (HEAD `e15e37b` at start of investigation)
- **Severity:** HIGH — silent data-correctness bug; production returns wrong
  depths for any line crossing the antimeridian in non-`zonly` modes.
- **Status:** **FIXED on branch, not yet committed/deployed.** Awaiting
  independent review (codex) before deploy.
- **Files changed:** `src/zprofile.py` (1-line dtype fix + comment),
  `tests/test_zprofile.py` (new regression test).

---

## 1. Symptom / reproduction

A line crossing 180° returns a severe, unphysical depth step. The two halves
of the antimeridian should be near-identical depth, but the API jumps from
~−3000 m to ~−5000 m at the crossing. Crossing 0° does **not** show this.

Reproduce against production (GEBCO_2026):

```
# crosses 180° (lon goes 179 -> -180): WRONG before the fix
https://api.odb.ntu.edu.tw/gebco?lon=179,-180&lat=9,9&mode=truncate

# does NOT cross (lon goes 179 -> 180, same physical segment): CORRECT
https://api.odb.ntu.edu.tw/gebco?lon=179,180&lat=9,9&mode=truncate
```

These two requests describe the **same physical segment** (179°E → 180°), so
they must return identical depths. Before the fix they did not:

| | lon at start (≈178.999°E) | reported z |
|---|---|---|
| `lon=179,-180` (crossing path) | 178.999 | **−3748** ← wrong |
| `lon=179,180` (normal path) | 178.999 | **−5850** ← correct |

The smooth "~−3700" half seen in the elevation plot is a **real seabed
profile from the wrong ocean** (see §3).

## 2. It is NOT a data-source bug

The user's first hypothesis was that the GEBCO_2026 grid differed from 2023.
It does not:

- The 2023 and 2026 Zarr coordinate arrays are **byte-identical**: `lon`
  ascending, cell-centred, `lon[0]=-179.99792`, `lon[-1]=179.99792`, 86400
  cells; `lat` likewise, 43200 cells.
- The corruption **reproduces on the 2023 grid too** under the current code.
- `verify_api_vs_netcdf.py` (1000 random points) shows GEBCO_2026 Zarr is
  byte-equal to the source NetCDF.

So the grid is fine; the regression is in code.

## 3. Root cause — `np.int16` index overflow

`src/zprofile.py` accumulates the bbox-relative `(lat_idx, lon_idx)` cell
indices of the line walk, then materialises them:

```python
idx1 = np.asarray(idx1_buf, dtype=np.int16)   # <-- the bug
...
xt1 = ds_s1["elevation"].values[tuple(idx1.T)]
```

`np.int16` holds **−32768..32767**.

For a **normal** line, `mlonbase` (the bbox origin) sits next to the line, so
relative column indices stay small — no overflow.

For a line **crossing 180°**, `src/xmeridian.crossBoundary` inserts break
points at **both** −180 and +180. The rewritten coordinate set therefore spans
the whole globe in longitude, `mlonbase` collapses to ≈0, and the relative
column indices become the **absolute** grid columns — up to **86399**.

`86399` does not fit in int16. It **wraps**:

```
np.int16(86159) == 20623        # 86159 - 65536
```

Column 20623 is longitude **−94.07°** (Pacific off Central America). So the
crossing path silently reads a transect there instead of at the antimeridian:

```
elev[lat=9, col 20623 (wrapped)] = -3748   # what the API returned
elev[lat=9, col 86159 (correct)] = -5850   # the true depth at lon 179
```

This is exactly the user's intuition — the line "wraps around to the other
side of the Earth." 241 of 242 output rows were corrupted; only the final
endpoint (`-180`, column 0) survived because 0 does not overflow.

### Why 0° is fine but 180° breaks

A 0°-crossing line keeps `mlonbase` near the line, so relative indices stay
well under 32767. Only the 180° crossing forces the full-width bbox that
pushes indices past the int16 ceiling.

### Why 2023 (old version) was fine

The pre-`v0.5.4` code built `idx1` with `np.append`:

```python
idx1 = np.empty(shape=[0, 2], dtype=np.int16)
idx1 = np.append(idx1, [[lat_idx, lon_idx]], axis=0)   # promotes to int64!
```

`np.append` concatenates the int16 array with the appended Python-int values
and **promotes the result to int64**, so the declared int16 never actually
held the indices — no overflow ever occurred.

The **v0.5.4 W1/H9 performance refactor** (commit `edb78bb`) replaced the
per-iteration `np.append` with a Python-list accumulator plus a single
`np.asarray(idx1_buf, dtype=np.int16)`. It copied the `int16` from the old
empty-array initialiser, not realising `np.append` had been silently
promoting to int64. That refactor's "byte-equal to v0.5.3" claim is therefore
**false for cross-180 / wide-bbox lines.**

## 4. Fix

`src/zprofile.py` — use a 64-bit index dtype (grid columns reach 86399, rows
43199; both exceed int16, and int64 matches numpy's natural index dtype and
the pre-H9 promoted behaviour):

```diff
         if idx1_buf:
-            idx1 = np.asarray(idx1_buf, dtype=np.int16)
+            # NOTE: must be a 64-bit index dtype. The v0.5.4 H9 refactor used
+            # np.int16 here, but bbox-relative cell indices reach ~86399 for
+            # cross-180° lines (crossBoundary inserts break-points at both
+            # ±180, so mlonbase≈0 and the subset spans the full grid width).
+            # int16 (max 32767) silently wraps those to garbage columns, so
+            # the API read the wrong ocean. The pre-H9 np.append code was
+            # correct only because np.append promoted the array to int64.
+            idx1 = np.asarray(idx1_buf, dtype=np.int64)
         else:
-            idx1 = np.empty(shape=(0, 2), dtype=np.int16)
+            idx1 = np.empty(shape=(0, 2), dtype=np.int64)
```

The change can only *correct* indices that were overflowing; small indices are
bit-for-bit unchanged, so non-crossing behaviour is untouched.

The `v0.5.5` sparse `zonly` line path is **not affected** — `src/line_planner.py`
already uses `np.int32` for its index buffers.

## 5. Verification

| Check | Before fix | After fix |
|---|---|---|
| `lon=179,-180` z at lon 179 | −3748 (wrong ocean) | **−5850** (= non-crossing) |
| crossing vs non-crossing 179→180 interior | 241/242 rows differ | **0 differ** |
| plot case `178.7 → −178.8`, lat 8.5 | mid-line discontinuity | **continuous, both sides ~−5800/−6000** |
| per-coordinate ground truth vs raw grid | 241 mismatches | **0 mismatches** |
| `uv run --group dev pytest tests/` | 100 passed | **101 passed** (added regression test) |
| `verify_polygon_meridian.py` T1–T7 | PASS | PASS |
| `verify_polyhandler_endpoint.py` T1/T4 | PASS | PASS |

Note on `verify_polygon_meridian.py` **T6** (line crossing 180°): its P95
moved 102 m → 140 m after the fix. This is **expected and healthy**. That
script compares the 2023-data API against the 2026-data API, both through the
*same* current code; before the fix both sides read the same wrapped cells, so
the bug cancelled and the diff looked small. After the fix both read correct
cells, so T6 now measures the genuine 2023↔2026 geophysical difference. (This
also reveals a blind spot in that harness: comparing two equally-buggy reads
can hide a bug — the decisive check is per-coordinate ground truth against the
raw grid, as used above.)

### Regression test

`tests/test_zprofile.py::test_cross_180_line_matches_equivalent_noncrossing_line`
builds a full-width (`basex=180`) toy grid so the cross-180 column index
exceeds int16, then asserts the crossing and non-crossing forms of 179°→180°
return identical interiors. Confirmed it **FAILS on `int16`, PASSES on
`int64`**.

---

## 6. Others (found in the same pass — now also fixed)

These were found while investigating and verifying the API against the
GEBCO_2026 data path. The data path itself is sound (byte-equal vs source
NetCDF; sparse-vs-legacy line reads byte-identical). **All four are now fixed
on the branch** (same uncommitted change-set as the main bug); details below.

### O-1 — `dev2026/pyproject.toml` was missing `pyproj` (HIGH for tooling) — **FIXED**
*Tooling only — production unaffected.*

The v0.5.4 H9 swap (geopy → pyproj) added `pyproj>=3.6,<4` to the **root**
`pyproject.toml` but not to `dev2026/pyproject.toml`. The two venvs are now
mutually incomplete:

- root/production venv: has `pyproj`, no `netCDF4`
- dev2026 venv: has `netCDF4`, no `pyproj`

So the scripts that need **both** netCDF4 *and* `src.zprofile` —
`verify_api_vs_netcdf.py`, `compare_old_new_api.py`, `benchmark_old_new_api.py`
— cannot run in a clean `uv sync` of either venv (`ModuleNotFoundError:
pyproj`). These are exactly the scripts used to validate a new GEBCO data drop
against the source NetCDF. (Verified the fix by injecting `--with pyproj`, after
which `verify_api_vs_netcdf.py` passes 1000/1000.)

**Fix applied:** added `"pyproj>=3.6,<4"` to `dev2026/pyproject.toml`
dependencies. **Verified:** a clean `uv run` of the dev2026 venv now runs
`verify_api_vs_netcdf.py` with no injection — 500/500 points byte-equal to the
source NetCDF.

### O-2 — single-point `mode=lon360` output not normalized (MEDIUM) — **FIXED**
*Correct z, wrong echoed longitude convention.*

In `src/zprofile.py` the `len(loni)==1` branch never applied the `lon360` (or
`truncate`) post-processing that the multi-point line path applies. A
single-point `lon=200&lat=10&mode=lon360` echoed longitude `-160.0` instead of
`200.0`. The depth value was correct; only the reported longitude was in the
wrong `[-180,180]` convention.

**Fix applied:** the `len==1` branch now applies `truncate`/`lon360` to the
echoed coordinates, matching the multi-point branch. **Tests added:**
`test_single_point_lon360_output_in_0_360`,
`test_single_point_truncate_rounds_coords`. Verified single-point
`lon360` → `200.0`, `lon360+truncate` → `199.87654`.

### O-3 — cross-180° lines over-trip `MAX_BBOX_CELLS_LINE` in non-`zonly` modes (MEDIUM) — **FIXED**
*Related to the main bug; same cross-meridian path. Was the user's "cross-180
trips the length cap easily even though the segment isn't long."*

The legacy (non-`zonly`) line read materialised the whole enclosing bbox
(`ds_s1["elevation"].values[idx]`) and guarded on the global min/max bbox cell
count. For a crossing line the bbox spans the full 360° width, so the read
wasted memory **and** the early guard rejected short transects with `413`,
while *longer* non-crossing lines passed. (`zonly` was already fine via the
v0.5.5 sparse path.)

**Fix applied:** the non-`zonly` line path now uses the **same touched-chunk
read** as the v0.5.5 sparse `zonly` path, generalised into
`src/zprofile._read_cells_by_chunk` / `_line_chunk_budget_global`:

- Lines whose enclosing bbox ≥ `LINE_SPARSE_MIN_CELLS` read only the touched
  chunks (via the cached `config.elev_zarr`, or `isel` fallback), guarded by
  `MAX_LINE_CHUNKS` → `413 kind="line_chunks"` like the zonly path.
- Smaller lines keep the cheaper dense read, still bounded by
  `MAX_BBOX_CELLS_LINE` → `413 kind="bbox_cells"`.
- The early global-bbox guard is removed; guarding happens at read time.

The chunk gather is **byte-identical** to the dense read (verified: the
`mlonbase`/`mlatbase` offset means `ds_s1.values[idx] == elev[idx+base]`).

**No cap value was retuned** — this only changes *which* guard applies and
*how cells are counted* for crossing lines (per `v0.5.5` plan §W4, which warns
against blind cap retuning). `MAX_LINE_CHUNKS` keeps its existing default 64.

**Verified on real data (GEBCO_2026):** cross-180 lines that previously
returned `413` in `truncate` mode (e.g. `lon=175,-175&lat=-5,5`; the long Q8
diagonal) now return `200` with **0 mismatches** vs per-coordinate ground
truth; normal lines remain byte-identical; the chunk guard still trips with
`kind="line_chunks"` for genuinely over-budget lines. **Tests added:**
`test_cross_180_chunk_read_matches_dense_no_premature_413`,
`test_cross_180_chunk_guard_trips_with_kind`.

### O-4 — `xmeridian.py` divide-by-zero RuntimeWarning (LOW / cosmetic) — **FIXED**
*No correctness impact.*

`src/xmeridian.py` (~L78 and ~L140) computed `absxdelta = abs((0.499/arc)/m)`
eagerly; on a horizontal segment (`m == 0`) crossing a meridian this divided by
zero and produced an unused `inf`, emitting a pytest `RuntimeWarning`.

**Fix applied:** `absxdelta` is now computed only when `absm > 1` (the only
place it is consumed, which also guarantees `m != 0`). The xmeridian
RuntimeWarnings are gone from the test run.

---

## 7. Change-set summary & notes for the reviewer / deploy

All changes are on `gebco_2026_perf_v054`, **uncommitted**, no `main`
operations.

| File | Change | For |
|---|---|---|
| `src/zprofile.py` | `idx1` int16→int64 | main bug |
| `src/zprofile.py` | single-point lon360/truncate normalize | O-2 |
| `src/zprofile.py` | `_read_cells_by_chunk` / `_line_chunk_budget_global`; non-zonly chunk read + read-time guard; remove early bbox guard | O-3 |
| `src/xmeridian.py` | lazy `absxdelta` | O-4 |
| `dev2026/pyproject.toml` | add `pyproj>=3.6,<4` | O-1 |
| `tests/test_zprofile.py` | 5 new regression tests | main, O-2, O-3 |
| `AGENTS.md` | gotcha #15 (index dtype) + v0.5.5 line-read note | docs |

Verification: `uv run --group dev pytest tests/` → **105 passed**;
`verify_polygon_meridian.py` T1–T7 PASS; `verify_polyhandler_endpoint.py` PASS;
`verify_api_vs_netcdf.py` (dev2026 venv, no injection) 500/500 byte-equal.

Suggested deploy verification on each VM after rollout (loopback smoke):

```bash
# both must return the SAME z series for the shared 179->180 interior (main fix)
curl -sk --get 'https://127.0.0.1:8013/gebco' \
     --data-urlencode 'lon=179,-180' --data-urlencode 'lat=9,9' --data-urlencode 'mode=truncate'
curl -sk --get 'https://127.0.0.1:8013/gebco' \
     --data-urlencode 'lon=179,180'  --data-urlencode 'lat=9,9' --data-urlencode 'mode=truncate'

# O-3: a cross-180 line with a real lat span must now return 200, not 413
curl -sko /dev/null -w '%{http_code}\n' --get 'https://127.0.0.1:8013/gebco' \
     --data-urlencode 'lon=175,-175' --data-urlencode 'lat=-5,5' --data-urlencode 'mode=truncate'
```

**Deploy reminder:** VM34 keeps its temporary `GEBCO_MAX_POLYGON_CELLS`
override; this change does not touch the polygon path. `GEBCO_LOG_SAMPLE_RATE=0`
on both VMs means `line_stats` won't be emitted — raise it temporarily if you
want to observe the new chunk-read stats in production.
