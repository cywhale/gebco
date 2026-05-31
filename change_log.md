#### ver 0.1.1 port on server 20221125

#### ver 0.1.2 Column(default)/row-output(row), point/line in mode(query string)

    - fix last end-point not in output
    - use geopy.geodesic to output distance between two points

###### 0.1.2.1: fix start-point in intra-segments should append distance, vertical/horizontal should judged by points, not grids

###### 0.1.2.2: fix slop>1 should use latitude-grid to count, not longitude; fix intra-segments lost/extra last point problem

###### 0.1.2.3: fix typo when two points-grid are identical (lonx -- lon) and should calculate distance (not zero)

###### 0.1.2.4: Use Polars. fix point-mode should prior to criteria of other modes

#### ver 0.2 Breaking: add crossing 0-/180- (prime/anti meridian) mode 20221201

    - add gebco_v1.json for openapi(apiverse)
    - upgrade package versions (requirements.txt) 20230117

###### ver 0.2.1 Upgrade to python3.10 xarray v2023.02.0 drop py3.8

    - https://github.com/pydata/xarray/releases/tag/v2023.02.0
    - pip-upgrade ./requirements.txt 20230426
```
+-----+-----------+-----------------+----------------+---------------------+
| No. | Package   | Current version | Latest version | Release date        |
+-----+-----------+-----------------+----------------+---------------------+
|  1  |  dask     | 2023.1.0        | 2023.4.0       | 2023-04-14 18:45:05 |
|  2  |  fastapi  | 0.90.0          | 0.95.1         | 2023-04-13 19:11:30 |
|  3  |  numpy    | 1.24.2          | 1.24.3         | 2023-04-22 21:29:36 |
|  4  |  polars   | 0.16.2          | 0.17.9         | 2023-04-25 14:48:37 |
|  5  |  uvicorn  | 0.20.0          | 0.21.1         | 2023-03-16 12:30:13 |
|  6  |  xarray   | 2023.2.0        | 2023.4.2       | 2023-04-21 04:03:23 |
|  7  |  zarr     | 2.13.6          | 2.14.2         | 2023-02-24 18:15:01 |
+-----+-----------+-----------------+----------------+---------------------+
```

###### ver 0.2.2 Drop columns args in polars DataFrame after v0.17.x

###### ver 0.2.3 Breaking move read_gebco01.py to gebco_app.py, try polygon mode in wireframe01 
    
###### ver 0.2.4 Add jsonsrc can feed JSON url/string, and fix openapi.json in /swagger

###### ver 0.2.5 Package upgrade/improve pm2 restart by pre_stop


#### ver 0.3.0 Breaking upgrade to GEBCO(2023) now. Change to use pipenv for package version management 

###### ver 0.3.1 Change Zarr dataset chunk-size to 675*2700 imporve startup loading in GEBCO(2023)

###### ver 0.3.2 Breaking: support GeoJSON to wireframe polygon mode/n1 still need resample, polygon cross xmeridian

###### ver 0.3.3 small fix 'list' format not array of long/lat/z bug

###### ver 0.3.4 support Polygon cross 180-degree line

###### ver 0.3.5 support longitude (0-360) output only when cross 180-degree line

###### ver 0.3.6 resample Polygon by every 5 points in default/upgrade python packages

###### ver 0.3.7 support normal GeoJSON for jsonsrc input: {"type":"Feature","properties":{},"geometry":{"type":"Polygon",...

    -- small package upgrade/n2/README.md move swagger doc to Ocean APIverse

###### ver 0.3.8 Add truncate mode to truncate lon/lat 5 decimal places. Not ouput lineid in default/major pacage upgrade

###### ver 0.3.9 Fix lon360 mode to output in all cases (not restrict to polygon cross 180-degree)

    -- add .env (use python-dotenv)

###### ver 0.4.0 Stable version with small package upgrade (as numpy remain at v1.26.4)

    -- back to numcodecs==0.15.1, which 0.16.0 cause zarr read error cannot import name cbuffer_sizes from numcodecs.blosc

