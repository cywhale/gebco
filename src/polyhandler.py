import gc
import polars as pl
import numpy as np
import json
import shapely
from shapely.geometry import shape, Polygon, LineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import split
from src.zprofile import zprofile, zdata_bbox
from src.xmeridian import whichSide
from src.modes import parse_modes
import src.config as config


def transform_coords_to_360(polygon):
    """Transform polygon coordinates from -180 - 180 to 0 - 360 range."""

    def transform_coord(lon, lat):
        return [(lon + 360 if lon < 0 else lon), lat]

    # here polygon not from a shape(geom) but already a geometry, so that cannot use polygon.exterior.coords
    transformed_coords = [
        transform_coord(lon, lat) for lon, lat in shapely.get_coordinates(polygon)
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


def coords_zprof(coords, line_id, mode, sample, *, stats_out=None):
    # locx, locy = coords[:, 0], coords[:, 1]
    zout = zprofile(
        loni=coords[:, 0],  # ",".join(str(x) for x in locx),
        lati=coords[:, 1],  # ",".join(str(y) for y in locy),
        mode=mode,
        sample=sample,
        stats_out=stats_out,
    )
    # Add line_id column to the DataFrame
    zout = zout.with_columns(pl.lit(line_id).alias("lineid"))
    return zout


def process_linestring(line, line_id, mode, sample, *, stats_list=None):
    coordx = shapely.get_coordinates(line)
    part_stats = {} if stats_list is not None else None
    df = coords_zprof(coordx, line_id, mode, sample, stats_out=part_stats)
    if stats_list is not None:
        stats_list.append(part_stats)
    return df


def process_polygon_part(
    polygon, line_id, mode, crosses_180=False, isRight=False, poly_sample=5
):
    """Build the per-cell DataFrame for a polygon (or polygon half on
    cross-180 splits).

    v0.5.4 W2-B — instead of one ``meshgrid + shapely.points + contains``
    call over the entire strided bbox (which forced peak RSS to scale
    with the bbox and made the cross-180 thin case allocate ~8 GB for
    only ~132k output rows — see ``dev2026/TESTING.md`` Phase D2), we now
    process the bbox in latitude-row batches of size
    ``GEBCO_POLYGON_ROW_BATCH`` (default 5000). Each batch builds its own
    meshgrid + shapely points + contains mask, harvests the matched
    cells into output buffers, then frees the per-batch temporaries.

    Semantics are byte-equal to v0.5.3:
      * the cells selected are exactly those whose centres fall inside
        ``trans_poly`` (``shapely.contains`` is run on each cell centre
        independently, same as before);
      * lon360 normalisation, sort order, and ``lineid`` / ``distance``
        columns are applied after concatenation, so column shape /
        dtype match v0.5.3 exactly;
      * empty polygons (zero matches) return the same empty-schema
        DataFrame the legacy code produced after sort.
    """
    # v0.5.3 H4: cache parsed mode tokens once per call.
    _modes = parse_modes(mode)
    if crosses_180:
        trans_coords = transform_back_to_180(polygon.exterior.coords, isRight)
    else:
        trans_coords = transform_back_to_180(shapely.get_coordinates(polygon), isRight)
    trans_poly = Polygon(trans_coords)
    shapely.prepare(trans_poly)
    minx, miny, maxx, maxy = shapely.bounds(trans_poly)
    subset_data = zdata_bbox(
        (minx, miny, maxx, maxy), crosses_180, isRight, poly_sample
    )

    # v0.5.4 W2-B: read coords + elevation values once; the elevation
    # array is the bbox slice from the Zarr (already strided by
    # poly_sample), so this materialises only the bbox once instead of
    # once per batch.
    lon_vals = subset_data.lon.values
    lat_vals = subset_data.lat.values
    elev_vals = subset_data.elevation.values
    n_lats = lat_vals.size
    n_lons = lon_vals.size

    # Output buffers — each entry contains the matched cells from one
    # row-batch. We use lists of ndarrays + np.concatenate at the end so
    # peak RSS during accumulation is bounded by the buffers, not the
    # whole bbox.
    lon_chunks: list = []
    lat_chunks: list = []
    z_chunks: list = []

    # v0.5.4 W2-B — cell-budget batching. ``POLYGON_BATCH_CELLS`` caps
    # the per-batch cell count regardless of the bbox aspect ratio, so
    # thin polygons (e.g. D2 cross180_thin's 14.2°×28.4° half-bbox)
    # actually get fragmented instead of fitting in one giant batch.
    # ``GEBCO_POLYGON_BATCH_CELLS == 0`` disables batching entirely
    # (matches the v0.5.3 single-meshgrid behaviour for A/B comparison).
    batch_cells = config.POLYGON_BATCH_CELLS
    if batch_cells <= 0 or n_lons == 0:
        batch_lats = n_lats if n_lats else 1
    else:
        batch_lats = max(1, batch_cells // n_lons)

    # v0.5.4 round 4 — guard gc.collect() to the multi-batch case ONLY.
    # The live A/B test against the deployed v0.5.2 endpoint
    # (ecodata.odb.ntu.edu.tw) showed a ~3-4× polygon regression for
    # small polygons that fit in a single batch (e.g. 0.5° Taiwan EEZ
    # polygon at sample=1: 117 ms v0.5.4 vs 41 ms v0.5.2). Root cause:
    # an unconditional `gc.collect()` at the end of each row-batch
    # iteration walks the entire gunicorn worker heap (xarray + dask +
    # polars + zarr) which dominates wall time when there is only one
    # batch to process. The gc was added to bound peak RSS during the
    # cross180_thin worst case (24+ batches); single-batch polygons
    # don't need it. We now compute the batch count once and gate gc on
    # `multi_batch` so the cross-180 RSS protection stays in place
    # without taxing the common small-polygon path.
    n_batches = (n_lats + batch_lats - 1) // batch_lats if batch_lats > 0 else 1
    multi_batch = n_batches > 1

    for row_start in range(0, n_lats, batch_lats):
        row_end = min(row_start + batch_lats, n_lats)
        chunk_lats = lat_vals[row_start:row_end]
        # Cheap bbox prefilter on latitude — if the polygon's lat extent
        # doesn't intersect this row batch, skip it entirely without
        # building any shapely objects.
        if chunk_lats.max() < miny or chunk_lats.min() > maxy:
            continue
        chunk_lon_mesh, chunk_lat_mesh = np.meshgrid(lon_vals, chunk_lats)
        # v0.5.4 W2-B round 3 — `shapely.contains_xy(geom, x_array, y_array)`
        # vectorises into GEOS directly from numpy arrays without
        # constructing one ``shapely.Point`` per cell. For a 1M-cell
        # batch this is ~12x faster than `shapely.points + shapely.contains`
        # AND drops the per-batch RSS spike from ~80 MB of Point PyObjects
        # to ~8 MB of mask + meshgrid scratch. Output is byte-equal to
        # the pre-round-3 mask (verified on a 1M-point uniform sample).
        flat_lon = chunk_lon_mesh.ravel()
        flat_lat = chunk_lat_mesh.ravel()
        mask = shapely.contains_xy(trans_poly, flat_lon, flat_lat)
        if not mask.any():
            del chunk_lon_mesh, chunk_lat_mesh, flat_lon, flat_lat, mask
            if multi_batch:
                gc.collect()
            continue
        mask_2d = mask.reshape(chunk_lon_mesh.shape)
        # Harvest matched cells. elev_vals[row_start:row_end] is itself a
        # view into the bbox-resolved numpy array, so no extra copy here.
        z_chunks.append(elev_vals[row_start:row_end][mask_2d])
        lon_chunks.append(chunk_lon_mesh[mask_2d])
        lat_chunks.append(chunk_lat_mesh[mask_2d])
        # explicit del + (conditional) gc.collect — `del` removes refs;
        # gc.collect forces immediate cycle reclaim of GEOS / numpy
        # internals to bound RSS across multi-batch runs. For
        # single-batch polygons (small / medium inputs at sample=5),
        # gc.collect costs more than it saves on a busy gunicorn worker
        # heap (see live A/B comment in the multi_batch computation
        # above); skipping it there preserves the v0.5.2 latency.
        del chunk_lon_mesh, chunk_lat_mesh, mask, mask_2d, flat_lon, flat_lat
        if multi_batch:
            gc.collect()

    if lon_chunks:
        masked_lons = np.concatenate(lon_chunks)
        masked_lats = np.concatenate(lat_chunks)
        elevation_data = np.concatenate(z_chunks)
    else:
        # Match the legacy empty-bbox dtype / shape exactly so the
        # downstream pl.DataFrame builds an empty frame with the same
        # column schema rather than raising.
        masked_lons = np.empty((0,), dtype=lon_vals.dtype)
        masked_lats = np.empty((0,), dtype=lat_vals.dtype)
        elevation_data = np.empty((0,), dtype=elev_vals.dtype)

    # Transform longitude to 0-360 range if 'lon360' is in mode
    if crosses_180 and "lon360" in _modes:
        masked_lons = np.where(masked_lons < 0, masked_lons + 360, masked_lons)

    # Adjust sorting order
    sort_descending = [crosses_180 and "lon360" not in _modes, True]

    df = pl.DataFrame(
        {
            "longitude": masked_lons.ravel(),
            "latitude": masked_lats.ravel(),
            "z": elevation_data.ravel(),
        }
    ).sort(["longitude", "latitude"], descending=sort_descending)

    # Conditionally include "lineid" only if "lineid" mode is enabled
    append_cols = []
    if "lineid" in _modes:
        append_cols.append(pl.lit(line_id).cast(pl.Int16).alias("lineid"))

    # Add distance column if not in "zonly" mode
    if "zonly" not in _modes:
        append_cols.append(pl.lit(None).cast(pl.Float64).alias("distance"))

    if append_cols:
        df = df.with_columns(append_cols)

    return df


def process_polygon(polygon, line_id, mode, poly_sample):
    _modes = parse_modes(mode)  # v0.5.3 H4
    minx, miny, maxx, maxy = shapely.bounds(polygon)
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
        if "lon360" in _modes:
            if right_df is not None and left_df is not None:
                df = pl.concat([right_df, left_df])
            elif right_df is not None:
                df = right_df
            else:
                df = left_df
        else:
            if right_df is not None and left_df is not None:
                df = pl.concat([left_df, right_df])
            elif left_df is not None:
                df = left_df
            else:
                df = right_df
    else:
        df = process_polygon_part(polygon, line_id, mode, False, False, poly_sample)

    return df


def _finalize_line_stats(stats_out: dict, line_stats: list[dict]) -> None:
    if not line_stats:
        return
    stats_out["multi_parts"] = len(line_stats)
    stats_out["touched_chunks_sum"] = int(sum(s.get("touched_chunks", 0) for s in line_stats))
    stats_out["touched_chunks_max"] = int(max(s.get("touched_chunks", 0) for s in line_stats))
    stats_out["bbox_subset_cells_sum"] = int(sum(s.get("bbox_subset_cells", 0) for s in line_stats))
    stats_out["projected_bytes_sum"] = int(sum(s.get("projected_bytes", 0) for s in line_stats))
    stats_out["projected_bytes_max"] = int(max(s.get("projected_bytes", 0) for s in line_stats))
    stats_out["output_rows_sum"] = int(sum(s.get("output_rows", 0) for s in line_stats))
    stats_out["output_rows_max"] = int(max(s.get("output_rows", 0) for s in line_stats))
    stats_out["any_sparse"] = any(s.get("use_sparse", False) for s in line_stats)


def polyhandler(
    geojson_input,
    line_id=0,
    mode="",
    sample=1,
    poly_sample=5,
    *,
    stats_out=None,
    _stats_root=True,
):
    dataframes = []
    hasFeature = False
    line_stats = None
    if stats_out is not None:
        line_stats = stats_out.setdefault("_line_parts", [])
    # v0.5.3 H4: ensure "dataframe" is in the mode string using set membership;
    # `mode` stays a string because it's passed through to internal calls and
    # recursive polyhandler() invocations that downstream parse_modes() again.
    _modes = parse_modes(mode)
    if "dataframe" not in _modes:
        mode = "dataframe" if not mode else mode + ",dataframe"
        _modes = parse_modes(mode)

    # Define schema dynamically based on "lineid" mode
    consistent_schema = {
        "longitude": pl.Float64,
        "latitude": pl.Float64,
        "z": pl.Float64,
    }

    if "zonly" not in _modes:
        consistent_schema["distance"] = pl.Float64

    if "lineid" in _modes:
        consistent_schema["lineid"] = pl.Int16

    if isinstance(geojson_input, BaseGeometry):
        geometry = geojson_input
    elif "type" in geojson_input or isinstance(geojson_input, str):
        if isinstance(geojson_input, str):
            geojson = json.loads(geojson_input)
        else:
            geojson = geojson_input

        if geojson["type"] == "FeatureCollection" or geojson["type"] == "Feature":
            hasFeature = True
            pts_coords = []
            if geojson["type"] == "FeatureCollection":
                geometries = geojson["features"]
            else:
                geometries = [geojson]
            # print("Got Feature collection and 0: ", geojson["features"][0]["geometry"])
            for feature in geometries:
                geom = feature["geometry"]
                if geom["type"] == "Point" and "connect_pt" in _modes:
                    pts_coords.append(geom["coordinates"])
                elif geom["type"] == "MultiPoint" and "connect_pts" in _modes:
                    pts_coords.extend(geom["coordinates"])
                else:
                    # print("Feature in collection: ", geom["type"], " and now pts_coords: ", pts_coords)
                    if len(pts_coords) > 0:
                        part_stats = {} if line_stats is not None else None
                        df = coords_zprof(
                            np.array(pts_coords), line_id, mode, sample, stats_out=part_stats
                        )
                        if line_stats is not None:
                            line_stats.append(part_stats)
                        dataframes.append(df)
                        line_id += 1
                        pts_coords = []

                    geometry = shape(geom)
                    df, line_id = polyhandler(
                        geometry, line_id, mode, sample, poly_sample, stats_out=stats_out, _stats_root=False
                    )
                    dataframes.append(df)

            if len(pts_coords) > 0:
                part_stats = {} if line_stats is not None else None
                df = coords_zprof(np.array(pts_coords), line_id, mode, sample, stats_out=part_stats)
                if line_stats is not None:
                    line_stats.append(part_stats)
                dataframes.append(df)
                line_id += 1
        else:
            geometry = shape(geojson)
    else:
        raise ValueError("Input must be a GeoJSON object or a Shapely Geometry")

    if not hasFeature:
        geom_type = geometry.geom_type

        if geom_type in {"Point", "LineString", "LinearRing", "MultiPoint"}:
            df = process_linestring(geometry, line_id, mode, sample, stats_list=line_stats)
            dataframes.append(df)
            line_id += 1
        elif geom_type == "Polygon":
            df = process_polygon(geometry, line_id, mode, poly_sample)
            dataframes.append(df)
            line_id += 1
        elif geom_type == "MultiLineString":
            for part in geometry.geoms:
                df = process_linestring(part, line_id, mode, sample, stats_list=line_stats)
                dataframes.append(df)
                line_id += 1
        elif geom_type == "MultiPolygon":
            for polygon in geometry.geoms:
                df = process_polygon(polygon, line_id, mode, poly_sample)
                dataframes.append(df)
                line_id += 1
        elif geom_type == "GeometryCollection":
            for part in geometry.geoms:
                part_df, line_id = polyhandler(
                    part, line_id, mode, sample, poly_sample, stats_out=stats_out, _stats_root=False
                )
                dataframes.append(part_df)

    if dataframes:
        # return pl.concat([df.cast(consistent_schema) for df in dataframes]), line_id
        df = pl.concat([df.cast(consistent_schema) for df in dataframes])
        # 202502 add truncated mode: Apply truncation if "truncate" mode is enabled
        if "truncate" in _modes:
            df = df.with_columns(
                [
                    pl.col("longitude").round(5),
                    pl.col("latitude").round(5),
                ]
            )

        # Ensure ALL longitudes are in 0-360 range if "lon360" mode is set
        if "lon360" in _modes:
            df = df.with_columns(
                pl.when(pl.col("longitude") < 0)
                .then(pl.col("longitude") + 360)
                .otherwise(pl.col("longitude"))
                .alias("longitude")
            )

        # v0.5.3 H5: invariant — zonly mode must not emit `distance` column.
        if "zonly" in _modes:
            assert "distance" not in df.columns, (
                "polyhandler invariant violated: zonly mode produced distance column"
            )

        if stats_out is not None and _stats_root:
            _finalize_line_stats(stats_out, stats_out.pop("_line_parts", []))
        return df, line_id
    else:
        if stats_out is not None and _stats_root:
            _finalize_line_stats(stats_out, stats_out.pop("_line_parts", []))
        return pl.DataFrame([], schema=consistent_schema), line_id
