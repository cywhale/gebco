import xarray as xr
import numpy as np
import math
import time
#import orjson
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
#from loggerConfig import logger
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


@app.get("/")
def subset():
    global ds
    global arcsec
    global arc
    global basex
    global basey
    global halfxidx
    global halfyidx
    global subsetFlag

    st = time.time()
    # ds = xr.open_mfdataset(
    #    'GEBCO_2022_sub_ice_topo.nc', chunks={0: 60, 1: 60},
    #et = time.time()
    #print('1. Load dataset time: ', et-st, 'sec')
    lon = [123.98, 123]  # just testing
    lat = [23.33, 21.33]

    if (len(lon) != len(lat)):
        ds.close()
        print("Input Error: check longitude, latitude length not equal")
        json_data = jsonable_encoder({"data": [].tolist()})
        return JSONResponse(content=json_data)

    # May not have exact lon, lat in gridded lon-lat, so need to calculate index
    elif (len(lon) == 1):
        st = time.time()
        lon0 = gridded_arcsec(lon[0], basex, arc)
        lat0 = gridded_arcsec(lat[0], basey, arc)
        st1 = ds.isel(lon=lon0, lat=lat0)
        ds.close()
        loc1 = [lon[0], lat[0]]
        xt1 = [st1['elevation'].values]
        et = time.time()
        print('Get only one-point exe time: ', et-st, 'sec')
        # return(xt1)
    else:
        st = time.time()
        idx1 = np.empty(shape=[0, 2], dtype=np.int16)
        loc1 = np.empty(shape=[0, 2], dtype=float)
        #ele1 = np.empty(shape=[0, 1], dtype=np.int16)

        mlon0 = np.min(lon)
        #mlon1 = np.max(lon)
        mlat0 = np.min(lat)
        #mlat1 = np.max(lat)
        # if do subsetting dataset, reference-0-x,y should be biased
        mlonbase = gridded_arcsec(mlon0, basex, arc) if subsetFlag else 0
        mlatbase = gridded_arcsec(mlat0, basey, arc) if subsetFlag else 0
        # index from gridded_arcsec will change is reference is ds_s1, not ds
        #print(mlonbase, mlatbase)
        #mlonidx1 = gridded_arcsec(mlon1, basex, arc)
        #mlatidx1 = gridded_arcsec(mlat1, basey, arc)
        for i in range(len(lon)-1):
            lonidx0 = gridded_arcsec(lon[i], basex, arc)
            latidx0 = gridded_arcsec(lat[i], basey, arc)
            lonidx1 = gridded_arcsec(lon[i+1], basex, arc)
            latidx1 = gridded_arcsec(lat[i+1], basey, arc)
            if (lonidx0 == lonidx1 and latidx0 == latidx1):
                idx1 = np.append(  # Note that idx1 is latitude first for zarr, but loc1 is longitude first for geojson
                    idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                loc1 = np.append(loc1, [[lon[i], lat[i]]], axis=0)
            elif(lonidx0 == lonidx1):
                stepi = -1 if latidx0 > latidx1 else 1
                for y in range(latidx0, latidx1, stepi):
                    idx1 = np.append(
                        idx1, [[y-mlatbase, lonidx0-mlonbase]], axis=0)
                    locy0 = y/arc - basey
                    locy0i = int(locy0)
                    doty0i = locy0-locy0i-0.25/arc  # a small bias to make sure it's in grid
                    #print("yi: ", y, locy0i + doty0i)
                    loc1 = np.append(loc1, [[lon[i], locy0i + doty0i]], axis=0)
            elif(latidx0 == latidx1):
                stepi = -1 if lonidx0 > lonidx1 else 1
                for x in range(lonidx0, lonidx1, stepi):
                    idx1 = np.append(
                        idx1, [[latidx0-mlatbase, x-mlonbase]], axis=0)
                    locx0 = x/arc - basex
                    locx0i = int(locx0)
                    dotx0i = locx0-locx0i-0.25/arc  # a small bias to make sure it's in grid
                    #print("xi: ", x, locx0i + dotx0i)
                    loc1 = np.append(loc1, [[locx0i + dotx0i, lat[i]]], axis=0)
            else:
                m = (lat[i+1]-lat[i])/(lon[i+1]-lon[i])
                b = lat[i] - m * lon[i]  # y = mx + b
                #print("m, b:", m ,b)
                stepi = -1 if lonidx0 > lonidx1 else 1
                for k, x in enumerate(range(lonidx0, lonidx1, stepi)):
                    if (k == 0):  # should consider internal node not repeated twice
                        idx1 = np.append(
                            idx1, [[latidx0-mlatbase, lonidx0-mlonbase]], axis=0)
                        loc1 = np.append(loc1, [[lon[i], lat[i]]], axis=0)
                    else:
                        locx0 = x/arc - basex
                        locx0i = int(locx0)
                        dotx0i = locx0-locx0i-0.25/arc  # a small bias to make sure it's in grid
                        locx1 = locx0i + dotx0i
                        locy1 = m * locx1 + b
                        y = gridded_arcsec(locy1, basey, arc)
                        idx1 = np.append(
                            idx1, [[y-mlatbase, x-mlonbase]], axis=0)
                        loc1 = np.append(loc1, [[locx1, locy1]], axis=0)
                        #print(x, locx0, dotx0i, locx1, locy1, y)

        et = time.time()
        print('Find index time: ', et-st, 'sec')
        # print(idx1)
        # np.savetxt("simu/test_loc.csv", loc1, delimiter=",", fmt='%f')

        st = time.time()
        # mlon0 = np.min(lon) #may cause slice offset to mlonbase, an offset +-1
        mlon1 = np.max(lon) + 1.5/arc  # to make it larger
        # mlat0 = np.min(lat) #may cause slice offset to mlatbase, an offset +-1
        mlat1 = np.max(lat) + 1.5/arc
        mlon0x = ds["lon"][mlonbase]
        mlat0x = ds["lat"][mlatbase]
        ds_s1 = ds.sel(lon=slice(mlon0x, mlon1), lat=slice(
            mlat0x, mlat1)) if subsetFlag else ds
        ds.close()
        # print(ds_s1['elevation'].shape)
        xt1 = ds_s1['elevation'].values[tuple(idx1.T)]
        ds_s1.close()
        et = time.time()
        print('Get value from index of points, time: ', et-st, 'sec')
        # return({"data": xt1})
        # np.savetxt("simu/test_zseg.csv", pt1, delimiter=",", fmt='%f')

    st = time.time()
    # jt1 = orjson.dumps(xt1.tolist(), option=orjson.OPT_NAIVE_UTC |
    #                   orjson.OPT_SERIALIZE_NUMPY)
    json_data = jsonable_encoder({"data": xt1.tolist()})
    et = time.time()
    print('4 Convert JSON by fastapi: ', et-st, 'sec')
    # return({"data": jt1})
    return JSONResponse(content=json_data)