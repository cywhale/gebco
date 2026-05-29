from pathlib import Path

from dotenv import load_dotenv
import os

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


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

ds = None  # Declare ds as a global variable; populated by gebco_app.lifespan()
# arcsec =  #15
arc = int(3600 / 15)  # 15 arc-second
basex = 180  # -180 - 180 <==> 0 - 360, half is 180
basey = 90   # -90 - 90 <==> 0 - 180, half is 90
# halfxidx = None #180 * arc  # in netcdf, longitude length = 86400
# halfyidx = None #90 * arc   # in netcdf, latitude length = 43200
# subsetFlag = None #True

# --- v0.5.3 hardening: env-controlled runtime knobs -----------------------
# H6 — bbox cell-count caps (line/point and polygon paths).
MAX_BBOX_CELLS_LINE = int(os.getenv("GEBCO_MAX_BBOX_CELLS_LINE", "200000000"))     # 2e8
MAX_POLYGON_CELLS = int(os.getenv("GEBCO_MAX_POLYGON_CELLS", "50000000"))          # 5e7

# H1 — jsonsrc URL fetch hardening.
JSONSRC_ALLOW_REMOTE = os.getenv("GEBCO_JSONSRC_ALLOW_REMOTE", "true").lower() != "false"
JSONSRC_MAX_BYTES = int(os.getenv("GEBCO_JSONSRC_MAX_BYTES", "2000000"))           # 2 MB
JSONSRC_CONNECT_TIMEOUT_S = float(os.getenv("GEBCO_JSONSRC_CONNECT_TIMEOUT_S", "2"))
JSONSRC_READ_TIMEOUT_S = float(os.getenv("GEBCO_JSONSRC_READ_TIMEOUT_S", "5"))

# H10 — dask multiprocessing pool size. 0/1 disables the pool; v0.5.3 default
# preserves v0.5.2 behaviour with pool size 4.
DASK_POOL_SIZE = int(os.getenv("GEBCO_DASK_POOL_SIZE", "4"))

# v0.5.4 W2-B — polygon row-batch budget, measured in CELLS per batch
# (not latitude rows). The polyhandler row-batch code computes
# ``batch_lats = max(1, POLYGON_BATCH_CELLS // n_lons)`` so the batch
# height auto-scales to the bbox shape. This is the right knob for the
# Phase D2 thin / cross180_thin archetypes where bbox is very narrow in
# one dimension and a row-count knob (the v0.5.3 initial design) was
# moot — the polygon already fits in one batch.
#
# Default 1_000_000 keeps per-batch scratch in the low-hundreds-of-MB
# range on the Phase G archetypes while still passing the thin/cross-180
# wall-time targets. Set to 0 to disable batching entirely (one giant
# meshgrid, like v0.5.3).
POLYGON_BATCH_CELLS = max(0, int(os.getenv("GEBCO_POLYGON_BATCH_CELLS", "1000000")))

# H13 — structured logging.
LOG_LEVEL = os.getenv("GEBCO_LOG_LEVEL", "WARNING").upper()
LOG_SAMPLE_RATE = float(os.getenv("GEBCO_LOG_SAMPLE_RATE", "1.0"))
SLOW_REQUEST_MS = int(os.getenv("GEBCO_SLOW_REQUEST_MS", "1000"))