#### ver 0.5.0 Breaking upgrade to GEBCO(2026). Zarr served from Blosc/lz4 for read-speed parity with 2023

    -- Data source: GEBCO_2026 sub_ice grid (doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)
    -- Verified 100k points byte-equal between NetCDF and produced Zarr
    -- 95% of API responses within 102 m of 2023 (P95, max 339 m, no extreme ice-sheet shifts)
    -- New API median latency 1.01x of 2023 (Blosc/lz4 matches old codec — Zlib was 2.66x slower)
    -- Add dev2026/ tooling tree (uv + Python 3.13) and AGENTS.md handover notes
    -- Add /AGENTS.md as the single handover doc for AI coding agents
    -- Polygon mode (/gebco?jsonsrc=...) regression covered at two levels:
       full-resolution mask consistency (verify_polygon_meridian.py) and
       real-polyhandler endpoint regression with production sample=5
       (verify_polyhandler_endpoint.py) — T1 2304 rows P95=155m,
       T4 (crosses 180°) 1152 rows P95=195m, both pass on a clean
       dev2026 `uv sync` env (polars 1.41 + shapely 2.x).
    -- Deferred to v0.5.1 (migration / housekeeping, not behaviour change):
       revisit polars pin if needed; pygeos→Shapely 2 migration is tracked
       separately in specs/v0.5.1_migration_plan.md

#### ver 0.5.1 Runtime/deployment hardening after GEBCO_2026 upgrade

    -- Port src/polyhandler.py from pygeos to native Shapely 2 APIs
    -- Remove pygeos from production dependency manifests (Pipfile, requirements, root uv env)
    -- Add root pyproject.toml + uv.lock and migrate production runtime plan to uv-managed .venv
    -- Verify polygon endpoint regression still passes (T1/T4, sample=5) after the port
    -- Add CPU-aware Polars installer (polars vs polars-lts-cpu) for mixed-VM deployment
    -- Add specs/v0.5.1_migration_plan.md and VM37 no-downtime staging/cutover rules
    -- Cut over VM37 pm2 production to GEBCO_2026 via .stage_v051 root-uv runtime

#### ver 0.5.2 Public metadata/doc cleanup before broader production rollout

    -- Update root README.md attribution/data-source text to the official GEBCO_2026 citation
    -- Make FastAPI OpenAPI metadata env-driven (API_VERSION, API_SERVERS, GEBCO_DATASET_*)
    -- Stop advertising localhost in Swagger servers; use public domains only
    -- Bump public API metadata version to 1.1.0 and sync conf/gebco_v1.json

#### ver 0.5.3 Public-API input-surface hardening + unit-test coverage

    -- jsonsrc URL fetch: inline-first dispatch, scheme allowlist, getaddrinfo
       + ipaddress private-CIDR block (incl. IPv4-mapped IPv6), allow_redirects=False,
       streamed Content-Length-ignoring size cap (default 2 MB), connect/read timeouts.
       Default still allows public http/https URLs (no behaviour change for legit clients).
    -- lon/lat finite + range validation in both query and jsonsrc JSON paths
       (rejects NaN, ±Inf, out-of-range; 400 with concrete message).
       mode=lon360 now also ACCEPTS input in [0, 360] (validated first, then
       normalised to [-180, 180] for grid lookup).
    -- mode tokens parsed once into a frozenset; 16 substring matches retired.
       Unknown tokens are warning-only, never reject (forward-compat).
    -- bbox cell-count caps on both line/point and polygon paths; over-cap
       requests return 413 instead of OOMing. Env knobs
       GEBCO_MAX_BBOX_CELLS_LINE (default 2e8) and GEBCO_MAX_POLYGON_CELLS
       (default 2e9 — provisional. Phase D2 showed the current polygon
       implementation is bbox-bound rather than row-bound, so this guard is
       effectively dormant in v0.5.3 and will be revisited in v0.5.4 after
       the polyhandler redesign).
    -- Dead df1.drop("distance") removed from gebco_app.py; invariant now
       asserted inside polyhandler() so future regressions are caught.
    -- GEBCO_DASK_POOL_SIZE env knob (default 4; preserves v0.5.2 behaviour
       byte-for-byte). Ablation toward default 0 planned post-merge per spec.
    -- Structured app-level logging via QueueHandler + QueueListener with
       idempotent configure / shutdown wired into lifespan. One JSON log
       line per request at INFO; errors and slow (>1000 ms) requests escalate
       to WARNING regardless of level. Envs GEBCO_LOG_LEVEL (default WARNING),
       GEBCO_LOG_SAMPLE_RATE, GEBCO_SLOW_REQUEST_MS.
    -- numarr_query_validator raises ValueError instead of returning the
       legacy "Format Error" string sentinel.
    -- dev2026/scripts/convert_incremental.py default codec flipped to blosc;
       zlib path now requires explicit --allow-slow-zlib (anti-foot-gun for
       future GEBCO release upgrades).
    -- New top-level tests/ tree with pytest fixtures; sandbox-runnable
       unit tests pass (modes / validation / jsonsrc / lon360 / logger /
       xmeridian), and the full Mac run `uv run --group dev pytest tests/`
       passes including the polars-dependent suites (zprofile / polyhandler /
       bbox guard).
    -- Deferred to follow-up:
       * H9 — np.append → list accumulation in zprofile/xmeridian hot paths
         (~70 call sites; perf-only refactor, needs benchmark vs real Zarr
         before merging).
       * S1 bbox sizing probe ran on Mac and Phase D/Phase D2 were recorded;
         results triggered the v0.5.4 performance plan rather than an
         immediate polygon-cap tightening.
       * H10 dask-pool default-off flip after VM37 staging soak.
    -- Spec: specs/v0.5.3_hardening_checklist_v2.md (round-2 reviewed).

