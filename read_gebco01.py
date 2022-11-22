import xarray as xr
import time
import orjson
from fastapi import FastAPI
#from loggerConfig import logger
import dask
from multiprocessing.pool import Pool
dask.config.set(pool=Pool(4))  # , scheduler='processes', num_workers=4)
# dask.set_options(get=dask.get_sync)

app = FastAPI()

# @app.on_event("startup")
# async def startup():
#    global ds
#   logger.info

# @app.on_event("shutdown")
# def release_dataset():
#    global ds
#    ds.close()


@app.get("/")
def subset():
    st = time.time()
    # ds = xr.open_mfdataset(
    #    'GEBCO_2022_sub_ice_topo.nc', chunks={0: 60, 1: 60},
    ds = xr.open_zarr(
        'GEBCO_2022_sub_ice_topo.zarr', chunks='auto', group='gebco',
        decode_cf=False, decode_times=False)
    et = time.time()
    print('1. Load dataset time: ', et-st, 'sec')

    lon0 = 105
    lon1 = 135
    lat0 = 2
    lat1 = 35
    st = time.time()
    ds_s1 = ds.sel(lon=slice(lon0, lon1), lat=slice(lat0, lat1))
    ds.close()
    et = time.time()
    print('2. Subsetting time: ', et-st, 'sec')

    # if (lon0 == lon1):
    #    def linef(x):
    #        return x
    # else:
    #    def linef()
    st = time.time()
    st1 = ds_s1.isel(lon=slice(1000, 1010), lat=slice(2000, 2010))
    ds_s1.close()
    #da = getattr(st1, 'elevation')
    # https://www.programcreek.com/python/example/123571/xarray.decode_cf
    #da.attrs['_Unsigned'] = 'false'

    xt1 = st1['elevation'].values
    et = time.time()
    print('3. Return multiple-index data time: ', et-st, 'sec')

    st = time.time()
    jt1 = orjson.dumps(xt1.tolist(), option=orjson.OPT_NAIVE_UTC |
                       orjson.OPT_SERIALIZE_NUMPY)
    et = time.time()
    print('4 Convert JSON by orjson time: ', et-st, 'sec')
    return({"data": jt1})
