import pygeos
import polars as pl
import numpy as np
import json
from shapely.geometry import shape, Polygon, LineString
from shapely.ops import split
from src.zprofile import zprofile, zdata_bbox
from src.xmeridian import whichSide
import src.config as config


def transform_coords_to_360(polygon):
    """Transform polygon coordinates from -180 - 180 to 0 - 360 range."""

    def transform_coord(lon, lat):
        return [(lon + 360 if lon < 0 else lon), lat]

    # here polygon not from a shape(geom) but already a geometry, so that cannot use polygon.exterior.coords
    transformed_coords = [
        transform_coord(lon, lat) for lon, lat in pygeos.get_coordinates(polygon)
    ]
    return Polygon(transformed_coords)


def is_right_polygon(polygon):
    """Check if the polygon is the right part (east of the 180-degree meridian)."""
    return all(0 <= lon <= 180 for lon, _ in polygon.exterior.coords)


def transform_back_to_180(coords, isRight=False):
    """Transform coordinates back to -180 to 180 range."""
    arc = config.arc

    def transform(lon):
        if isRight:
            return 180 - 0.01 / arc if lon == 180 else lon
        else:
            return (
                -180 + 0.01 / arc if lon == 180 else (lon - 360 if lon > 180 else lon)
            )

    return [[transform(lon), lat] for lon, lat in coords]


def split_polygon_at_180(polygon):
    """Split a polygon at the 180-degree longitude line in 0-360 coordinate system."""
    transformed_polygon = transform_coords_to_360(polygon)
    splitter = LineString([(180, 90), (180, -90)])
    return split(transformed_polygon, splitter)


def coords_zprof(coords, line_id, mode, sample):
    # locx, locy = coords[:, 0], coords[:, 1]
    zout = zprofile(
        loni=coords[:, 0],  # ",".join(str(x) for x in locx),
        lati=coords[:, 1],  # ",".join(str(y) for y in locy),
        mode=mode,
        sample=sample,
    )
    # Add line_id column to the DataFrame
    zout = zout.with_columns(pl.lit(line_id).alias("lineid"))
    return zout


def process_linestring(line, line_id, mode, sample):
    coordx = pygeos.get_coordinates(line)
    return coords_zprof(coordx, line_id, mode, sample)


def process_polygon_part(
    polygon, line_id, mode, crosses_180=False, isRight=False, poly_sample=5
):
    if crosses_180:
        trans_coords = transform_back_to_180(polygon.exterior.coords, isRight)
    else:
        trans_coords = transform_back_to_180(pygeos.get_coordinates(polygon), isRight)
    trans_poly = pygeos.from_shapely(Polygon(trans_coords))
    minx, miny, maxx, maxy = pygeos.bounds(trans_poly)
    subset_data = zdata_bbox(
        (minx, miny, maxx, maxy), crosses_180, isRight, poly_sample
    )

    # Create a mask for data points within the polygon
    lons, lats = np.meshgrid(subset_data.lon, subset_data.lat)
    points = pygeos.points(lons.ravel(), lats.ravel())
    mask = pygeos.contains(trans_poly, points)
    mask_reshaped = mask.reshape(lats.shape)

    # Apply mask and create DataFrame
    elevation_data = subset_data.elevation.values[mask_reshaped]
    masked_lons, masked_lats = lons[mask_reshaped], lats[mask_reshaped]

    # Transform longitude to 0-360 range if 'lon360' is in mode
    if crosses_180 and "lon360" in mode:
        masked_lons = np.where(masked_lons < 0, masked_lons + 360, masked_lons)

    # Adjust sorting order
    sort_descending = [crosses_180 and "lon360" not in mode, True]

    df = pl.DataFrame(
        {
            "longitude": masked_lons.ravel(),
            "latitude": masked_lats.ravel(),
            "z": elevation_data.ravel(),
        }
    ).sort(["longitude", "latitude"], descending=sort_descending)

    if "zonly" in mode:
        append_cols = [pl.lit(line_id).cast(pl.Int16).alias("lineid")]
    else:
        append_cols = [
            pl.lit(None).cast(pl.Float64).alias("distance"),
            pl.lit(line_id).cast(pl.Int16).alias("lineid"),
        ]

    df = df.with_columns(append_cols)
    return df


def process_polygon(polygon, line_id, mode, poly_sample):
    # pygeos_poly = pygeos.from_shapely(polygon)
    minx, miny, maxx, maxy = pygeos.bounds(polygon)
    # crosses_180 = minx < -170 and maxx > 170
    crosses_180 = whichSide([minx], [maxx]) == "away-zero"

    if crosses_180:
        split_poly = split_polygon_at_180(polygon)
        # dataframes=[]
        left_df, right_df = None, None

        for part in split_poly.geoms:
            if part.is_empty:
                continue
            isRight = is_right_polygon(part) if crosses_180 else False
            df = process_polygon_part(
                part, line_id, mode, crosses_180, isRight, poly_sample
            )
            # dataframes.append(df)
            if isRight:
                right_df = df
            else:
                left_df = df
        # df = pl.concat(dataframes)
        # Determine the order of concatenation based on mode
        if "lon360" in mode:
            df = (
                pl.concat([right_df, left_df])
                if right_df is not None and left_df is not None
                else (right_df or left_df)
            )
        else:
            df = (
                pl.concat([left_df, right_df])
                if right_df is not None and left_df is not None
                else (right_df or left_df)
            )
    else:
        df = process_polygon_part(polygon, line_id, mode, False, poly_sample)

    return df


def polyhandler(geojson_input, line_id=0, mode="", sample=1, poly_sample=5):
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
                        df = coords_zprof(np.array(pts_coords), line_id, mode, sample)
                        dataframes.append(df)
                        line_id += 1
                        pts_coords = []

                    geometry = pygeos.from_shapely(shape(geom))
                    df, line_id = polyhandler(
                        geometry, line_id, mode, sample, poly_sample
                    )
                    dataframes.append(df)

            if len(pts_coords) > 0:
                df = coords_zprof(np.array(pts_coords), line_id, mode, sample)
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
            df = process_linestring(geometry, line_id, mode, sample)
            dataframes.append(df)
            line_id += 1
        elif geom_type == 3:  # POLYGON
            df = process_polygon(geometry, line_id, mode, poly_sample)
            dataframes.append(df)
            line_id += 1
        elif geom_type in [5]:  # MULTILINESTRING
            for part in pygeos.get_parts(geometry):
                df = process_linestring(part, line_id, mode, sample)
                dataframes.append(df)
                line_id += 1
        elif geom_type == 6:  # MULTIPOLYGON
            for polygon in pygeos.get_parts(geometry):
                df = process_polygon(polygon, line_id, mode, poly_sample)
                dataframes.append(df)
                line_id += 1
        elif geom_type == 7:  # GEOMETRYCOLLECTION
            for part in pygeos.get_parts(geometry):
                part_df, line_id = polyhandler(part, line_id, mode, sample, poly_sample)
                dataframes.append(part_df)

    if dataframes:
        return pl.concat([df.cast(consistent_schema) for df in dataframes]), line_id
    else:
        return pl.DataFrame([], schema=consistent_schema), line_id
