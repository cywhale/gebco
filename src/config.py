from dotenv import load_dotenv
import os
load_dotenv()
host = os.getenv('HOST', "http://localhost:8013")
ds = None  # Declare ds as a global variable
# arcsec =  #15
arc = int(3600 / 15)  # 15 arc-second
basex = 180  # -180 - 180 <==> 0 - 360, half is 180
basey = 90   # -90 - 90 <==> 0 - 180, half is 90
# halfxidx = None #180 * arc  # in netcdf, longitude length = 86400
# halfyidx = None #90 * arc   # in netcdf, latitude length = 43200
# subsetFlag = None #True
