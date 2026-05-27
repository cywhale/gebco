from dotenv import load_dotenv
import os
load_dotenv()


def _csv_env(name, default=""):
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


host = os.getenv("HOST", "http://localhost:8013")
api_title = os.getenv("API_TITLE", "ODB API for GEBCO Bathymetry")
api_version = os.getenv("API_VERSION", "1.1.0")
api_dataset_label = os.getenv("GEBCO_DATASET_LABEL", "GEBCO_2026 Grid")
api_dataset_attribution = os.getenv(
    "GEBCO_DATASET_ATTRIBUTION",
    "GEBCO Bathymetric Compilation Group 2026(2026). "
    "The GEBCO_2026 Grid - a continuous terrain model for oceans and land "
    "at 15 arc-second intervals. NERC EDS British Oceanographic Data Centre "
    "NOC. doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa",
)
api_servers = _csv_env("API_SERVERS")

ds = None  # Declare ds as a global variable
# arcsec =  #15
arc = int(3600 / 15)  # 15 arc-second
basex = 180  # -180 - 180 <==> 0 - 360, half is 180
basey = 90   # -90 - 90 <==> 0 - 180, half is 90
# halfxidx = None #180 * arc  # in netcdf, longitude length = 86400
# halfyidx = None #90 * arc   # in netcdf, latitude length = 43200
# subsetFlag = None #True
