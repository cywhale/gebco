# FastAPI of GEBCO bathymetry

[![DOI](https://zenodo.org/badge/doi/10.5281/zenodo.7502986.svg)](https://doi.org/10.5281/zenodo.7502986)

#### Swagger API doc

<a href="https://api.odb.ntu.edu.tw/hub/swagger?node=odb_gebco_v1" target="_blank">GEBCO API manual/online try-out</a>

#### Usage

1. Records-output (in JSON) of z-profile (and distances) between longitude/latitude points with 15-arcsec resolutions

    e.g: https://api.odb.ntu.edu.tw/gebco?lon=120,120.1,120.1&lat=25.0,25.1,24.9&mode=row
    
2. Output only endpoints: &mode=row,point

3. **Polygon** mode by `jsonsrc` parameter (default down-sampling every 5 points, see `sample` parameter in manual) e.g

```
   https://api.odb.ntu.edu.tw/gebco?mode=zonly&jsonsrc={"type": "Polygon", "coordinates": [[[121, 22.5], [121, 23.5], [122, 23.5], [122, 22.5], [121, 22.5]]]}
```

   * Parmeter `mode=zonly` means pairwise distance evaluation/output is disabled, only evaluates z data. Multiple values separated by comma in `mode` is allowed.
   * Multiple features are allowed:

```
   https://api.odb.ntu.edu.tw/gebco?mode=zonly&jsonsrc={"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": { "type": "Polygon", "coordinates": [[[121, 22.5], [121, 23.5], [122, 23.5], [122, 22.5], [121, 22.5]]]}}]}
```

#### Demo polygon-mode data by plotly on <a href="https://api.odb.ntu.edu.tw/hub" target="_blank">Ocean APIverse</a>

<p align="center"><img src="https://github.com/cywhale/ODB/blob/master/img/gebco_terrain3D_202401.png" width=540 alt="Polygon-mode data by GEBCO API" /></p>

*Given the polygon in GeoJSON, the 15 arc-seconds gridded GEBCO data can be obtained to present the 3D terrain of this area.*


#### Attribution

* Data source

    GEBCO Bathymetric Compilation Group 2026(2026). The GEBCO_2026 Grid - a continuous terrain model for oceans and land at 15 arc-second intervals. NERC EDS British Oceanographic Data Centre NOC. doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa
    
    https://www.gebco.net/data-products-gridded-bathymetry-data/gebco2026-grid

#### Citation

* This API is compiled by [Ocean Data Bank](https://www.odb.ntu.edu.tw) (ODB), and can be cited as:

    Ocean Data Bank, National Science and Technology Council, Taiwan. https://doi.org/10.5281/zenodo.7512112. Accessed DAY/MONTH/YEAR from api.odb.ntu.edu.tw/gebco. v1.1.0.



    
