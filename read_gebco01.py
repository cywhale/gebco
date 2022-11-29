import xarray as xr
import numpy as np
import pandas as pd
import math
# import time
# import orjson
from geopy.distance import geodesic
from fastapi import FastAPI, status  # , Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from typing import Union
# from loggerConfig import logger
# from models import zprofSchema
import dask
from multiprocessing.pool import Pool
dask.config.set(pool=Pool(4))  # , scheduler='processes', num_workers=4)
# dask.set_options(get=dask.get_sync)


app = FastAPI()

arcsec = 15
arc = int(3600/arcsec)  # 15 arc-second
basex = 180  # -180 - 180 <==> 0 - 360, half is 180
basey = 90  # -90 - 90 <==> 0 - 180, half is 90
halfxidx = 180 * arc  # in netcdf, longitude length = 86400
halfyidx = 90 * arc  # in netcdf, latitude length = 43200
subsetFlag = True


@app.on_event("startup")
async def startup():
    global ds
    # logger.info
    ds = xr.open_zarr(
        'GEBCO_2022_sub_ice_topo.zarr', chunks='auto', group='gebco',
        decode_cf=False, decode_times=False)

# @app.on_event("shutdown")
# def release_dataset():
#   global ds
#   ds.close() #if use nc file


def gridded_arcsec(x, base=90, arc=3600/15):
    return((int(x) + base) * arc + math.ceil((x-int(x))*arc))


def curDist(loc, dis=np.empty(shape=[0, 1], dtype=float)):
    if len(loc) < 2:
        return 0  # None
    lk = len(loc)-1
    return(np.append(dis,
                     geodesic((loc[lk-1, 1], loc[lk-1, 0]),
                              (loc[lk, 1], loc[lk, 0])).km))


def numarr_query_validator(qry):
    if ',' in qry:
        try:
            out = [float(x.strip()) for x in qry.split(',')]
            return(out)
        except ValueError:
            return("Format Error")
    else:
        try:
            out = [float(qry.strip())]
            return(out)
        except ValueError:
            return("Format Error")


