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
