import xarray as xr
import numpy as np
#import pandas as pd
import polars as pl
import math
import time
# import orjson
from geopy.distance import geodesic
from fastapi import FastAPI, status  # , Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from typing import Union
# from loggerConfig import logger
# from models import zprofSchema
from xmeridian import *

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
lon = '179.8465,179.9015,-179.8502, 179.88'
lat = '0.163806258890185, 0.074795994003052, -0.264, -0.274'
format = 'row'
gbl_count = 0


@app.on_event("startup")
async def startup():
    global ds
    global gbl_count
    # logger.info
    ds = xr.open_zarr(
        'GEBCO_2022_sub_ice_topo.zarr', chunks='auto', group='gebco',
        decode_cf=False, decode_times=False)
    gbl_count = gbl_count + 1  # for simulation

# @app.on_event("shutdown")
# def release_dataset():
#   global ds
#   ds.close() #if use nc file


def gridded_arcsec(x, base=90, arc=3600/15):
    idx = (int(x) + base) * arc + math.ceil((x-int(x))*arc)
    return idx-1 if idx > 0 else 0


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
            out = np.array([float(x.strip()) for x in qry.split(',')])
            return(out)
        except ValueError:
            return("Format Error")
    else:
        try:
            out = np.array([float(qry.strip())])
            return(out)
        except ValueError:
            return("Format Error")