@app.get("/gebco")
def zprofile(lon: str, lat: str, mode: Union[str, None] = None):
    lonx = numarr_query_validator(lon)
    latx = numarr_query_validator(lat)
    if isinstance(lonx, str) or isinstance(latx, str):
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content=jsonable_encoder({"Error": "Check your input format should be comma-separated values"}))

    global ds
    global arcsec
    global arc
    global basex
    global basey
    global halfxidx
    global halfyidx
    global subsetFlag

    # st = time.time()
    # ds = xr.open_mfdataset(
    #    'GEBCO_2022_sub_ice_topo.nc', chunks={0: 60, 1: 60},
    # et = time.time()
    # print('1. Load dataset time: ', et-st, 'sec')
    # lon = [123.98, 123]  # just testing
    # lat = [23.33, 21.33]
    # i.e. {longitude=[...], laitude=[...], z=[...]}, otherwise 'row', output df-like records
    format = 'default'
    # i.e, output all gridded points along the line; otherwise 'point', output only end-points.
    zmode = 'line'
    if mode is not None:
        if 'point' in mode.lower():
            zmode = 'point'
        if 'row' in mode.lower():
            format = 'row'
    #print("current mode: ", format, " & ", zmode)

    if len(lonx) != len(latx):
        ds.close()
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content=jsonable_encoder({"Error": "Check your input of lon/lat should be in equal length"}))

    # May not have exact lon, lat in gridded lon-lat, so need to calculate index
    elif len(lonx) == 1:
        # st = time.time()
        lon0 = gridded_arcsec(lonx[0], basex, arc)
        lat0 = gridded_arcsec(latx[0], basey, arc)
        mlon0x = ds["lon"][lon0]
        mlat0x = ds["lat"][lat0]
        st1 = ds.sel(lon=mlon0x, lat=mlat0x)
        ds.close()
        loc1 = [lonx[0], latx[0]]
        xt1 = np.array([st1['elevation'].values])
        if format == 'row':
            df1 = pd.DataFrame({"longitude": np.array([loc1[0]]).tolist(),
                                "latitude": np.array([loc1[1]]).tolist(),
                                "z": xt1.tolist(), "distance": np.array([0]).tolist()},
                               columns=['longitude', 'latitude', 'z', 'distance'])
        else:
            out = jsonable_encoder({"longitude": np.array([loc1[0]]).tolist(),
                                    "latitude": np.array([loc1[1]]).tolist(),
                                    "z": xt1.tolist(), "distance": np.array([0]).tolist()})
        # et = time.time()
        # print('Get only one-point exe time: ', et-st, 'sec')
        # return(xt1)
    else:
        # st = time.time()
        idx1 = np.empty(shape=[0, 2], dtype=np.int16)
        loc1 = np.empty(shape=[0, 2], dtype=float)
        dis1 = np.array([0.0])
        mlon0 = np.min(lonx)
        #mlon1 = np.max(lonx)
        mlat0 = np.min(latx)
        #mlat1 = np.max(latx)
        # if do subsetting dataset, reference-0-x,y should be biased
        mlonbase = gridded_arcsec(mlon0, basex, arc) if subsetFlag else 0
        mlatbase = gridded_arcsec(mlat0, basey, arc) if subsetFlag else 0
        # index from gridded_arcsec will change is reference is ds_s1, not ds
        #print(mlonbase, mlatbase)
        #mlonidx1 = gridded_arcsec(mlon1, basex, arc)
        #mlatidx1 = gridded_arcsec(mlat1, basey, arc)
        for i in range(len(lonx)-1):
            lonidx0 = gridded_arcsec(lonx[i], basex, arc)
            latidx0 = gridded_arcsec(latx[i], basey, arc)
            lonidx1 = gridded_arcsec(lonx[i+1], basex, arc)
            latidx1 = gridded_arcsec(latx[i+1], basey, arc)
            if lonidx0 == lonidx1 and latidx0 == latidx1:
                idx1 = np.append(
                    idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                loc1 = np.append(loc1, [[lonx[i], latx[i]]], axis=0)
                # to match the same length
                if i >= 1:
                    dis1 = curDist(loc1, dis1)
            elif zmode == 'point':
                idx1 = np.append(
                    idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                loc1 = np.append(loc1, [[lonx[i], latx[i]]], axis=0)
                dist = geodesic((latx[i], lonx[i]),
                                (latx[i+1], lonx[i+1])).km
                dis1 = np.append(dis1, dist, axis=None)
                if i == len(lonx)-2:
                    idx1 = np.append(
                        idx1, [[latidx1-mlatbase, lonidx1-mlonbase]], axis=0)
                    loc1 = np.append(loc1, [[lonx[i+1], latx[i+1]]], axis=0)
            else:
                if lonx[i] == lonx[i+1]:
                    stepi = -1 if latidx0 > latidx1 else 1
                    rngi = range(latidx0, latidx1+stepi, stepi)
                    leni = len(rngi)
                    for k, y in enumerate(rngi):
                        locy0 = y/arc - basey
                        locy0i = int(locy0)
                        doty0i = locy0-locy0i-0.25/arc  # a small bias to make sure it's in grid
                        locy1 = locy0i + doty0i
                        if (k < (leni-1)) or (stepi == 1 and locy1 < latx[i+1]) or (stepi == -1 and locy1 > latx[i+1]):
                            idx1 = np.append(
                                idx1, [[y-mlatbase, lonidx0-mlonbase]], axis=0)
                            loc1 = np.append(loc1, [[lonx[i], locy1]], axis=0)
                            if i >= 1 or k >= 1:
                                dis1 = curDist(loc1, dis1)

                elif latx[i] == latx[i+1]:
                    stepi = -1 if lonidx0 > lonidx1 else 1
                    rngi = range(lonidx0, lonidx1+stepi, stepi)
                    leni = len(rngi)
                    for k, x in enumerate(rngi):
                        locx0 = x/arc - basex
                        locx0i = int(locx0)
                        dotx0i = locx0-locx0i-0.25/arc  # a small bias to make sure it's in grid
                        locx1 = locx0i + dotx0i
                        if (k < (leni-1)) or (stepi == 1 and locx1 < lonx[i+1]) or (stepi == -1 and locx1 > lonx[i+1]):
                            idx1 = np.append(
                                idx1, [[latidx0-mlatbase, x-mlonbase]], axis=0)
                            loc1 = np.append(loc1, [[locx1, latx[i]]], axis=0)
                            if i >= 1 or k >= 1:
                                dis1 = curDist(loc1, dis1)

                else:
                    m = (latx[i+1]-latx[i])/(lonx[i+1]-lonx[i])
                    b = latx[i] - m * lonx[i]  # y = mx + b
                    # print("m, b:", m ,b)
                    if np.absolute(m) <= 1:
                        lidx0 = lonidx0
                        lidx1 = lonidx1
                    else:
                        lidx0 = latidx0
                        lidx1 = latidx1

                    stepi = -1 if lidx0 > lidx1 else 1
                    rngi = range(lidx0, lidx1+stepi, stepi)
                    leni = len(rngi)
                    for k, s in enumerate(rngi):
                        if k == 0:  # should consider internal node not repeated twice
                            idx1 = np.append(
                                idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                            loc1 = np.append(
                                loc1, [[lonx[i], latx[i]]], axis=0)
                            if i >= 1:
                                dis1 = curDist(loc1, dis1)

                        else:
                            if np.absolute(m) <= 1:
                                locx0 = s/arc - basex
                                locx0i = int(locx0)
                                dotx0i = locx0-locx0i-0.25/arc  # a small bias to make sure it's in grid
                                locx1 = locx0i + dotx0i
                                locy1 = m * locx1 + b
                                if (k < (leni-1)) or (stepi == 1 and locx1 < lonx[i+1]) or (stepi == -1 and locx1 > lonx[i+1]):
                                    y = gridded_arcsec(locy1, basey, arc)
                                    idx1 = np.append(
                                        idx1, [[y-mlatbase, s-mlonbase]], axis=0)
                                    loc1 = np.append(
                                        loc1, [[locx1, locy1]], axis=0)
                                    dis1 = curDist(loc1, dis1)
                            else:
                                locy0 = s/arc - basey
                                locy0i = int(locy0)
                                doty0i = locy0-locy0i-0.25/arc
                                locy1 = locy0i + doty0i
                                locx1 = (locy1 - b)/m
                                if (k < (leni-1)) or (stepi == 1 and locy1 < latx[i+1]) or (stepi == -1 and locy1 > latx[i+1]):
                                    x = gridded_arcsec(locx1, basex, arc)
                                    idx1 = np.append(
                                        idx1, [[s-mlatbase, x-mlonbase]], axis=0)
                                    loc1 = np.append(
                                        loc1, [[locx1, locy1]], axis=0)
                                    dis1 = curDist(loc1, dis1)

                if i == len(lonx)-2:
                    idx1 = np.append(
                        idx1, [[latidx1-mlatbase, lonidx1-mlonbase]], axis=0)
                    loc1 = np.append(loc1, [[lonx[i+1], latx[i+1]]], axis=0)
                    dis1 = curDist(loc1, dis1)

        # np.savetxt("simu/test_loc.csv", loc1, delimiter=",", fmt='%f')
        # st = time.time()
        # mlon0 = np.min(lonx) #may cause slice offset to mlonbase, an offset +-1
        mlon1 = np.max(lonx) + 1.5/arc  # to make it larger
        # mlat0 = np.min(latx) #may cause slice offset to mlatbase, an offset +-1
        mlat1 = np.max(latx) + 1.5/arc
        mlon0x = ds["lon"][mlonbase]
        mlat0x = ds["lat"][mlatbase]
        ds_s1 = ds.sel(lon=slice(mlon0x, mlon1), lat=slice(
            mlat0x, mlat1)) if subsetFlag else ds
        ds.close()
        # print(ds_s1['elevation'].shape)
        xt1 = ds_s1['elevation'].values[tuple(idx1.T)]
        ds_s1.close()
        if format == 'row':
            df1 = pd.DataFrame({"longitude": loc1[:, 0].tolist(),
                                "latitude": loc1[:, 1].tolist(),
                                "z": xt1.tolist(),
                                "distance": dis1.tolist()},
                               columns=['longitude', 'latitude', 'z', 'distance'])
        else:
            out = jsonable_encoder({"longitude": loc1[:, 0].tolist(),
                                    "latitude": loc1[:, 1].tolist(),
                                    "z": xt1.tolist(),
                                    "distance": dis1.tolist()})
        # et = time.time()
        # print('Get value from index of points, time: ', et-st, 'sec')
        # return({"data": xt1})
        # np.savetxt("simu/test_zseg.csv", pt1, delimiter=",", fmt='%f')

    # st = time.time()
    # jt1 = orjson.dumps(xt1.tolist(), option=orjson.OPT_NAIVE_UTC |
    #                   orjson.OPT_SERIALIZE_NUMPY)
    # out = jsonable_encoder({"data": xt1.tolist()})
    if format == 'row':
        out = df1.to_dict(orient='records')
    # et = time.time()
    # print('4 Convert JSON by fastapi: ', et-st, 'sec')
    return JSONResponse(content=out)
