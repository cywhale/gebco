## ver 0.1.1 port on server 20221125

## ver 0.1.2 Column(default)/row-output(row), point/line in mode(query string)

#### fix last end-point not in output

#### use geopy.geodesic to output distance between two points

#### 0.1.2.1: fix start-point in intra-segments should append distance, vertical/horizontal should judged by points, not grids

#### 0.1.2.2: fix slop>1 should use latitude-grid to count, not longitude; fix intra-segments lost/extra last point problem

#### 0.1.2.3: fix typo when two points-grid are identical (lonx -- lon) and should calculate distance (not zero)

#### 0.1.2.4: Use Polars. fix point-mode should prior to criteria of other modes
