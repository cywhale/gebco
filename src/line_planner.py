from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.xmeridian import crossBoundary


def gridded_arcsec(x: float, base: int = 90, arc: int = 240) -> int:
    idx = (int(x) + base) * arc + np.ceil((x - int(x)) * arc)
    idx = int(idx)
    return idx - 1 if idx > 0 else 0


@dataclass(frozen=True)
class LinePlan:
    """Planned cell walk for a line / polyline at sample=1.

    `idx` stores the ordered cell indices relative to `(mlatbase, mlonbase)`,
    matching the indexing convention used by `zprofile()` when slicing a bbox
    subset and then gathering `elevation[idx]`.
    """

    idx: np.ndarray
    loc: np.ndarray
    unique_idx: np.ndarray
    inverse: np.ndarray
    breakpoint_indices: np.ndarray
    mlatbase: int
    mlonbase: int
    bbox_lon0: float
    bbox_lat0: float
    bbox_lon1: float
    bbox_lat1: float
    bbox_height: int
    bbox_width: int
    bbox_subset_cells: int
    inner_iters: int


def plan_line_cells(loni: np.ndarray, lati: np.ndarray, arc: int, basex: int, basey: int) -> LinePlan:
    """Plan the exact cell walk for a line/polyline.

    This is the single source of truth for the line-walk geometry used by:
    - production `src.zprofile`
    - measurement scripts under `dev2026/scripts/`
    - future sparse-read implementations
    """

    lon_arr = np.asarray(loni, dtype=float)
    lat_arr = np.asarray(lati, dtype=float)
    if lon_arr.shape != lat_arr.shape:
        raise ValueError("lon/lat must have equal length")
    if lon_arr.size == 0:
        return LinePlan(
            idx=np.empty((0, 2), dtype=np.int32),
            loc=np.empty((0, 2), dtype=float),
            unique_idx=np.empty((0, 2), dtype=np.int32),
            inverse=np.empty((0,), dtype=np.int32),
            breakpoint_indices=np.empty((0,), dtype=np.int32),
            mlatbase=0,
            mlonbase=0,
            bbox_lon0=0.0,
            bbox_lat0=0.0,
            bbox_lon1=0.0,
            bbox_lat1=0.0,
            bbox_height=0,
            bbox_width=0,
            bbox_subset_cells=0,
            inner_iters=0,
        )

    ats = crossBoundary(lon_arr, lat_arr)
    lonk = np.asarray(ats[0], dtype=float)
    latk = np.asarray(ats[1], dtype=float)
    brks = np.asarray(ats[2], dtype=np.int32)

    mlon0 = max(float(np.min(lonk)) - 1.5 / arc, -basex + 0.00001)
    mlat0 = max(float(np.min(latk)) - 1.5 / arc, -basey + 0.00001)
    mlon1 = min(float(np.max(lonk)) + 1.5 / arc, basex - 0.00001)
    mlat1 = min(float(np.max(latk)) + 1.5 / arc, basey - 0.00001)
    mlonbase = gridded_arcsec(mlon0, basex, arc)
    mlatbase = gridded_arcsec(mlat0, basey, arc)

    idx_buf: list[tuple[int, int]] = []
    loc_buf: list[tuple[float, float]] = []
    preidx = 0
    inner_iters = 0

    for brk in brks:
        brk = int(brk)
        lonx = lonk[preidx:] if brk == -1 else lonk[preidx : (brk + 1)]
        latx = latk[preidx:] if brk == -1 else latk[preidx : (brk + 1)]
        if len(lonx) == 1:
            lonidx0 = gridded_arcsec(float(lonx[0]), basex, arc)
            latidx0 = gridded_arcsec(float(latx[0]), basey, arc)
            idx_buf.append((latidx0 - mlatbase, lonidx0 - mlonbase))
            loc_buf.append((float(lonx[0]), float(latx[0])))
            continue

        for i in range(len(lonx) - 1):
            lonidx0 = gridded_arcsec(float(lonx[i]), basex, arc)
            latidx0 = gridded_arcsec(float(latx[i]), basey, arc)
            lonidx1 = gridded_arcsec(float(lonx[i + 1]), basex, arc)
            latidx1 = gridded_arcsec(float(latx[i + 1]), basey, arc)

            if lonidx0 == lonidx1 and latidx0 == latidx1:
                idx_buf.append((latidx0 - mlatbase, lonidx0 - mlonbase))
                loc_buf.append((float(lonx[i]), float(latx[i])))
            elif lonx[i] == lonx[i + 1]:
                stepi = -1 if latidx0 > latidx1 else 1
                rngi = range(latidx0, latidx1 + stepi, stepi)
                leni = len(rngi)
                inner_iters += leni
                for k, y in enumerate(rngi):
                    locy0 = (y + 1) / arc - basey
                    locy0i = int(locy0)
                    doty0i = locy0 - locy0i - 0.25 / arc
                    locy1 = locy0i + doty0i
                    locy1 = basey if locy1 > basey else (-basey if locy1 < -basey else locy1)
                    if (
                        (k < (leni - 1))
                        or (stepi == 1 and locy1 < latx[i + 1])
                        or (stepi == -1 and locy1 > latx[i + 1])
                    ):
                        idx_buf.append((y - mlatbase, lonidx0 - mlonbase))
                        loc_buf.append((float(lonx[i]), float(locy1)))
            elif latx[i] == latx[i + 1]:
                stepi = -1 if lonidx0 > lonidx1 else 1
                rngi = range(lonidx0, lonidx1 + stepi, stepi)
                leni = len(rngi)
                inner_iters += leni
                for k, x in enumerate(rngi):
                    locx0 = (x + 1) / arc - basex
                    locx0i = int(locx0)
                    dotx0i = locx0 - locx0i - 0.25 / arc
                    locx1 = locx0i + dotx0i
                    locx1 = basex if locx1 > basex else (-basex if locx1 < -basex else locx1)
                    if (
                        (k < (leni - 1))
                        or (stepi == 1 and locx1 < lonx[i + 1])
                        or (stepi == -1 and locx1 > lonx[i + 1])
                    ):
                        idx_buf.append((latidx0 - mlatbase, x - mlonbase))
                        loc_buf.append((float(locx1), float(latx[i])))
            else:
                m = (latx[i + 1] - latx[i]) / (lonx[i + 1] - lonx[i])
                b = latx[i] - m * lonx[i]
                lidx0, lidx1 = (lonidx0, lonidx1) if abs(m) <= 1 else (latidx0, latidx1)
                stepi = -1 if lidx0 > lidx1 else 1
                rngi = range(lidx0, lidx1 + stepi, stepi)
                leni = len(rngi)
                inner_iters += leni
                for k, s in enumerate(rngi):
                    if k == 0:
                        idx_buf.append((latidx0 - mlatbase, lonidx0 - mlonbase))
                        loc_buf.append((float(lonx[i]), float(latx[i])))
                    else:
                        if abs(m) <= 1:
                            locx0 = (s + 1) / arc - basex
                            locx0i = int(locx0)
                            dotx0i = locx0 - locx0i - 0.25 / arc
                            locx1 = locx0i + dotx0i
                            locx1 = basex if locx1 > basex else (-basex if locx1 < -basex else locx1)
                            locy1 = m * locx1 + b
                            locy1 = basey if locy1 > basey else (-basey if locy1 < -basey else locy1)
                            if (
                                (k < (leni - 1))
                                or (stepi == 1 and locx1 < lonx[i + 1])
                                or (stepi == -1 and locx1 > lonx[i + 1])
                            ):
                                y = gridded_arcsec(locy1, basey, arc)
                                idx_buf.append((y - mlatbase, s - mlonbase))
                                loc_buf.append((float(locx1), float(locy1)))
                        else:
                            locy0 = (s + 1) / arc - basey
                            locy0i = int(locy0)
                            doty0i = locy0 - locy0i - 0.25 / arc
                            locy1 = locy0i + doty0i
                            locy1 = basey if locy1 > basey else (-basey if locy1 < -basey else locy1)
                            locx1 = (locy1 - b) / m
                            locx1 = basex if locx1 > basex else (-basex if locx1 < -basex else locx1)
                            if (
                                (k < (leni - 1))
                                or (stepi == 1 and locy1 < latx[i + 1])
                                or (stepi == -1 and locy1 > latx[i + 1])
                            ):
                                x = gridded_arcsec(locx1, basex, arc)
                                idx_buf.append((s - mlatbase, x - mlonbase))
                                loc_buf.append((float(locx1), float(locy1)))

            if i == len(lonx) - 2:
                idx_buf.append((latidx1 - mlatbase, lonidx1 - mlonbase))
                loc_buf.append((float(lonx[i + 1]), float(latx[i + 1])))
        if brk != -1:
            preidx = brk + 1

    idx = np.asarray(idx_buf, dtype=np.int32) if idx_buf else np.empty((0, 2), dtype=np.int32)
    loc = np.asarray(loc_buf, dtype=float) if loc_buf else np.empty((0, 2), dtype=float)
    unique_idx, inverse = (
        np.unique(idx, axis=0, return_inverse=True)
        if idx.size
        else (np.empty((0, 2), dtype=np.int32), np.empty((0,), dtype=np.int32))
    )
    bbox_height = gridded_arcsec(mlat1, basey, arc) - mlatbase + 1
    bbox_width = gridded_arcsec(mlon1, basex, arc) - mlonbase + 1
    breakpoint_indices = brks[brks >= 0].astype(np.int32, copy=False)
    return LinePlan(
        idx=idx,
        loc=loc,
        unique_idx=unique_idx,
        inverse=inverse.astype(np.int32, copy=False),
        breakpoint_indices=breakpoint_indices,
        mlatbase=mlatbase,
        mlonbase=mlonbase,
        bbox_lon0=float(mlon0),
        bbox_lat0=float(mlat0),
        bbox_lon1=float(mlon1),
        bbox_lat1=float(mlat1),
        bbox_height=int(bbox_height),
        bbox_width=int(bbox_width),
        bbox_subset_cells=int(bbox_height * bbox_width),
        inner_iters=inner_iters,
    )
