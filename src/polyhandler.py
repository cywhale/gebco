import pygeos
import polars as pl
import numpy as np
import json
from shapely.geometry import shape
from src.zprofile import zprofile, zdata_bbox


def coords_zprof(coords, line_id, mode):
    # locx, locy = coords[:, 0], coords[:, 1]
    zout = zprofile(
        loni=coords[:, 0],  # ",".join(str(x) for x in locx),
        lati=coords[:, 1],  # ",".join(str(y) for y in locy),
        mode=mode,
    )
    # Add line_id column to the DataFrame
    zout = zout.with_columns(pl.lit(line_id).alias("lineid"))
    return zout


def process_linestring(line, line_id, mode):
    coordx = pygeos.get_coordinates(line)
    return coords_zprof(coordx, line_id, mode)


def process_polygon(polygon, line_id, mode):
    # pygeos_poly = pygeos.from_shapely(polygon)
    # Extract bounding box and subset the Zarr dataset
    minx, miny, maxx, maxy = pygeos.bounds(polygon)
    subset_data = zdata_bbox((minx, miny, maxx, maxy))

    # Create a mask for data points within the polygon
    lons, lats = np.meshgrid(subset_data.lon, subset_data.lat)
    points = pygeos.points(lons.ravel(), lats.ravel())
    mask = pygeos.contains(polygon, points)  # .within(points, polygon)
    # Reshape the mask to match the dimensions of the elevation data
    mask_reshaped = mask.reshape(lats.shape)

    # Apply mask and create DataFrame
    elevation_data = subset_data.elevation.values[mask_reshaped]
    masked_lons, masked_lats = lons[mask_reshaped], lats[mask_reshaped]
    if "zonly" in mode:
        df = (
            pl.DataFrame(
                {
                    "longitude": masked_lons.ravel(),
                    "latitude": masked_lats.ravel(),
                    "z": elevation_data.ravel(),
                }
            )
            .sort("longitude", "latitude", descending=[False, True])
            .with_columns(
                [
                    pl.lit(line_id).cast(pl.Int16).alias("lineid"),
                ]
            )
        )
    else:
        df = (
            pl.DataFrame(
                {
                    "longitude": masked_lons.ravel(),
                    "latitude": masked_lats.ravel(),
                    "z": elevation_data.ravel(),
                }
            )
            .sort("longitude", "latitude", descending=[False, True])
            .with_columns(
                [
                    pl.lit(None).cast(pl.Float64).alias("distance"),
                    pl.lit(line_id).cast(pl.Int16).alias("lineid"),
                ]
            )
        )
    return df


def polyhandler(geojson_input, line_id=0, mode=""):
    dataframes = []
    hasFeature = False
    mode = (
        "dataframe"
        if mode is None
        else (mode if "dataframe" in mode else mode + ",dataframe")
    )
    if "zonly" in mode.lower():
        consistent_schema = {
            "longitude": pl.Float64,
            "latitude": pl.Float64,
            "z": pl.Float64,
            "lineid": pl.Int16,
        }
    else:
        consistent_schema = {
            "longitude": pl.Float64,
            "latitude": pl.Float64,
            "z": pl.Float64,
            "distance": pl.Float64,
            "lineid": pl.Int16,
        }

    if isinstance(geojson_input, pygeos.lib.Geometry):
        geometry = geojson_input
    elif "type" in geojson_input or isinstance(geojson_input, str):
        if isinstance(geojson_input, str):
            geojson = json.loads(geojson_input)
        else:
            geojson = geojson_input

        if geojson["type"] == "FeatureCollection":
            hasFeature = True
            pts_coords = []
            # print("Got Feature collection and 0: ", geojson["features"][0]["geometry"])
            for feature in geojson["features"]:
                geom = feature["geometry"]
                if (
                    geom["type"] == "Point"
                    and mode is not None
                    and "connect_pt" in mode.lower()
                ):
                    pts_coords.append(geom["coordinates"])
                elif (
                    geom["type"] == "MultiPoint"
                    and mode is not None
                    and "connect_pts" in mode.lower()
                ):
                    pts_coords.extend(geom["coordinates"])
                else:
                    # print("Feature in collection: ", geom["type"], " and now pts_coords: ", pts_coords)
                    if len(pts_coords) > 0:
                        df = coords_zprof(np.array(pts_coords), line_id, mode)
                        dataframes.append(df)
                        line_id += 1
                        pts_coords = []

                    geometry = pygeos.from_shapely(shape(geom))
                    df, line_id = polyhandler(geometry, line_id, mode)
                    dataframes.append(df)

            if len(pts_coords) > 0:
                df = coords_zprof(np.array(pts_coords), line_id, mode)
                dataframes.append(df)
                line_id += 1
        else:
            geometry = pygeos.from_shapely(shape(geojson))
    else:
        raise ValueError("Input must be a GeoJSON object or a PyGEOS Geometry")

    if not hasFeature:
        geom_type = pygeos.get_type_id(geometry)
        print(
            "Got geometry: ",
            geometry,
            " with type id: ",
            geom_type,
            " with mode: ",
            mode,
        )
        # interval = 15 / 3600  # 15 arc-seconds in degrees

        if geom_type in [0, 1, 2, 4]:  # POINT, LINESTRING, LINEARRING, MULTIPOINT
            df = process_linestring(geometry, line_id, mode)
            dataframes.append(df)
            line_id += 1
        elif geom_type == 3:  # POLYGON
            df = process_polygon(geometry, line_id, mode)
            dataframes.append(df)
            line_id += 1
        elif geom_type in [5]:  # MULTILINESTRING
            for part in pygeos.get_parts(geometry):
                df = process_linestring(part, line_id, mode)
                dataframes.append(df)
                line_id += 1
        elif geom_type == 6:  # MULTIPOLYGON
            for polygon in pygeos.get_parts(geometry):
                df = process_polygon(polygon, line_id, mode)
                dataframes.append(df)
                line_id += 1
        elif geom_type == 7:  # GEOMETRYCOLLECTION
            for part in pygeos.get_parts(geometry):
                part_df, line_id = polyhandler(part, line_id, mode)
                dataframes.append(part_df)

    if dataframes:
        return pl.concat([df.cast(consistent_schema) for df in dataframes]), line_id
    else:
        return pl.DataFrame([], schema=consistent_schema), line_id
