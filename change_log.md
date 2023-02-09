## ver 0.1.1 port on server 20221125

## ver 0.1.2 Column(default)/row-output(row), point/line in mode(query string)

#### fix last end-point not in output

#### use geopy.geodesic to output distance between two points

#### 0.1.2.1: fix start-point in intra-segments should append distance, vertical/horizontal should judged by points, not grids

#### 0.1.2.2: fix slop>1 should use latitude-grid to count, not longitude; fix intra-segments lost/extra last point problem

#### 0.1.2.3: fix typo when two points-grid are identical (lonx -- lon) and should calculate distance (not zero)

#### 0.1.2.4: Use Polars. fix point-mode should prior to criteria of other modes

## ver 0.2 Breaking: add crossing 0-/180- (prime/anti meridian) mode 20221201

#### add gebco_v1.json for openapi(apiverse)
#### upgrade package versions (requirements.txt) 20230117

## ver 0.2.1 Upgrade to python3.10 xarray v2023.02.0 drop py3.8

#### https://github.com/pydata/xarray/releases/tag/v2023.02.0