#### ver 0.5.4 Performance hardening — H9 + polygon redesign

    Branch: gebco_2026_perf_v054. Spec: specs/v0.5.4_performance_hardening_plan.md.

    Status: implementation + Mac-side measurements complete on branch.

    -- W1 / H9: Replace np.append accumulators in src/xmeridian.crossBoundary
       and the multi-point branch of src/zprofile.zprofile with Python-list
       buffers + a single np.asarray at the loop tail. Drops the long-polyline
       hot path from O(n²) to amortised O(n).
    -- W2-B round 4 (BLOCKER, from live A/B test): The unconditional
       `gc.collect()` introduced in round 3 caused a ~3-4× polygon
       regression for small / single-batch polygons in the live deploy.
       Measured against ecodata.odb.ntu.edu.tw (v0.5.2) from
       api.odb.ntu.edu.tw (v0.5.4), with 3-trial median:
         * 0.5° Taiwan polygon, sample=1, mode=zonly:
             v0.5.4=117ms  v0.5.2=41ms  (data byte-identical)
         * 0.5° Taiwan polygon, sample=5, mode=zonly:
             v0.5.4=68ms   v0.5.2=17ms  (data byte-identical)
         * 1°x0.5° cross-180 Fiji polygon, sample=5, mode=zonly:
             v0.5.4=132ms  v0.5.2=33ms  (data byte-identical)
       Root cause: gc.collect on a busy gunicorn worker walks the entire
       heap (xarray + dask + polars + zarr objects) — ~50ms per call.
       For single-batch polygons there's nothing to collect (contains_xy
       doesn't accumulate Python Point objects), so the gc is pure waste.
       Fix: pre-compute `multi_batch = n_batches > 1` and gate the gc on
       it, so cross-180 / very-large bbox (D2 cross180_thin) still get
       RSS protection but the common small-polygon path matches v0.5.2
       latency. Live A/B harness is now preserved in-repo at
       `dev2026/scripts/api_compare_v054_vs_v052.py` for future deploy
       verification and public-endpoint comparisons.
    -- W1 / H9 round 4: Replace `np.absolute(scalar)` with builtin `abs()`
       in zprofile + xmeridian hot loops (3x faster per-call on scalars;
       numpy wraps the value before computing absolute). Adds
       `dev2026/scripts/profile_sparse_polyline.py` for stage-level wall
       decomposition.
    -- W1 / H9 round 3: Swap geopy.distance.geodesic for pyproj.Geod.inv
       (`src.zprofile._seg_km`). Both use the same WGS84/Karney geographiclib
       algorithm internally so distance values are byte-equal (verified to
       3.64e-12 km / 3.6 picometres max delta over 0.1-20000 km test pairs).
       pyproj scalar call is ~74× faster than geopy.geodesic per call
       (1.48 ms vs 110 ms for 4800 calls on a sandbox aarch64 venv) because
       pyproj wraps PROJ's optimised C implementation instead of geopy's
       pure-Python class+method stack on top of geographiclib. Mac final
       benchmark against `main`:
         * sparse style `206.26 ms → 22.94 ms` (`8.99×`)
         * dense style  `237.44 ms → 15.71 ms` (`15.11×`)
       Adds pyproj>=3.6,<4 to root pyproject.toml runtime deps.
       Sandbox correctness gate: tests/test_xmeridian.py 13/13 pass
       (including new long_alternating + long_polyline shape cases).
       Mac correctness gate (TODO): verify_polygon_meridian.py T5/T6
       byte-equal vs v0.5.3 + benchmark_long_polyline.py target ≥ 3× median speedup.
    -- W1 H13 follow-up: gebco_app.gebco() now requests dataframe mode
       internally and reads df.height directly for the structured log line,
       removing the json.loads(response.body) round-trip the v0.5.3 ship
       carried as a documented stop-gap.
    -- W2-B: src/polyhandler.process_polygon_part now batches the
       meshgrid + shapely.contains_xy pipeline on a **cell-count budget**
       (default 1e6 cells/batch, env knob GEBCO_POLYGON_BATCH_CELLS).
       Batch height auto-derives so thin polygons (where row-count
       batching from the earlier v0.5.4 draft was a no-op) still get
       fragmented. Preserves exact contains-on-cell-centre semantics →
       polygon outputs byte-equal to v0.5.3. Includes a cheap lat-extent
       prefilter that drops whole batches outside the polygon's bbox.
    -- W2-B round 3: Replace shapely.points + shapely.contains with
       shapely.contains_xy. The latter vectorises directly from numpy
       coordinate arrays into GEOS without constructing one Python
       Point object per cell — ~12× faster wall (29.5 ms vs 358.6 ms on
       a 1M-point batch in sandbox) AND drops per-batch peak RSS from
       ~80 MB of Point PyObjects to ~8 MB of mask+meshgrid scratch.
       Mask output is byte-equal to the pre-round-3 implementation.
    -- W2-B round 3: Add gc.collect() at end of each row-batch loop to
       force immediate reclaim of GEOS / numpy temporaries before the
       next batch allocates. `del` alone doesn't always run cycle
       collection on shapely's internal references, which let the
       v0.5.3-era ~80 MB-per-batch temporaries accumulate into ~2 GB
       across 24 batches on the D2 cross180_thin case. With gc.collect
       per batch, peak RSS is bounded to one batch's working set.
    -- W2-B round 4: Prepare the polygon geometry once per request half
       before calling `shapely.contains_xy`, and remove the leftover
       `print("Got geometry ...")` hot-path debug output. This is what
       finally moved `thin_ribbon` from a borderline `0.52–0.56 s`
       fail into a stable `0.121 s` pass on Mac.
    -- New env knob: GEBCO_POLYGON_BATCH_CELLS (default `1_000_000`).
       Renamed from the v0.5.4 draft's GEBCO_POLYGON_ROW_BATCH after
       codex measurements showed a row-count knob was moot for thin
       polygons at production sample=5 (the whole polygon fit in one
       batch). The cell-budget knob actually bites on the D2 cross180_thin
       worst case at sample=1.
    -- New verification artefacts:
       * dev2026/scripts/profile_cross180_thin.py — required §5 pre-work
         cProfile spike (--repo-root respected per codex's fix), runs
         the D2 archetype under cProfile and dumps pstats + top frames
         for PR2 reviewer baseline.
       * dev2026/scripts/benchmark_long_polyline.py — P-Bench-1 driver
         with --style sparse|dense. Sparse style (2-vertex wide span)
         exercises the asymptotic O(n²) → O(n) regime the plan §8 ≥3×
         target refers to; dense style (n input vertices, small span)
         tests segment-level appends. Default is sparse so the §8
         acceptance is measurable.
       * dev2026/scripts/benchmark_polygon_shapes.py — P-Bench-2 driver
         with concrete §12 acceptance thresholds (thin ≤ 0.5 s, coastal
         ≤ 1.5 s, cross180_thin ≤ 30 s + ≤ 2000 MB RSS). Shapes are
         built from `probe_polygon_cap_shapes.py`'s D2 constructors
         (`_dense_rect`, `_thin_rect`, `_coastal_jagged`,
         `_cross180_parts`) so mask ratios match D2 (~0.04 thin, ~0.6
         coastal, ~0.07 cross180) — rectangles were used in an earlier
         v0.5.4 draft but inflated harvest RSS by ~10× because mask=1.0.
         Defaults to --sample 1 so the plan thresholds (which were
         derived from D2's sample=1 conditions) actually apply.
       * verify_polygon_meridian.py T7 — D2 cross180_thin regression case.
    -- TESTING.md Phase F repurposed for H9 long-polyline bench; new
       Phase G for polygon redesign + cProfile snapshot + T7 result;
       new Phase H for the §6 PR3 cap-revisit decision.
    -- Mac verification completed:
       * `uv run --group dev pytest tests/ -q` PASS
       * `verify_polygon_meridian.py` PASS (T1–T7)
       * `verify_polyhandler_endpoint.py` PASS
       * `benchmark_polygon_shapes.py --trials 3` PASS for all archetypes
         (`thin_ribbon 0.121 s / 5.8 MB`, `coastal 0.127 s / 51.8 MB`,
         `cross180_thin 4.115 s / 203.6 MB`)
       * Phase H decision: default `GEBCO_MAX_POLYGON_CELLS` tightened
         from provisional `2e9` to `5e7`.

#### ver 0.5.5 Line / MultiLine sparse-read redesign + observability

    -- Add `src/line_planner.py` as the single source of truth for the
       sample=1 line-cell walk used by `zprofile()` and the dev2026
       investigation / prototype scripts.
    -- Integrate a first-pass sparse line reader into `src.zprofile`
       for `mode=zonly` line / MultiLineString requests. Large line
       transects now read only touched chunks instead of materialising
       the entire enclosing bbox before gathering touched cells.
    -- Cache a raw `zarr.Array` handle for `elevation` in FastAPI
       lifespan (`config.elev_zarr`) so the production sparse reader
       reaches parity with the raw prototype path (~21.6–21.8 ms
       read-stage on q5/q8 benchmark cases).
    -- Add new env knobs:
       * `GEBCO_MAX_LINE_CHUNKS` (default `64`) for sparse line guard
       * `GEBCO_LINE_SPARSE_MIN_CELLS` (default `1_000_000`) for the
         short-line fallback to the legacy bbox-materialise path
    -- `BboxTooLarge` now carries `kind` (`bbox_cells`, `line_chunks`,
       `polygon_cells`), and HTTP 413 responses include that kind for
       client-side branching and log aggregation.
    -- Add W3 observability plumbing:
       * `zprofile(..., stats_out=...)`
       * `polyhandler(..., stats_out=...)`
       * per-request structured logs now optionally include `line_stats`
         (sampled by `GEBCO_LOG_SAMPLE_RATE`)
       * MultiLineString aggregates include `multi_parts`,
         `touched_chunks_sum/max`, `bbox_subset_cells_sum`,
         `projected_bytes_sum/max`, `output_rows_sum/max`, `any_sparse`
    -- New tests:
       * `tests/test_line_planner.py`
       * `tests/test_line_observability.py`
       * sparse fallback / `kind` assertions in zprofile + bbox guard tests
    -- Investigation / prototype artefacts added:
       * `dev2026/scripts/investigate_line_path_caps.py`
       * `dev2026/scripts/prototype_sparse_line_read.py`
       * `specs/v0.5.5_line_path_redesign_plan.md`
    -- Verification:
       * targeted pytest (`line_planner`, `zprofile`, `bbox_guard`,
         `line_observability`, `polyhandler`) PASS
       * `verify_polygon_meridian.py` PASS
       * `verify_polyhandler_endpoint.py` PASS
    -- Production rollout outcome (`2026-05-31`):
       * VM34 (`ecodata`) and VM37 (`api.odb`) are both now on branch
         `gebco_2026_perf_v054`, revision `ad4179c`, served from the
         root repo via pm2 → `./.venv/bin/gunicorn`
       * the original near-cap `Q8` diagonal transect now returns `200`
         on both public sites instead of the v0.5.4-era premature `413`
       * rollout caveat learned the hard way: if `ps -ef` still shows a
         stale `/home/odbadmin/.pyenv/.../gunicorn` bound to `8013`, you
         are not testing the intended deployment even if the repo checkout
         is correct
       * `dev2026/scripts/api_compare_v054_vs_v052.py` is now a generic
         public A/B smoke harness despite its historical filename
