"""FastAPI app entry point for the ODB GEBCO bathymetry service.

v0.5.3 hardening pass — see specs/v0.5.3_hardening_checklist_v2.md.

The application contract is unchanged from v0.5.2; this version adds:

  * H1  jsonsrc URL fetch hardened (timeout / SSRF guard / no-redirect /
        streamed size cap). Default still accepts http/https URLs from
        the public internet.
  * H2  lon / lat finite + range validation (rejects NaN, ±Inf,
        out-of-range values).
  * H3  `mode=lon360` accepts input in [0, 360] (validated first, then
        normalised to [-180, 180] for grid lookup).
  * H4  `mode` tokens parsed once into a frozenset; substring matches
        retired.
  * H5  dead `df1.drop("distance")` code removed; the invariant is now
        asserted inside `polyhandler()`.
  * H6  bbox cell-count caps for line/point and polygon paths; over-cap
        requests return 413 instead of OOMing.
  * H10 dask pool size made env-controlled (`GEBCO_DASK_POOL_SIZE`).
  * H13 structured logging via QueueHandler; one log line per request.
"""
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import dask
import numpy as np
import xarray as xr
from fastapi import FastAPI, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, ORJSONResponse
from multiprocessing.pool import Pool

# Resolve data/ paths relative to this file, NOT the current working directory.
# Production launchers (gunicorn / pm2) always cd into the repo root so the
# previous "data/..." relative path worked there, but dev2026 verification
# scripts run from outside the repo root and would FileNotFoundError on the
# old path. Anchoring to __file__ is correct in both cases.
_APP_ROOT = Path(__file__).resolve().parent

import src.config as config
from src import logger as gebco_logger
from src.jsonsrc import JsonSrcError, load_jsonsrc
from src.modes import parse_modes
from src.polyhandler import polyhandler
from src.validation import numarr_query_validator, validate_lonlat
from src.zprofile import BboxTooLarge, zprofile

# v0.5.3 H10 — dask multiprocessing pool is opt-in via env knob; default
# matches v0.5.2 (size 4) to keep behaviour byte-equal at upgrade time.
if config.DASK_POOL_SIZE > 1:
    dask.config.set(pool=Pool(config.DASK_POOL_SIZE))


def generate_custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=config.api_title,
        version=config.api_version,
        description="Z-profile (and distances) between longitude/latitude points with 15-arcsec resolutions.\n"
        + "Data source attribution: "
        + config.api_dataset_attribution,
        routes=app.routes,
    )
    if config.api_servers:
        openapi_schema["servers"] = [{"url": url} for url in config.api_servers]
    else:
        openapi_schema.pop("servers", None)
    app.openapi_schema = openapi_schema
    return app.openapi_schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v0.5.3 H13: start the structured logger.
    gebco_logger.configure()

    config.ds = xr.open_zarr(
        # Blosc/LZ4 clevel=5 — matches 2023 compressor for read-speed parity.
        # See dev2026/scripts/benchmark_old_new_api.py for the reasoning.
        # Path is anchored to this file's directory (see _APP_ROOT above) so
        # the app works regardless of caller's cwd.
        str(_APP_ROOT / "data" / "GEBCO_2026_sub_ice_topo.zarr"),
        chunks="auto",
        decode_cf=False,
        decode_times=False,
    )
    arcsec = 15
    config.arc = int(3600 / arcsec)  # 15 arc-second
    config.basex = 180
    config.basey = 90
    yield
    config.ds.close()
    # v0.5.3 H13: stop the background log listener so reloads don't leak.
    gebco_logger.shutdown()


app = FastAPI(docs_url=None, lifespan=lifespan, default_response_class=ORJSONResponse)


@app.get("/gebco/openapi.json", include_in_schema=False)
async def custom_openapi():
    return JSONResponse(generate_custom_openapi())


@app.get("/gebco/swagger", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url="/gebco/openapi.json",
        title=app.title,
    )


def geojson_validator(json_obj):
    return ("type" in json_obj) and (
        (json_obj["type"] == "FeatureCollection" and "features" in json_obj)
        or (
            json_obj["type"] == "Feature"
            and "geometry" in json_obj
            and json_obj["geometry"]["type"]
            in [
                "Point",
                "LineString",
                "Polygon",
                "MultiPoint",
                "MultiLineString",
                "MultiPolygon",
                "GeometryCollection",
            ]
        )
        or (
            json_obj["type"]
            in [
                "Point",
                "LineString",
                "Polygon",
                "MultiPoint",
                "MultiLineString",
                "MultiPolygon",
                "GeometryCollection",
            ]
        )
    )


def _error_response(status_code: int, message: str, *, request: Request,
                    t0: float, modes: frozenset, polyMode: bool,
                    npoints: Optional[int]) -> JSONResponse:
    """Build a 4xx/5xx JSONResponse and emit a single WARNING log line."""
    elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
    gebco_logger.logger.warning(json.dumps({
        "event": "gebco_request_error",
        "status": status_code,
        "error": message,
        "client": getattr(request.client, "host", None) if request.client else None,
        "ua": (request.headers.get("user-agent", "") or "")[:120],
        "mode": sorted(modes) if modes else [],
        "polyMode": polyMode,
        "npoints": npoints,
        "elapsed_ms": elapsed_ms,
    }))
    return JSONResponse(status_code=status_code, content={"Error": message})