@app.get("/gebco")
def zprofile(mode: Union[str, None] = None):
    st = time.time()
    global lon, lat, format, gbl_count  # for simulation
    loni = numarr_query_validator(lon)
    lati = numarr_query_validator(lat)
    if isinstance(loni, str) or isinstance(lati, str):
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

    # format = 'default' # for simulation
    # i.e, output all gridded points along the line; otherwise 'point', output only end-points.
    zmode = 'line'
    if mode is not None:
        if 'point' in mode.lower():
            zmode = 'point'
        if 'default' in mode.lower():
            format = 'default'  # for simulation

    if len(loni) != len(lati):
        ds.close()
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content=jsonable_encoder({"Error": "Check your input of lon/lat should be in equal length"}))

    # May not have exact lon, lat in gridded lon-lat, so need to calculate index
    elif len(loni) == 1:
        lon0 = gridded_arcsec(loni[0], basex, arc)
        lat0 = gridded_arcsec(lati[0], basey, arc)
        mlon0x = ds["lon"][lon0]
        mlat0x = ds["lat"][lat0]
        st1 = ds.sel(lon=mlon0x, lat=mlat0x)
        ds.close()
        loc1 = [loni[0], lati[0]]
        xt1 = np.array([st1['elevation'].values])
        if format == 'row':
            # df1= pd.DataFrame({"longitude": np.array([loc1[0]]).tolist(),
            df1 = pl.DataFrame({"longitude": np.array([loc1[0]]).tolist(),
                                "latitude": np.array([loc1[1]]).tolist(),
                                "z": xt1.tolist(), "distance": np.array([0]).tolist()},
                               columns=['longitude', 'latitude', 'z', 'distance'])
        else:
            out = jsonable_encoder({"longitude": np.array([loc1[0]]).tolist(),
                                    "latitude": np.array([loc1[1]]).tolist(),
                                    "z": xt1.tolist(), "distance": np.array([0]).tolist()})

    else:
        if zmode == 'point':
            lonk = loni
            latk = lati
            brks = [-1]
        else:
            ats = crossBoundary(loni, lati)
            lonk = np.asarray(ats[0])
            latk = np.asarray(ats[1])
            brks = ats[2]
            #autoFly = ats[3]
            # dirFlag = ats[4] #deprecated
        idx1 = np.empty(shape=[0, 2], dtype=np.int16)
        loc1 = np.empty(shape=[0, 2], dtype=float)
        dis1 = np.array([0.0])
        mlon0 = np.min(lonk) - 1.5/arc   # to make it smaller
        mlon0 = mlon0 if mlon0 > -basex else -basex+0.00001
        # mlon1 = np.max(long)
        mlat0 = np.min(latk) - 1.5/arc
        mlat0 = mlat0 if mlat0 > -basey else -basey+0.00001
        # mlat1 = np.max(latg)
        # if do subsetting dataset, reference-0-x,y should be biased
        mlonbase = gridded_arcsec(mlon0, basex, arc) if subsetFlag else 0
        mlatbase = gridded_arcsec(mlat0, basey, arc) if subsetFlag else 0
        # index from gridded_arcsec will change is reference is ds_s1, not ds
        # mlonidx1 = gridded_arcsec(mlon1, basex, arc)
        # mlatidx1 = gridded_arcsec(mlat1, basey, arc)
        preidx = 0
        for brk in brks:
            lonx = lonk[preidx:] if brk == -1 else lonk[preidx:(brk+1)]
            latx = latk[preidx:] if brk == -1 else latk[preidx:(brk+1)]
            if len(lonx) == 1:  # only occur because break-points cause a closer-zero end-pt appear in last segment
                lonidx0 = gridded_arcsec(lonx[0], basex, arc)
                latidx0 = gridded_arcsec(latx[0], basey, arc)
                idx1 = np.append(
                    idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                loc1 = np.append(loc1, [[lonx[0], latx[0]]], axis=0)
                continue

            for i in range(len(lonx)-1):
                lonidx0 = gridded_arcsec(lonx[i], basex, arc)
                latidx0 = gridded_arcsec(latx[i], basey, arc)
                lonidx1 = gridded_arcsec(lonx[i+1], basex, arc)
                latidx1 = gridded_arcsec(latx[i+1], basey, arc)
                if zmode == 'point':
                    idx1 = np.append(
                        idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                    loc1 = np.append(loc1, [[lonx[i], latx[i]]], axis=0)
                    dist = geodesic((latx[i], lonx[i]),
                                    (latx[i+1], lonx[i+1])).km
                    dis1 = np.append(dis1, dist, axis=None)
                    if i == len(lonx)-2:
                        idx1 = np.append(
                            idx1, [[latidx1-mlatbase, lonidx1-mlonbase]], axis=0)
                        loc1 = np.append(
                            loc1, [[lonx[i+1], latx[i+1]]], axis=0)
                elif lonidx0 == lonidx1 and latidx0 == latidx1:
                    idx1 = np.append(
                        idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                    loc1 = np.append(loc1, [[lonx[i], latx[i]]], axis=0)
                    # to match the same length
                    if i >= 1:
                        dis1 = curDist(loc1, dis1)
                else:
                    if lonx[i] == lonx[i+1]:
                        stepi = -1 if latidx0 > latidx1 else 1
                        rngi = range(latidx0, latidx1+stepi, stepi)
                        leni = len(rngi)
                        for k, y in enumerate(rngi):
                            locy0 = (y+1)/arc - basey
                            locy0i = int(locy0)
                            doty0i = locy0-locy0i-0.25/arc  # a small bias to make sure it's in grid
                            locy1 = locy0i + doty0i
                            locy1 = basey if locy1 > basey else (
                                -basey if locy1 < -basey else locy1)
                            if (k < (leni-1)) or (stepi == 1 and locy1 < latx[i+1]) or (stepi == -1 and locy1 > latx[i+1]):
                                idx1 = np.append(
                                    idx1, [[y-mlatbase, lonidx0-mlonbase]], axis=0)
                                loc1 = np.append(
                                    loc1, [[lonx[i], locy1]], axis=0)
                                if i >= 1 or k >= 1:
                                    dis1 = curDist(loc1, dis1)

                    elif latx[i] == latx[i+1]:
                        stepi = -1 if lonidx0 > lonidx1 else 1
                        rngi = range(lonidx0, lonidx1+stepi, stepi)
                        leni = len(rngi)
                        for k, x in enumerate(rngi):
                            locx0 = (x+1)/arc - basex
                            locx0i = int(locx0)
                            dotx0i = locx0-locx0i-0.25/arc  # a small bias to make sure it's in grid
                            locx1 = locx0i + dotx0i
                            locx1 = basex if locx1 > basex else (
                                -basex if locx1 < -basex else locx1)
                            if (k < (leni-1)) or (stepi == 1 and locx1 < lonx[i+1]) or (stepi == -1 and locx1 > lonx[i+1]):
                                idx1 = np.append(
                                    idx1, [[latidx0-mlatbase, x-mlonbase]], axis=0)
                                loc1 = np.append(
                                    loc1, [[locx1, latx[i]]], axis=0)
                                if i >= 1 or k >= 1:
                                    dis1 = curDist(loc1, dis1)

                    else:
                        m = (latx[i+1]-latx[i])/(lonx[i+1]-lonx[i])
                        b = latx[i] - m * lonx[i]  # y = mx + b
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
                                    locx0 = (s+1)/arc - basex
                                    locx0i = int(locx0)
                                    dotx0i = locx0-locx0i-0.25/arc  # a small bias to make sure it's in grid
                                    locx1 = locx0i + dotx0i
                                    locx1 = basex if locx1 > basex else (
                                        -basex if locx1 < -basex else locx1)
                                    locy1 = m * locx1 + b
                                    locy1 = basey if locy1 > basey else (
                                        -basey if locy1 < -basey else locy1)
                                    if (k < (leni-1)) or (stepi == 1 and locx1 < lonx[i+1]) or (stepi == -1 and locx1 > lonx[i+1]):
                                        y = gridded_arcsec(locy1, basey, arc)
                                        idx1 = np.append(
                                            idx1, [[y-mlatbase, s-mlonbase]], axis=0)
                                        loc1 = np.append(
                                            loc1, [[locx1, locy1]], axis=0)
                                        dis1 = curDist(loc1, dis1)
                                else:
                                    locy0 = (s+1)/arc - basey
                                    locy0i = int(locy0)
                                    doty0i = locy0-locy0i-0.25/arc
                                    locy1 = locy0i + doty0i
                                    locy1 = basey if locy1 > basey else (
                                        -basey if locy1 < -basey else locy1)
                                    locx1 = (locy1 - b)/m
                                    locx1 = basex if locx1 > basex else (
                                        -basex if locx1 < -basex else locx1)
                                    if (k < (leni-1)) or (stepi == 1 and locy1 < latx[i+1]) or (stepi == -1 and locy1 > latx[i+1]):
                                        x = gridded_arcsec(locx1, basex, arc)
                                        idx1 = np.append(
                                            idx1, [[s-mlatbase, x-mlonbase]], axis=0)
                                        loc1 = np.append(
                                            loc1, [[locx1, locy1]], axis=0)
                                        dis1 = curDist(loc1, dis1)

                if zmode != 'point' and i == len(lonx)-2:
                    idx1 = np.append(
                        idx1, [[latidx1-mlatbase, lonidx1-mlonbase]], axis=0)
                    loc1 = np.append(loc1, [[lonx[i+1], latx[i+1]]], axis=0)
                    dis1 = curDist(loc1, dis1)
            # end loop-i
            if brk != -1:
                dist = geodesic((latk[brk], lonk[brk]),
                                (latk[brk+1], lonk[brk+1])).km
                dis1 = np.append(dis1, dist, axis=None)
                preidx = brk+1
        # end loop-brks
        # np.savetxt("simu/test_loc.csv", loc1, delimiter=",", fmt='%f')
        # mlon0 = np.min(lonx) #may cause slice offset to mlonbase, an offset +-1
        mlon1 = np.max(lonk) + 1.5/arc   # to make it larger
        mlon1 = mlon1 if mlon1 <= basex else basex-0.00001
        # mlat0 = np.min(latx) #may cause slice offset to mlatbase, an offset +-1
        mlat1 = np.max(latk) + 1.5/arc
        mlat1 = mlat1 if mlat1 <= basey else basey-0.00001
        mlon0x = ds["lon"][mlonbase].item()
        mlat0x = ds["lat"][mlatbase].item()
        ds_s1 = ds.sel(lon=slice(mlon0x, mlon1), lat=slice(
            mlat0x, mlat1)) if subsetFlag else ds
        ds.close()
        xt1 = ds_s1['elevation'].values[tuple(idx1.T)]
        ds_s1.close()
        if format == 'row':
            # df1= pd.DataFrame({"longitude": loc1[:, 0].tolist(),
            df1 = pl.DataFrame({"longitude": loc1[:, 0].tolist(),
                                "latitude": loc1[:, 1].tolist(),
                                "z": xt1.tolist(),
                                "distance": dis1.tolist()},
                               columns=['longitude', 'latitude', 'z', 'distance'])
        else:
            out = jsonable_encoder({"longitude": loc1[:, 0].tolist(),
                                    "latitude": loc1[:, 1].tolist(),
                                    "z": xt1.tolist(),
                                    "distance": dis1.tolist()})
        # return({"data": xt1})
        # np.savetxt("simu/test_zseg.csv", pt1, delimiter=",", fmt='%f')

    # st = time.time()
    # jt1 = orjson.dumps(xt1.tolist(), option=orjson.OPT_NAIVE_UTC |
    #                   orjson.OPT_SERIALIZE_NUMPY)
    # out = jsonable_encoder({"data": xt1.tolist()})
    if format == 'row':
        # out= df1.to_dict(orient='records') #by using pandas
        out = df1.to_dicts()  # by polars
    et = time.time()
    print('Duration: execution all: ', et-st, 'sec')
    print('Size of output dataframe (row: 172 /col: 4): ',
          df1.shape[0], df1.shape[1])
    print('Last z should be -5326: ', xt1[-1])
    print('Execute times: ', gbl_count)
    return JSONResponse(content=out)