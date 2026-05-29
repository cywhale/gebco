# Phase D2 — Polygon Cap Probe for Real Frontend-Like Shapes

## Goal

Phase D showed that a **square bbox** is too pessimistic to directly set
`MAX_POLYGON_CELLS`. The server-side cap applies to **raw bbox cells**,
while the frontend constraint applies to **returned rows** after
`sample=5`. Those are different quantities.

Phase D2 answers the missing question:

> For polygon shapes that resemble real frontend usage, how large can
> `raw_bbox_cells` get before polygon-mode wall time / RSS becomes
> operationally unsafe?

The result should let us decide whether:

1. keep `MAX_POLYGON_CELLS` at the current generous fallback,
2. lower it safely, or
3. replace the single raw-bbox cap with a smarter rule later.


## Why D2 Is Needed

Phase D square probes measured the internal polygon path:

* `meshgrid`
* `shapely.points(...)`
* `shapely.contains(...)`
* boolean mask
* masked `elevation`

That is useful for identifying the worst-case memory slope, but square
bboxes overstate the cost for many real polygons because:

* the frontend often sends **thin** or **coastal** polygons,
* returned rows depend on `mask_ratio`, and
* server cost depends on both `raw_bbox_cells` and how much of that bbox
  becomes live arrays during masking.


## Design Principles

Phase D2 should be:

* **shape-aware**: cover the polygon archetypes users actually draw.
* **bounded**: do not brute-force huge payloads or full output dumps.
* **binary-search based**: find the knee with ~4–6 probes per shape, not
  linear sweeps.
* **summary-only**: print one row per probe with small scalar metrics.


## Metrics

For each probe record only these scalars:

* `shape_id`
* `raw_bbox_cells`
* `mask_ratio`
* `returned_rows_sample1`
* `estimated_rows_sample5`
* `wall_s`
* `rss_delta_mb`
* `status`
  * `ok`
  * `slow`
  * `rss_high`
  * `both`

Definitions:

* `raw_bbox_cells = bbox_width_deg * bbox_height_deg * arc * arc`
* `mask_ratio = inside_polygon_cells / raw_bbox_cells`
* `returned_rows_sample1 = inside_polygon_cells`
* `estimated_rows_sample5 ≈ inside_polygon_cells / 25`
  * This is good enough for cap sizing; no need to materialise a second
    `sample=5` run for every probe.


## Archetypes to Probe

Use 4 archetypes only. That is enough to expose the cap tradeoff without
turning the probe into a full benchmark suite.

### A1. Dense rectangle

Purpose:

* sanity baseline
* near-maximal `mask_ratio` (`~0.8–1.0`)

Shape:

* axis-aligned rectangle filling most of its bbox

Expected:

* close to Phase D square behaviour

### A2. Thin rectangle

Purpose:

* closest model of the "large bbox, modest returned rows" case
* directly challenges whether `MAX_POLYGON_CELLS≈1e7` is too strict

Shape:

* width much larger than height
* target `mask_ratio ~0.05–0.15`

Suggested family:

* aspect ratios `20:1`, `40:1`, `80:1`

### A3. Coastal / jagged polygon

Purpose:

* mimic a hand-drawn coastal polygon with moderate sparsity

Shape:

* start from a rectangle
* add 8–16 vertices of low-amplitude zig-zag along one or two sides

Expected:

* `mask_ratio ~0.3–0.6`

### A4. Cross-180 thin polygon

Purpose:

* ensure the split-at-180 path is not materially worse than a same-sized
  non-crossing thin polygon

Shape:

* thin rectangle centered on 180°
* same aspect ratio family as A2


## Search Strategy

Do **not** sweep many sizes. Use bounded search.

For each archetype:

1. start with a safe lower target, e.g. `raw_bbox_cells = 1e6`
2. double until one of these triggers:
   * `wall_s > 2.0`
   * `rss_delta_mb > 1200`
   * `estimated_rows_sample5 > 1_000_000`
   * hard ceiling reached (e.g. `raw_bbox_cells = 2.5e8`)
3. binary-search between the last safe point and first triggered point
   for 2–3 iterations

This keeps each archetype to roughly 5–7 probes.

Total expected runs:

* `4 archetypes × ~6 probes = ~24 probes`

That is small enough to run on a Mac without producing large output or
high token volume.


## Stop Conditions

Abort further growth for a given archetype as soon as any one of these
is true:

* `rss_delta_mb > 1500`
* `wall_s > 5.0`
* `estimated_rows_sample5 > 1_250_000`

These stop conditions are not the cap policy; they are just to keep the
probe itself safe.


## Acceptance Logic

Phase D2 is a decision aid, not a pass/fail test.

Interpretation:

* If **thin** polygons remain operationally safe far above `1e7`
  raw cells while still estimating `< 1e6` sample-5 rows, then the
  current generous polygon cap is justified.
* If thin polygons also become unsafe near `1e7`, then a lower backend
  cap is reasonable and the frontend should not be treated as sufficient
  protection.
* If the safe threshold varies sharply by archetype, then a single
  `MAX_POLYGON_CELLS` is structurally crude and should remain generous
  until a smarter rule is implemented in a later release.


## Recommended Output Table

Record one summary row per archetype:

| shape | safe_up_to_raw_cells | first_bad_raw_cells | mask_ratio_at_knee | est_rows_sample5_at_knee | limiting_factor |
|------|-----------------------|---------------------|--------------------|---------------------------|-----------------|
| dense_rect | TBD | TBD | TBD | TBD | time/rss/rows |
| thin_rect  | TBD | TBD | TBD | TBD | time/rss/rows |
| coastal    | TBD | TBD | TBD | TBD | time/rss/rows |
| cross180_thin | TBD | TBD | TBD | TBD | time/rss/rows |

Then add a short recommendation:

* keep current cap
* lower cap to `X`
* or defer cap tightening pending smarter guard


## Suggested Script

Add a new script rather than overloading `probe_bbox_limits.py`:

* `dev2026/scripts/probe_polygon_cap_shapes.py`

Why separate:

* Phase D is still useful as a worst-case square reference.
* D2 has different semantics: real-shape exploration, not bbox slope.


## Implementation Notes

Keep the script efficient:

* open Zarr once
* reuse a single open-Pacific anchor region for non-crossing shapes
* compute only scalars; do not print or store coordinate arrays
* use `sample=1` internally for accurate `mask_ratio`
* estimate `sample=5` rows by division instead of rerunning the query
* avoid JSON / HTTP entirely; call the polygon internals directly

Use the same internal path Phase D already targeted:

* `subset = ds.sel(...)`
* `np.meshgrid(...)`
* `shapely.points(...)`
* `shapely.contains(...)`
* `inside = mask.sum()`

That is enough to answer the cap question without involving FastAPI or
frontend rendering.


## What D2 Should Not Do

* do not benchmark point/line mode again
* do not emit full result payloads
* do not try dozens of geographic regions
* do not mix in production/public API calls
* do not attempt to pick the final cap from one dense-square metric alone


## Deliverables

1. `dev2026/scripts/probe_polygon_cap_shapes.py`
2. `dev2026/TESTING.md`
   * new subsection: `Phase D2 — polygon cap probe for frontend-like shapes`
3. one short decision note in `AGENTS.md`
   * explain why Phase D square probe was insufficient
   * point future agents to D2 results before changing
     `MAX_POLYGON_CELLS`
