import xarray as xr
import numpy as np
from fastapi import FastAPI, status, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, ORJSONResponse
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from contextlib import asynccontextmanager
from typing import Optional  # , Union

# from pydantic import BaseModel, ValidationError, HttpUrl, validator
import requests
import json

# import orjson
# from loggerConfig import logger
import src.config as config
from src.zprofile import zprofile
from src.polyhandler import polyhandler

import dask
from multiprocessing.pool import Pool

dask.config.set(pool=Pool(4))  # , scheduler='processes', num_workers=4)


def generate_custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="ODB API for GEBCO Bathymetry",
        version="1.0.0",
        description="Z-profile (and distances) between longitude/latitude points with 15-arcsec resolutions.\n"
        +
        # "Data source: GEBCO Compilation Group (2022) GEBCO_2022 Grid (doi:10.5285/e0f0bb80-ab44-2739-e053-6c86abc0289c)",
        "Data source: GEBCO Compilation Group (2023) GEBCO 2023 Grid (doi:10.5285/f98b053b-0cbc-6c23-e053-6c86abc0af7b)",
        routes=app.routes,
    )
    openapi_schema["servers"] = [{"url": "https://api.odb.ntu.edu.tw"}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


# @app.on_event("startup")
# async def startup():
@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ds = xr.open_zarr(
        #'data/GEBCO_2022_sub_ice_topo.zarr', chunks='auto', group='gebco',
        "data/GEBCO_2023_sub_ice_topo.zarr",
        chunks="auto",
        decode_cf=False,
        decode_times=False,
    )
    arcsec = 15
    config.arc = int(3600 / arcsec)  # 15 arc-second
    config.basex = 180  # -180 - 180 <==> 0 - 360, half is 180
    config.basey = 90  # -90 - 90 <==> 0 - 180, half is 90
    # config.halfxidx = 180 * config.arc  # in netcdf, longitude length = 86400
    # config.halfyidx = 90 * config.arc  # in netcdf, latitude length = 43200
    # config.subsetFlag = True
    # above code to execute when app is loading
    yield
    # below code to execute when app is shutting down
    config.ds.close()


app = FastAPI(docs_url=None, lifespan=lifespan, default_response_class=ORJSONResponse)


@app.get("/gebco/openapi.json", include_in_schema=False)
async def custom_openapi():
    return JSONResponse(
        generate_custom_openapi()
    )  # app.openapi()) modify to customize openapi.json


@app.get("/gebco/swagger", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url="/gebco/openapi.json",  # app.openapi_url
        title=app.title,
    )


### Global variables move to config.py ###


def geojson_validator(json_obj):
    return ("type" in json_obj) and (
        (json_obj["type"] == "FeatureCollection" and "features" in json_obj)
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


def numarr_query_validator(qry):
    if "," in qry:
        try:
            out = np.array([float(x.strip()) for x in qry.split(",")])
            return out
        except ValueError:
            return "Format Error"
    else:
        try:
            out = np.array([float(qry.strip())])
            return out
        except ValueError:
            return "Format Error"


@app.get("/gebco", tags=["Bathymetry"], summary="Get GEBCO(2023) bathymetry")
def gebco(
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
        None, description="comma-separated modes: row, point. Optional can be none"
    ),
    jsonsrc: Optional[str] = Query(
        None,
        description="Optional. A valid URL for JSON source or a JSON string that contains longitude and latitude keys with values in array.\n"
        + 'Example: {"longitude":[122.36,122.47,122.56,122.66],"latitude":[25.02,24.82,24.72,24.62]}',
    ),
):
    polyMode = False
    try:
        if jsonsrc:
            # Validate it's a URL
            try:
                json_resp = requests.get(jsonsrc)
                json_resp.raise_for_status()
                json_obj = json_resp.json()
            except requests.RequestException:
                try:
                    json_obj = json.loads(jsonsrc)
                except json.JSONDecodeError:
                    raise ValueError(
                        "Input jsonsrc must be a valid URL or a JSON string."
                    )

            polyMode = geojson_validator(json_obj)
            if polyMode:
                df1, _ = polyhandler(json_obj, 0, mode)
                format = "default"
                if mode is not None and "row" in mode.lower():
                    format = "row"
                if (
                    mode is not None
                    and "zonly" in mode.lower()
                    and "distance" in df1.columns
                ):
                    df1.drop("distance")
                if format == "row":
                    out = df1.to_dicts()
                else:
                    # out = df1.to_pandas().to_dict() #produce {"longitude": {...}, "latitude": {...}, "value": {}}
                    # But desired result: ` {"longitude": [...], "latitude": [...], "value": [....]}`.
                    out = {column: df1[column].to_list() for column in df1.columns}
                return ORJSONResponse(content=out)

            loni = np.array(json_obj["longitude"])
            lati = np.array(json_obj["latitude"])
        else:
            if lon and lat:
                loni = numarr_query_validator(lon)
                lati = numarr_query_validator(lat)

                if isinstance(loni, str) or isinstance(lati, str):
                    return JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content=jsonable_encoder(
                            {
                                "Error": "Check your input format should be comma-separated values"
                            }
                        ),
                    )
                # Validate longitude and latitude
                # LonLat(longitude=longitude.tolist(), latitude=latitude.tolist())
            else:
                raise ValueError(
                    "Both 'lon' and 'lat' parameters must be provided, otherwise use 'jsonsrc' as input"
                )

    except (ValueError, json.JSONDecodeError) as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"Error": str(e)}
        )
    except requests.HTTPError as e:
        return JSONResponse(
            status_code=e.response.status_code, content={"Error": str(e)}
        )

    return zprofile(loni, lati, mode)
