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

    -- back to numcodecs==0.15.1, which 0.16.0 cause zarr read error cannot import name cbuffer_sizes from numcodecs.blosc

#### ver 0.5.0 Breaking upgrade to GEBCO(2026). Zarr served from Blosc/lz4 for read-speed parity with 2023

    -- Data source: GEBCO_2026 sub_ice grid (doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)
    -- Verified 100k points byte-equal between NetCDF and produced Zarr
    -- 95% of API responses within 102 m of 2023 (P95, max 339 m, no extreme ice-sheet shifts)
    -- New API median latency 1.01x of 2023 (Blosc/lz4 matches old codec — Zlib was 2.66x slower)
    -- Add dev2026/ tooling tree (uv + Python 3.13) and AGENTS.md handover notes
    -- Add /AGENTS.md as the single handover doc for AI coding agents
    -- Polygon mode (/gebco?jsonsrc=...) regression covered at two levels:
       full-resolution mask consistency (verify_polygon_meridian.py) and
       real-polyhandler endpoint regression with production sample=5
       (verify_polyhandler_endpoint.py) — T1 2304 rows P95=155m,
       T4 (crosses 180°) 1152 rows P95=195m, both pass on a clean
       dev2026 `uv sync` env (polars 1.41 + shapely 2.x).
    -- Deferred to v0.5.1 (migration / housekeeping, not behaviour change):
       port src/polyhandler.py off pygeos onto native Shapely 2 APIs
       (dev2026/scripts/_pygeos_shim.py is the transitional bridge); revisit
       polars pin if needed. v0.5.0 ships with the shim in place — reviewer
       confirmed the real polyhandler path runs unmodified through it.
