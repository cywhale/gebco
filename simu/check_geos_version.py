import shapely.geos
import pygeos

print(shapely.geos.geos_version_string)
print(pygeos.geos_version_string)

#sudo apt-get update
#sudo apt-get install libgeos-dev
#sudo updatedb  # Update the database for 'locate'
#locate libgeos_c.so
#export GEOS_INCLUDE_PATH=/usr/include/geos
#export GEOS_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu
#pip install --force-reinstall --no-binary shapely shapely
#pip install --force-reinstall --no-binary pygeos pygeos
