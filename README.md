# FastAPI of GEBCO bathymetry

[![DOI](https://zenodo.org/badge/doi/10.5281/zenodo.7502986.svg)](https://doi.org/10.5281/zenodo.7502986)

#### Swagger API doc

https://api.odb.ntu.edu.tw/gebco/swagger

#### Usage
1. Records-output (in JSON) of z-profile (and distances) between longitude/latitude points with 15-arcsec resolutions

    e.g: https://api.odb.ntu.edu.tw/gebco?lon=120,120.1,120.1&lat=25.0,25.1,24.9&mode=row
    
2. Output only endpoints: &mode=row,point


#### Attribution

* Data source

    GEBCO Compilation Group (2023) GEBCO 2023 Grid (doi:10.5285/f98b053b-0cbc-6c23-e053-6c86abc0af7b)
    
    https://www.gebco.net/data_and_products/gridded_bathymetry_data/


    
