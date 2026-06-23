import numpy as np
import polars as pl
import math
from geopy.distance import geodesic
import pyproj
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, ORJSONResponse
import src.config as config
from src.line_planner import gridded_arcsec, plan_line_cells
from src.modes import parse_modes
from src.xmeridian import crossBoundary

# v0.5.4 W1 H9 round-3 — pyproj.Geod.inv is byte-equal to
# geopy.distance.geodesic (both use WGS84 / Karney geographiclib internally,
# verified to ≤3.64e-12 km / 3.6 picometres max delta over a sweep of
# 0.1 km → ~20000 km test pairs), but the scalar pyproj call is ~74x
# faster than geopy.geodesic because pyproj wraps PROJ's optimised C
# implementation while geopy's geodesic is a pure-Python class+method
# stack on top of geographiclib. Geopy stays imported for `curDist`
# back-compat with the historical dev/ notebook only.
_GEOD_WGS84 = pyproj.Geod(ellps="WGS84")


def _seg_km(lat1, lon1, lat2, lon2) -> float:
    """Single-segment WGS84 geodesic distance, in km.

    Byte-equal drop-in for ``geopy.distance.geodesic((lat1,lon1),(lat2,lon2)).km``
    but ~74x faster per call. Returns ``s12 / 1000`` of pyproj.Geod.inv.
    """
    _, _, s_m = _GEOD_WGS84.inv(lon1, lat1, lon2, lat2)
    return s_m / 1000.0


class BboxTooLarge(Exception):
    """Raised by zprofile / zdata_bbox when the requested bbox would
    materialise more cells than the configured cap. Translates to HTTP
    413 in `gebco_app.py` (v0.5.3 H6)."""

    def __init__(
        self,
        requested: int,
        cap: int,
        *,
        path: str = "line",
        kind: str = "bbox_cells",
    ):
        self.requested = int(requested)
        self.cap = int(cap)
        self.path = path
        self.kind = kind
        super().__init__(
            f"{self.path} {self.kind} too large: "
            f"{self.requested:,} > cap {self.cap:,}; "
            f"narrow your range or "
            f"{'increase sample=N' if self.kind == 'polygon_cells' else 'reduce span'}."
        )


def curDist(loc, dis=np.empty(shape=[0, 1], dtype=float)):
    """Legacy helper kept for back-compat with the dev/ historical notebook.

    The production hot path in ``zprofile()`` now uses ``_push_dist_buf``
    (list-based, no per-call reallocation) — see v0.5.4 W1 / H9.
    """
    if len(loc) < 2:
        return 0  # None
    lk = len(loc) - 1
    return np.append(
        dis, geodesic((loc[lk - 1, 1], loc[lk - 1, 0]), (loc[lk, 1], loc[lk, 0])).km
    )


def _push_dist_buf(loc_buf, dis_buf):
    """v0.5.4 H9 (round 3): list-based replacement for ``curDist``.

    Reads the last two (lon, lat) tuples from ``loc_buf`` and appends the
    WGS84 geodesic km distance into ``dis_buf`` via ``_seg_km`` (pyproj
    scalar — byte-equal to geopy.distance.geodesic but ~74x faster per
    call). No-op when there are fewer than two points.
    """
    if len(loc_buf) < 2:
        return
    last = loc_buf[-1]
    prev = loc_buf[-2]
    dis_buf.append(_seg_km(prev[1], prev[0], last[1], last[0]))