@app.get(
    "/gebco",
    tags=["Bathymetry"],
    summary=f"Get GEBCO bathymetry ({config.api_dataset_label})",
)
def gebco(
    request: Request,
    lon: Optional[str] = Query(
        None,
        description="comma-separated longitude values. One of lon/lat and jsonsrc should be specified as longitude/latitude input.",
        example="122.36,122.47",
    ),
    lat: Optional[str] = Query(
        None,
        description="comma-separated latitude values. One of lon/lat and jsonsrc should be specified as longitude/latitude input.",
        example="25.02,24.82",
    ),
    mode: Optional[str] = Query(
        None,
        description="comma-separated modes: row, point, truncate (for longitude and latitude to 5 decimal places), lon360 (output longitude in [0, 360]; input may also be in [0, 360]). Optional can be none.\n"
        + "Special mode for polygon: zonly (not output pair-wise distance), lineid (output lineid for MultiLineString).",
    ),
    sample: Optional[int] = Query(
        5,
        description="Re-sampling polygon every N points(default 5, only work for polygon, and always 1 for line and point)",
    ),
    jsonsrc: Optional[str] = Query(
        None,
        description="Optional. A valid URL for JSON source or a JSON string that contains longitude and latitude keys with values in array.\n"
        + 'Example: {"longitude":[122.36,122.47,122.56,122.66],"latitude":[25.02,24.82,24.72,24.62]}.\n'
        + 'New feature: GeoJSON to get terrain of polygon. Example: {"type": "Polygon", "coordinates": [[[121, 22.5], [121, 23.5], [122, 23.5], [122, 22.5], [121, 22.5]]]}. Feature collection is allowed.',
    ),
):
    t0 = time.monotonic()
    polyMode = False
    modes = parse_modes(mode)  # v0.5.3 H4

    if sample is None or sample < 1:
        poly_sample = 5
    else:
        poly_sample = sample

    loni: Optional[np.ndarray] = None
    lati: Optional[np.ndarray] = None
    rows: Optional[int] = None
    response: Optional[JSONResponse] = None

    try:
        if jsonsrc:
            # v0.5.3 H1: hardened jsonsrc loader (inline-first, SSRF guard,
            # streamed size cap). Raises JsonSrcError (subclass of ValueError).
            json_obj = load_jsonsrc(jsonsrc)

            polyMode = geojson_validator(json_obj)
            if polyMode:
                df1, _ = polyhandler(json_obj, 0, mode if mode else "", 1, poly_sample)
                # v0.5.3 H5: dead `df1.drop("distance")` block removed.
                # polyhandler() guarantees no distance column in zonly mode.
                if "row" in modes:
                    out = df1.to_dicts()
                else:
                    out = {column: df1[column].to_list() for column in df1.columns}
                rows = df1.height
                response = ORJSONResponse(content=out)
            else:
                loni = np.asarray(json_obj["longitude"], dtype=np.float64)
                lati = np.asarray(json_obj["latitude"], dtype=np.float64)
        else:
            if lon and lat:
                # v0.5.3 H8: validators now raise ValueError instead of
                # returning a "Format Error" string sentinel.
                loni = numarr_query_validator(lon)
                lati = numarr_query_validator(lat)
            else:
                raise ValueError(
                    "Both 'lon' and 'lat' parameters must be provided, otherwise use 'jsonsrc' as input"
                )

        if response is None:
            # v0.5.3 H2 + H3: validate, then (for lon360) normalise.
            if "lon360" in modes:
                err = validate_lonlat(loni, lati, allow_lon360=True)
                if err is None:
                    # H3 — only normalise AFTER the original input has been
                    # accepted against the [0, 360] window.
                    loni = np.where(loni > 180.0, loni - 360.0, loni)
            else:
                err = validate_lonlat(loni, lati, allow_lon360=False)
            if err is not None:
                response = _error_response(
                    status.HTTP_400_BAD_REQUEST, err,
                    request=request, t0=t0, modes=modes,
                    polyMode=polyMode, npoints=int(loni.size) if loni is not None else None,
                )
            else:
                # v0.5.4 H13 follow-up: ask zprofile for a polars DataFrame
                # directly so the handler owns the row count without a JSON
                # round-trip. Format the response from the DataFrame here.
                # sample is always 1 for line/point queries.
                mode_for_zp = mode or ""
                if "dataframe" not in modes:
                    mode_for_zp = (mode_for_zp + ",dataframe") if mode_for_zp else "dataframe"
                df = zprofile(loni, lati, mode_for_zp, 1)
                rows = int(df.height)
                if "row" in modes:
                    out = df.to_dicts()
                else:
                    out = {col: df[col].to_list() for col in df.columns}
                response = ORJSONResponse(content=out)
    except JsonSrcError as exc:
        return _error_response(
            status.HTTP_400_BAD_REQUEST, str(exc),
            request=request, t0=t0, modes=modes, polyMode=polyMode, npoints=None,
        )
    except BboxTooLarge as exc:
        return _error_response(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc),
            request=request, t0=t0, modes=modes, polyMode=polyMode,
            npoints=int(loni.size) if loni is not None else None,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return _error_response(
            status.HTTP_400_BAD_REQUEST, str(exc),
            request=request, t0=t0, modes=modes, polyMode=polyMode,
            npoints=int(loni.size) if loni is not None else None,
        )

    # v0.5.3 H13: one structured INFO line per successful request.
    elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
    log_payload = {
        "event": "gebco_request",
        "client": getattr(request.client, "host", None) if request.client else None,
        "ua": (request.headers.get("user-agent", "") or "")[:120],
        "mode": sorted(modes),
        "polyMode": polyMode,
        "npoints": int(loni.size) if loni is not None else None,
        "rows": rows,
        "elapsed_ms": elapsed_ms,
    }
    if elapsed_ms > config.SLOW_REQUEST_MS:
        # Slow requests escalate to WARNING even when INFO is off.
        gebco_logger.logger.warning(json.dumps({**log_payload, "slow": True}))
    else:
        gebco_logger.logger.info(json.dumps(log_payload))
    return response