def _line_chunk_budget(plan, ds) -> tuple[int, int]:
    """Return (touched_chunks, projected_materialized_cells) for a line plan."""
    elev = ds["elevation"]
    lat_chunk, lon_chunk = elev.encoding.get("chunks", (0, 0))
    if not lat_chunk or not lon_chunk or plan.unique_idx.size == 0:
        return 0, 0
    global_lat = plan.unique_idx[:, 0] + plan.mlatbase
    global_lon = plan.unique_idx[:, 1] + plan.mlonbase
    chunk_ids = np.stack((global_lat // lat_chunk, global_lon // lon_chunk), axis=1)
    touched_chunks = np.unique(chunk_ids, axis=0).shape[0]
    projected_cells = int(touched_chunks * lat_chunk * lon_chunk)
    return int(touched_chunks), projected_cells


def _read_sparse_line_values(ds, plan) -> np.ndarray | None:
    """Read only the touched chunks for a planned line and reconstruct order.

    Returns ``None`` when chunk metadata is unavailable so callers can
    fall back to the legacy bbox-materialise path instead of surfacing a
    500 to clients.
    """
    elev = ds["elevation"]
    lat_chunk, lon_chunk = elev.encoding.get("chunks", (0, 0))
    if not lat_chunk or not lon_chunk:
        return None
    if plan.unique_idx.size == 0:
        return np.empty((0,), dtype=elev.dtype)

    global_lat = plan.unique_idx[:, 0] + plan.mlatbase
    global_lon = plan.unique_idx[:, 1] + plan.mlonbase
    chunk_row = global_lat // lat_chunk
    chunk_col = global_lon // lon_chunk
    chunk_ids = np.stack((chunk_row, chunk_col), axis=1)

    by_chunk: dict[tuple[int, int], list[int]] = {}
    for pos, cid in enumerate(map(tuple, chunk_ids)):
        by_chunk.setdefault(cid, []).append(pos)

    uniq_values = np.empty((plan.unique_idx.shape[0],), dtype=elev.dtype)
    raw_elev = getattr(config, "elev_zarr", None)
    for (cr, cc), positions in by_chunk.items():
        lat0 = int(cr * lat_chunk)
        lat1 = min(lat0 + lat_chunk, elev.shape[0])
        lon0 = int(cc * lon_chunk)
        lon1 = min(lon0 + lon_chunk, elev.shape[1])
        if raw_elev is not None:
            chunk = raw_elev[lat0:lat1, lon0:lon1]
        else:
            chunk = elev.isel(lat=slice(lat0, lat1), lon=slice(lon0, lon1)).values
        pos_arr = np.asarray(positions, dtype=np.int32)
        local_lat = global_lat[pos_arr] - lat0
        local_lon = global_lon[pos_arr] - lon0
        uniq_values[pos_arr] = chunk[local_lat, local_lon]
    return uniq_values[plan.inverse]


def _line_chunk_budget_global(ds, global_lat, global_lon):
    """(touched_chunks, projected_cells, (lat_chunk, lon_chunk)) for raw global
    cell indices. Like ``_line_chunk_budget`` but driven by index arrays (the
    legacy line walk's ``idx1`` + base) rather than a ``LinePlan``."""
    elev = ds["elevation"]
    lat_chunk, lon_chunk = elev.encoding.get("chunks", (0, 0))
    if not lat_chunk or not lon_chunk or global_lat.size == 0:
        return 0, 0, (lat_chunk, lon_chunk)
    chunk_ids = np.stack((global_lat // lat_chunk, global_lon // lon_chunk), axis=1)
    touched = int(np.unique(chunk_ids, axis=0).shape[0])
    return touched, int(touched * lat_chunk * lon_chunk), (int(lat_chunk), int(lon_chunk))


def _read_cells_by_chunk(ds, global_lat, global_lon):
    """Gather elevation for global ``(lat, lon)`` cell indices, reading only the
    touched Zarr chunks instead of materialising the enclosing bbox.

    Order-preserving and duplicate-safe (one value per input index, in input
    order). Byte-identical to ``ds["elevation"].values[lat, lon]`` but bounded
    to the touched chunks. This is the same read strategy as the v0.5.5 sparse
    ``zonly`` reader (`_read_sparse_line_values`), generalised so the
    distance/`truncate` (non-`zonly`) line path can share it — the key to not
    over-materialising (and not prematurely 413-ing) cross-180° lines whose
    enclosing bbox spans the whole grid width. Returns ``None`` when chunk
    metadata is unavailable so callers fall back to a dense read.
    """
    elev = ds["elevation"]
    lat_chunk, lon_chunk = elev.encoding.get("chunks", (0, 0))
    if not lat_chunk or not lon_chunk:
        return None
    n = int(global_lat.shape[0])
    if n == 0:
        return np.empty((0,), dtype=elev.dtype)
    out = np.empty((n,), dtype=elev.dtype)
    raw_elev = getattr(config, "elev_zarr", None)
    chunk_row = global_lat // lat_chunk
    chunk_col = global_lon // lon_chunk
    by_chunk: dict[tuple[int, int], list[int]] = {}
    for pos, cid in enumerate(zip(chunk_row.tolist(), chunk_col.tolist())):
        by_chunk.setdefault(cid, []).append(pos)
    for (cr, cc), positions in by_chunk.items():
        lat0 = int(cr * lat_chunk)
        lat1 = min(lat0 + lat_chunk, elev.shape[0])
        lon0 = int(cc * lon_chunk)
        lon1 = min(lon0 + lon_chunk, elev.shape[1])
        if raw_elev is not None:
            chunk = raw_elev[lat0:lat1, lon0:lon1]
        else:
            chunk = elev.isel(lat=slice(lat0, lat1), lon=slice(lon0, lon1)).values
        pos_arr = np.asarray(positions, dtype=np.int64)
        out[pos_arr] = chunk[global_lat[pos_arr] - lat0, global_lon[pos_arr] - lon0]
    return out


def _record_line_stats(
    stats_out: dict | None,
    *,
    use_sparse: bool,
    plan,
    touched_chunks: int,
    projected_cells: int,
    output_rows: int,
) -> None:
    if stats_out is None:
        return
    stats_out.update(
        {
            "use_sparse": bool(use_sparse),
            "bbox_subset_cells": int(plan.bbox_subset_cells),
            "touched_chunks": int(touched_chunks),
            "projected_cells": int(projected_cells),
            "projected_bytes": int(projected_cells * 2),
            "unique_cells": int(plan.unique_idx.shape[0]),
            "output_rows": int(output_rows),
        }
    )


def zdata_bbox(bbox, crosses_180=False, isRight=False, sample=5):
    ds = config.ds  # config.ds is the global variable of zarr dataset
    arc = config.arc
    minx, miny, maxx, maxy = bbox
    # if crosses_180, left polygon's longitude is like maxx = -179.5, minx = -179.999
    # if crosses_180, right polygon's longitude is like maxx= 179.99, minx = 179.5
    lftx = (
        minx - 0.25 / arc
        if not crosses_180 or (crosses_180 and isRight)
        else max(minx - 0.25 / arc, -180 + 0.01 / arc)
    )
    rgtx = (
        min(maxx + 1.5 / arc, 180 - 0.01 / arc)
        if not crosses_180 or (crosses_180 and isRight)
        else maxx + 1.5 / arc
    )
    # v0.5.3 H6: polygon-path bbox guard. Counted on RAW cells (before the
    # sample stride) because that bounds the worst-case allocation: meshgrid
    # + shapely points + boolean mask allocate against the strided subset,
    # but the read path still touches raw_cells / sample² cells. The cap is
    # tuned (see specs/v0.5.3) to leave large headroom for thin polygons
    # at the frontend's 1M-row ceiling.
    raw_cells = (
        (rgtx - lftx) * ((maxy + 1.5 / arc) - (miny - 0.25 / arc)) * arc * arc
    )
    if raw_cells > config.MAX_POLYGON_CELLS:
        raise BboxTooLarge(
            raw_cells,
            config.MAX_POLYGON_CELLS,
            path="polygon",
            kind="polygon_cells",
        )
    # print("Debug left, right to slice: ", lftx, rgtx, " and bbox: ", bbox, " and condition: ", crosses_180, isRight)
    subset_data = ds.sel(
        lon=slice(lftx, rgtx, sample),
        lat=slice(miny - 0.25 / arc, maxy + 1.5 / arc, sample),
    )
    return subset_data


def empty_data():
    _columns = {"longitude": pl.Float64, "latitude": pl.Float64, "z": pl.Float64}
    return pl.DataFrame(schema=_columns)


def zprofile(loni, lati, mode, sample=1, *, stats_out: dict | None = None):
    # global ds #move to config.py
    # global arcsec #15
    # global arc
    # global basex
    # global basey
    # global subsetFlag
    ds = config.ds
    arc = config.arc  # int(3600 / arcsec)  # 15 arc-second
    basex = config.basex  # 180  # -180 - 180 <==> 0 - 360, half is 180
    basey = config.basey  # 90  # -90 - 90 <==> 0 - 180, half is 90
    subsetFlag = True
    format = "default"
    # i.e, output all gridded points along the line; otherwise 'point', output only end-points.
    zonly = False  # don't compute distance function so that can improve speed
    zmode = "line"
    # v0.5.3 H4: substring → set tokens. parse_modes() also tolerates
    # unknown tokens (warning only, not reject) for backward compatibility.
    modes = parse_modes(mode)
    if "zonly" in modes:
        zonly = True
    if "point" in modes:
        zmode = "point"
    if "row" in modes:
        format = "row"
    if "dataframe" in modes:
        format = "dataframe"

    if len(loni) != len(lati):
        if "dataframe" in modes:
            return empty_data()
        else:
            # ds.close() # Now handle in lifespan
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=jsonable_encoder(
                    {"Error": "Check your input of lon/lat should be in equal length"}
                ),
            )

    # May not have exact lon, lat in gridded lon-lat, so need to calculate index
    elif len(loni) == 1:
        lon0 = gridded_arcsec(loni[0], basex, arc)
        lat0 = gridded_arcsec(lati[0], basey, arc)
        mlon0x = ds["lon"][lon0]
        mlat0x = ds["lat"][lat0]
        st1 = ds.sel(lon=mlon0x, lat=mlat0x)
        loc1 = [loni[0], lati[0]]
        # O-2: a single point must honour truncate / lon360 in its echoed
        # coordinates, exactly like the multi-point line path below. Without
        # this, `mode=lon360` returned the [-180,180] grid-lookup longitude
        # instead of the requested [0,360] convention.
        if "truncate" in modes:
            loc1 = [round(float(loc1[0]), 5), round(float(loc1[1]), 5)]
        if "lon360" in modes and loc1[0] < 0:
            loc1 = [loc1[0] + 360, loc1[1]]
        xt1 = np.array([st1["elevation"].values])
        if format == "row" or format == "dataframe":
            if not zonly:
                # df1= pd.DataFrame({"longitude": loc1[:, 0].tolist(),
                df1 = pl.DataFrame(
                    {
                        "longitude": np.array([loc1[0]]).tolist(),
                        "latitude": np.array([loc1[1]]).tolist(),
                        "z": xt1.tolist(),
                        "distance": np.array([0]).tolist(),
                    }
                )
            else:
                df1 = pl.DataFrame(
                    {
                        "longitude": np.array([loc1[0]]).tolist(),
                        "latitude": np.array([loc1[1]]).tolist(),
                        "z": xt1.tolist(),
                    }
                )
        else:
            if not zonly:
                out = jsonable_encoder(
                    {
                        "longitude": np.array([loc1[0]]).tolist(),
                        "latitude": np.array([loc1[1]]).tolist(),
                        "z": xt1.tolist(),
                        "distance": np.array([0]).tolist(),
                    }
                )
            else:
                out = jsonable_encoder(
                    {
                        "longitude": np.array([loc1[0]]).tolist(),
                        "latitude": np.array([loc1[1]]).tolist(),
                        "z": xt1.tolist(),
                    }
                )
    else:
        # v0.5.5 W2 first-pass integration: line / MultiLineString requests
        # in zonly mode can use the sparse chunk-aware reader. Point mode and
        # distance-producing paths stay on the legacy implementation for now.
        if zmode != "point" and zonly:
            plan = plan_line_cells(loni, lati, arc, basex, basey)
            loc1 = plan.loc
            bbox_subset_cells = plan.bbox_subset_cells

            use_sparse = bbox_subset_cells >= config.LINE_SPARSE_MIN_CELLS
            if use_sparse:
                touched_chunks, projected_cells = _line_chunk_budget(plan, ds)
                if config.MAX_LINE_CHUNKS > 0 and touched_chunks > config.MAX_LINE_CHUNKS:
                    raise BboxTooLarge(
                        projected_cells,
                        config.MAX_LINE_CHUNKS
                        * ds["elevation"].encoding["chunks"][0]
                        * ds["elevation"].encoding["chunks"][1],
                        path="line",
                        kind="line_chunks",
                    )
                xt1 = _read_sparse_line_values(ds, plan)
                if xt1 is not None:
                    _record_line_stats(
                        stats_out,
                        use_sparse=True,
                        plan=plan,
                        touched_chunks=touched_chunks,
                        projected_cells=projected_cells,
                        output_rows=plan.loc.shape[0],
                    )
                    if "truncate" in modes:
                        loc1[:, 0] = np.round(loc1[:, 0], 5)
                        loc1[:, 1] = np.round(loc1[:, 1], 5)
                    if "lon360" in modes:
                        loc1[:, 0] = np.where(loc1[:, 0] < 0, loc1[:, 0] + 360, loc1[:, 0])

                    if format == "row" or format == "dataframe":
                        df1 = pl.DataFrame({"longitude": loc1[:, 0], "latitude": loc1[:, 1], "z": xt1})
                    else:
                        out = jsonable_encoder(
                            {"longitude": loc1[:, 0].tolist(), "latitude": loc1[:, 1].tolist(), "z": xt1.tolist()}
                        )
                    if format == "dataframe":
                        return df1
                    if format == "row":
                        return ORJSONResponse(content=df1.to_dicts())
                    return ORJSONResponse(content=out)

        if zmode == "point":
            lonk = loni
            latk = lati
            brks = [-1]
        else:
            ats = crossBoundary(loni, lati)
            lonk = np.asarray(ats[0])
            latk = np.asarray(ats[1])
            brks = ats[2]
            # autoFly = ats[3]
            # dirFlag = ats[4] #deprecated
        # v0.5.4 H9: list-based accumulators. The hot loop below appends
        # tuples here instead of calling ``np.append`` per iteration (which
        # reallocates and copies every time). All three are converted to
        # numpy arrays once after the loop finishes.
        idx1_buf: list = []
        loc1_buf: list = []
        dis1_buf: list = [0.0]
        mlon0 = np.min(lonk) - 1.5 / arc  # to make it smaller
        mlon0 = mlon0 if mlon0 > -basex else -basex + 0.00001
        # mlon1 = np.max(long)
        mlat0 = np.min(latk) - 1.5 / arc
        mlat0 = mlat0 if mlat0 > -basey else -basey + 0.00001
        # v0.5.3 H6 / O-3: the line/point bbox guard now lives at read time
        # (see the chunk-aware read below). Counting cells from the global
        # min/max bbox here over-rejected cross-180° lines: crossBoundary
        # inserts break-points at both ±180, so the bbox spans the full grid
        # width even for a short transect, tripping a spurious 413 in
        # non-zonly modes. The read either gathers only touched chunks
        # (guarded by MAX_LINE_CHUNKS) or, for small lines, does a bounded
        # dense read (guarded by MAX_BBOX_CELLS_LINE).
        # mlat1 = np.max(latg)
        # if do subsetting dataset, reference-0-x,y should be biased
        mlonbase = gridded_arcsec(mlon0, basex, arc) if subsetFlag else 0
        mlatbase = gridded_arcsec(mlat0, basey, arc) if subsetFlag else 0
        # index from gridded_arcsec will change is reference is ds_s1, not ds
        # mlonidx1 = gridded_arcsec(mlon1, basex, arc)
        # mlatidx1 = gridded_arcsec(mlat1, basey, arc)
        preidx = 0
        for brk in brks:
            lonx = lonk[preidx:] if brk == -1 else lonk[preidx : (brk + 1)]
            latx = latk[preidx:] if brk == -1 else latk[preidx : (brk + 1)]
            if (
                len(lonx) == 1
            ):  # only occur because break-points cause a closer-zero end-pt appear in last segment
                lonidx0 = gridded_arcsec(lonx[0], basex, arc)
                latidx0 = gridded_arcsec(latx[0], basey, arc)
                idx1_buf.append((latidx0 - mlatbase, lonidx0 - mlonbase))
                loc1_buf.append((float(lonx[0]), float(latx[0])))
                continue

            for i in range(len(lonx) - 1):
                lonidx0 = gridded_arcsec(lonx[i], basex, arc)
                latidx0 = gridded_arcsec(latx[i], basey, arc)
                lonidx1 = gridded_arcsec(lonx[i + 1], basex, arc)
                latidx1 = gridded_arcsec(latx[i + 1], basey, arc)
                if zmode == "point":
                    idx1_buf.append((latidx0 - mlatbase, lonidx0 - mlonbase))
                    loc1_buf.append((float(lonx[i]), float(latx[i])))
                    if not zonly:
                        dist = _seg_km(latx[i], lonx[i], latx[i + 1], lonx[i + 1])
                        dis1_buf.append(dist)
                    if i == len(lonx) - 2:
                        idx1_buf.append((latidx1 - mlatbase, lonidx1 - mlonbase))
                        loc1_buf.append((float(lonx[i + 1]), float(latx[i + 1])))
                elif lonidx0 == lonidx1 and latidx0 == latidx1:
                    idx1_buf.append((latidx0 - mlatbase, lonidx0 - mlonbase))
                    loc1_buf.append((float(lonx[i]), float(latx[i])))
                    # to match the same length
                    if i >= 1 and not zonly:
                        _push_dist_buf(loc1_buf, dis1_buf)
                else:
                    if lonx[i] == lonx[i + 1]:
                        stepi = -1 if latidx0 > latidx1 else 1
                        rngi = range(latidx0, latidx1 + stepi, stepi)
                        leni = len(rngi)
                        for k, y in enumerate(rngi):
                            locy0 = (y + 1) / arc - basey
                            locy0i = int(locy0)
                            doty0i = (
                                locy0 - locy0i - 0.25 / arc
                            )  # a small bias to make sure it's in grid
                            locy1 = locy0i + doty0i
                            locy1 = (
                                basey
                                if locy1 > basey
                                else (-basey if locy1 < -basey else locy1)
                            )
                            if (
                                (k < (leni - 1))
                                or (stepi == 1 and locy1 < latx[i + 1])
                                or (stepi == -1 and locy1 > latx[i + 1])
                            ):
                                idx1_buf.append((y - mlatbase, lonidx0 - mlonbase))
                                loc1_buf.append((float(lonx[i]), float(locy1)))
                                if (i >= 1 or k >= 1) and not zonly:
                                    _push_dist_buf(loc1_buf, dis1_buf)

                    elif latx[i] == latx[i + 1]:
                        stepi = -1 if lonidx0 > lonidx1 else 1
                        rngi = range(lonidx0, lonidx1 + stepi, stepi)
                        leni = len(rngi)
                        for k, x in enumerate(rngi):
                            locx0 = (x + 1) / arc - basex
                            locx0i = int(locx0)
                            dotx0i = (
                                locx0 - locx0i - 0.25 / arc
                            )  # a small bias to make sure it's in grid
                            locx1 = locx0i + dotx0i
                            locx1 = (
                                basex
                                if locx1 > basex
                                else (-basex if locx1 < -basex else locx1)
                            )
                            if (
                                (k < (leni - 1))
                                or (stepi == 1 and locx1 < lonx[i + 1])
                                or (stepi == -1 and locx1 > lonx[i + 1])
                            ):
                                idx1_buf.append((latidx0 - mlatbase, x - mlonbase))
                                loc1_buf.append((float(locx1), float(latx[i])))
                                if (i >= 1 or k >= 1) and not zonly:
                                    _push_dist_buf(loc1_buf, dis1_buf)

                    else:
                        m = (latx[i + 1] - latx[i]) / (lonx[i + 1] - lonx[i])
                        b = latx[i] - m * lonx[i]  # y = mx + b
                        if abs(m) <= 1:
                            lidx0 = lonidx0
                            lidx1 = lonidx1
                        else:
                            lidx0 = latidx0
                            lidx1 = latidx1

                        stepi = -1 if lidx0 > lidx1 else 1
                        rngi = range(lidx0, lidx1 + stepi, stepi)
                        leni = len(rngi)
                        for k, s in enumerate(rngi):
                            if (
                                k == 0
                            ):  # should consider internal node not repeated twice
                                idx1_buf.append(
                                    (latidx0 - mlatbase, lonidx0 - mlonbase)
                                )
                                loc1_buf.append((float(lonx[i]), float(latx[i])))
                                if i >= 1 and not zonly:
                                    _push_dist_buf(loc1_buf, dis1_buf)

                            else:
                                if abs(m) <= 1:
                                    locx0 = (s + 1) / arc - basex
                                    locx0i = int(locx0)
                                    dotx0i = (
                                        locx0 - locx0i - 0.25 / arc
                                    )  # a small bias to make sure it's in grid
                                    locx1 = locx0i + dotx0i
                                    locx1 = (
                                        basex
                                        if locx1 > basex
                                        else (-basex if locx1 < -basex else locx1)
                                    )
                                    locy1 = m * locx1 + b
                                    locy1 = (
                                        basey
                                        if locy1 > basey
                                        else (-basey if locy1 < -basey else locy1)
                                    )
                                    if (
                                        (k < (leni - 1))
                                        or (stepi == 1 and locx1 < lonx[i + 1])
                                        or (stepi == -1 and locx1 > lonx[i + 1])
                                    ):
                                        y = gridded_arcsec(locy1, basey, arc)
                                        idx1_buf.append(
                                            (y - mlatbase, s - mlonbase)
                                        )
                                        loc1_buf.append((float(locx1), float(locy1)))
                                        if not zonly:
                                            _push_dist_buf(loc1_buf, dis1_buf)
                                else:
                                    locy0 = (s + 1) / arc - basey
                                    locy0i = int(locy0)
                                    doty0i = locy0 - locy0i - 0.25 / arc
                                    locy1 = locy0i + doty0i
                                    locy1 = (
                                        basey
                                        if locy1 > basey
                                        else (-basey if locy1 < -basey else locy1)
                                    )
                                    locx1 = (locy1 - b) / m
                                    locx1 = (
                                        basex
                                        if locx1 > basex
                                        else (-basex if locx1 < -basex else locx1)
                                    )
                                    if (
                                        (k < (leni - 1))
                                        or (stepi == 1 and locy1 < latx[i + 1])
                                        or (stepi == -1 and locy1 > latx[i + 1])
                                    ):
                                        x = gridded_arcsec(locx1, basex, arc)
                                        idx1_buf.append(
                                            (s - mlatbase, x - mlonbase)
                                        )
                                        loc1_buf.append((float(locx1), float(locy1)))
                                        if not zonly:
                                            _push_dist_buf(loc1_buf, dis1_buf)

                if zmode != "point" and i == len(lonx) - 2:
                    idx1_buf.append((latidx1 - mlatbase, lonidx1 - mlonbase))
                    loc1_buf.append((float(lonx[i + 1]), float(latx[i + 1])))
                    if not zonly:
                        _push_dist_buf(loc1_buf, dis1_buf)
            # end loop-i
            if brk != -1:
                if not zonly:
                    dist = _seg_km(
                        latk[brk], lonk[brk], latk[brk + 1], lonk[brk + 1]
                    )
                    dis1_buf.append(dist)
                preidx = brk + 1
        # end loop-brks
        # v0.5.4 H9: convert buffers to numpy arrays once, matching the
        # shapes/dtypes the downstream slicing and DataFrame builds expect.
        if idx1_buf:
            # NOTE: must be a 64-bit index dtype. The v0.5.4 H9 refactor used
            # np.int16 here, but bbox-relative cell indices reach ~86399 for
            # cross-180° lines (crossBoundary inserts break-points at both
            # ±180, so mlonbase≈0 and the subset spans the full grid width).
            # int16 (max 32767) silently wraps those to garbage columns, so
            # the API read the wrong ocean. The pre-H9 np.append code was
            # correct only because np.append promoted the array to int64.
            idx1 = np.asarray(idx1_buf, dtype=np.int64)
        else:
            idx1 = np.empty(shape=(0, 2), dtype=np.int64)
        if loc1_buf:
            loc1 = np.asarray(loc1_buf, dtype=float)
        else:
            loc1 = np.empty(shape=(0, 2), dtype=float)
        dis1 = np.asarray(dis1_buf, dtype=float)
        # mlon0 = np.min(lonx) #may cause slice offset to mlonbase, an offset +-1
        mlon1 = np.max(lonk) + 1.5 / arc  # to make it larger
        mlon1 = mlon1 if mlon1 <= basex else basex - 0.00001
        # mlat0 = np.min(latx) #may cause slice offset to mlatbase, an offset +-1
        mlat1 = np.max(latk) + 1.5 / arc
        mlat1 = mlat1 if mlat1 <= basey else basey - 0.00001
        # O-3 / v0.5.5+ : read elevation for the planned cells via a
        # touched-chunk gather instead of materialising the whole enclosing
        # bbox. For a cross-180° line the bbox spans the full grid width even
        # when the line is short, so the dense read both wasted memory and made
        # the (removed) early bbox guard reject legitimate short transects with
        # 413. The gather is byte-identical to ds_s1.values[idx] but bounded to
        # the touched chunks, guarded by MAX_LINE_CHUNKS like the zonly sparse
        # path. Small lines (bbox < LINE_SPARSE_MIN_CELLS) keep the cheaper
        # dense read, which the legacy MAX_BBOX_CELLS_LINE guard still bounds.
        # Use the unclamped max estimates (as the original early guard did) so
        # the cell count is not distorted by the ±base clamping applied to
        # mlon1/mlat1 for the dense slice below.
        bbox_cells = (
            (np.max(lonk) + 1.5 / arc - mlon0)
            * (np.max(latk) + 1.5 / arc - mlat0)
            * arc
            * arc
        )
        global_lat = idx1[:, 0].astype(np.int64) + mlatbase
        global_lon = idx1[:, 1].astype(np.int64) + mlonbase
        xt1 = None
        if subsetFlag and sample == 1 and bbox_cells >= config.LINE_SPARSE_MIN_CELLS:
            touched, projected, (lat_chunk, lon_chunk) = _line_chunk_budget_global(
                ds, global_lat, global_lon
            )
            if lat_chunk and lon_chunk:
                if config.MAX_LINE_CHUNKS > 0 and touched > config.MAX_LINE_CHUNKS:
                    raise BboxTooLarge(
                        projected,
                        config.MAX_LINE_CHUNKS * lat_chunk * lon_chunk,
                        path="line",
                        kind="line_chunks",
                    )
                xt1 = _read_cells_by_chunk(ds, global_lat, global_lon)
        if xt1 is None:
            if bbox_cells > config.MAX_BBOX_CELLS_LINE:
                raise BboxTooLarge(
                    bbox_cells,
                    config.MAX_BBOX_CELLS_LINE,
                    path="line",
                    kind="bbox_cells",
                )
            mlon0x = ds["lon"][mlonbase].item()
            mlat0x = ds["lat"][mlatbase].item()
            ds_s1 = (
                ds.sel(lon=slice(mlon0x, mlon1, sample), lat=slice(mlat0x, mlat1, sample))
                if subsetFlag
                else ds
            )
            xt1 = ds_s1["elevation"].values[tuple(idx1.T)]
            ds_s1.close()
        if stats_out is not None:
            plan = plan_line_cells(loni, lati, arc, basex, basey)
            touched_chunks, projected_cells = _line_chunk_budget(plan, ds)
            _record_line_stats(
                stats_out,
                use_sparse=False,
                plan=plan,
                touched_chunks=touched_chunks,
                projected_cells=projected_cells,
                output_rows=plan.loc.shape[0],
            )

        # 202502 add truncated mode: Apply truncation if "truncate" mode is enabled
        if "truncate" in modes:
            loc1[:, 0] = np.round(loc1[:, 0], 5)
            loc1[:, 1] = np.round(loc1[:, 1], 5)

        # Ensure ALL longitudes are in 0-360 range if "lon360" mode is set
        if "lon360" in modes:
            loc1[:, 0] = np.where(loc1[:, 0] < 0, loc1[:, 0] + 360, loc1[:, 0])    

        if format == "row" or format == "dataframe":
            if not zonly:
                df1 = pl.DataFrame(
                    {
                        "longitude": loc1[:, 0],  # .tolist(),
                        "latitude": loc1[:, 1],  # .tolist(),
                        "z": xt1,  # .tolist(),
                        "distance": dis1,
                    }
                )
            else:
                df1 = pl.DataFrame(
                    {"longitude": loc1[:, 0], "latitude": loc1[:, 1], "z": xt1}
                )
        else:
            if not zonly:
                out = jsonable_encoder(
                    {
                        "longitude": loc1[:, 0].tolist(),
                        "latitude": loc1[:, 1].tolist(),
                        "z": xt1.tolist(),
                        "distance": dis1.tolist(),
                    }
                )
            else:
                out = jsonable_encoder(
                    {
                        "longitude": loc1[:, 0].tolist(),
                        "latitude": loc1[:, 1].tolist(),
                        "z": xt1.tolist(),
                    }
                )

    if format == "dataframe":
        return df1

    if format == "row":
        out = df1.to_dicts()  # by polars

    return ORJSONResponse(content=out)
