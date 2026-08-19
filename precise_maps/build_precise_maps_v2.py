from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import planetary_computer
import rasterio
from matplotlib.colors import LightSource
from pyproj import CRS, Transformer
from pystac_client import Client
from rasterio.features import geometry_mask
from rasterio.merge import merge
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_geom
from rasterio.windows import Window, from_bounds
from scipy import ndimage
from shapely.geometry import LineString, MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

CADASTRAL_NUMBER = "22:43:010001:1229"
SAFE_CAD = CADASTRAL_NUMBER.replace(":", "_")
INPUT_GEOJSON_CANDIDATES = [
    Path("precise_maps/input/parcel_all_components.geojson"),
    Path("precise_maps/parcel_all_components.geojson"),
]
OUTPUT_ROOT = Path("precise_maps_output") / SAFE_CAD
ZIP_PATH = Path(f"precise_maps_{SAFE_CAD}.zip")
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COMMON_RESOLUTION_M = 30.0
NODATA = -9999.0

# These are independent global elevation products available as public COG/STAC assets.
# FABDEM is probed as an optional collection and is only reported as included if an
# actual raster asset is returned for this parcel.
MODEL_SPECS: list[dict[str, Any]] = [
    {
        "id": "copernicus_glo30",
        "title": "Copernicus DEM GLO-30",
        "collections": ["cop-dem-glo-30"],
        "asset_keys": ["data"],
        "nominal_resolution": "1 arc-second / about 30 m",
        "surface": "DSM",
        "provider": "Copernicus / Airbus, public mirror in Microsoft Planetary Computer",
    },
    {
        "id": "nasadem",
        "title": "NASADEM HGT v001",
        "collections": ["nasadem"],
        "asset_keys": ["elevation", "data"],
        "nominal_resolution": "1 arc-second / about 30 m",
        "surface": "SRTM-derived DEM",
        "provider": "NASA / USGS, public mirror in Microsoft Planetary Computer",
    },
    {
        "id": "alos_aw3d30",
        "title": "ALOS World 3D-30m (AW3D30)",
        "collections": ["alos-dem"],
        "asset_keys": ["data", "elevation"],
        "nominal_resolution": "1 arc-second / about 30 m",
        "surface": "DSM",
        "provider": "JAXA, public mirror in Microsoft Planetary Computer",
    },
    {
        "id": "fabdem",
        "title": "FABDEM v1.2",
        "collections": ["fabdem", "fabdem-v1-2", "fabdem-v1.2"],
        "asset_keys": ["data", "elevation", "dem"],
        "nominal_resolution": "1 arc-second / about 30 m",
        "surface": "bare-earth model derived from Copernicus GLO-30",
        "provider": "University of Bristol / FABDEM public distribution when exposed by STAC",
        "optional": True,
    },
]


def clean_url(url: str) -> str:
    """Remove temporary SAS query parameters while retaining the durable asset path."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def find_input_geojson() -> Path:
    for candidate in INPUT_GEOJSON_CANDIDATES:
        if candidate.exists():
            return candidate
    found = list(Path("precise_maps").rglob("*.geojson"))
    if found:
        return found[0]
    raise FileNotFoundError("Cadastral GeoJSON was not restored by the workflow")


def flatten_polygons(geometry: Any) -> list[Polygon]:
    geom = shape(geometry) if isinstance(geometry, dict) else geometry
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    result: list[Polygon] = []
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            result.extend(flatten_polygons(part))
    return result


def load_parcel() -> tuple[MultiPolygon, list[Polygon], dict[str, Any]]:
    source_path = find_input_geojson()
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    geometries: list[Polygon] = []
    properties: dict[str, Any] = {}
    if payload.get("type") == "FeatureCollection":
        for feature in payload.get("features", []):
            if feature.get("geometry"):
                geometries.extend(flatten_polygons(feature["geometry"]))
            if not properties and isinstance(feature.get("properties"), dict):
                properties = dict(feature["properties"])
    elif payload.get("type") == "Feature":
        geometries.extend(flatten_polygons(payload["geometry"]))
        properties = dict(payload.get("properties") or {})
    else:
        geometries.extend(flatten_polygons(payload))
    geometries = [geom.buffer(0) for geom in geometries if not geom.is_empty]
    if not geometries:
        raise ValueError("No polygon geometry found in cadastral GeoJSON")
    union = unary_union(geometries)
    if isinstance(union, Polygon):
        union = MultiPolygon([union])
    centroid = union.centroid
    utm_epsg = 32600 + int((centroid.x + 180) // 6) + 1 if centroid.y >= 0 else 32700 + int((centroid.x + 180) // 6) + 1
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True).transform
    geometries.sort(key=lambda geom: shapely_transform(to_utm, geom).area, reverse=True)
    return MultiPolygon(geometries), geometries, {"source_path": str(source_path), "source_properties": properties, "utm_epsg": utm_epsg}


def save_geometry_files(parcel: MultiPolygon, components: list[Polygon], info: dict[str, Any]) -> None:
    geometry_dir = OUTPUT_ROOT / "geometry"
    geometry_dir.mkdir(parents=True, exist_ok=True)
    full_feature = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": mapping(parcel),
            "properties": {"cadastral_number": CADASTRAL_NUMBER, "component_count": len(components)},
        }],
    }
    write_json(geometry_dir / "parcel_all_components.geojson", full_feature)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{info['utm_epsg']}", always_xy=True).transform
    component_rows = []
    for index, component in enumerate(components, start=1):
        area_ha = shapely_transform(to_utm, component).area / 10_000
        feature = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": mapping(component),
                "properties": {
                    "cadastral_number": CADASTRAL_NUMBER,
                    "component": index,
                    "area_ha_from_geometry": round(area_ha, 6),
                },
            }],
        }
        write_json(geometry_dir / f"component_{index:02d}.geojson", feature)
        component_rows.append({"component": index, "area_ha_from_geometry": area_ha, "centroid_lon": component.centroid.x, "centroid_lat": component.centroid.y})
    with (geometry_dir / "components.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(component_rows[0].keys()))
        writer.writeheader()
        writer.writerows(component_rows)


def open_catalog() -> Client:
    return Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)


def discover_model_assets(catalog: Client, spec: dict[str, Any], parcel: MultiPolygon) -> dict[str, Any]:
    errors: list[str] = []
    for collection in spec["collections"]:
        try:
            items = list(catalog.search(collections=[collection], intersects=mapping(parcel), max_items=100).items())
        except Exception as exc:
            errors.append(f"{collection}: {type(exc).__name__}: {exc}")
            continue
        if not items:
            errors.append(f"{collection}: no intersecting STAC items")
            continue
        assets: list[dict[str, Any]] = []
        for item in items:
            chosen_key = next((key for key in spec["asset_keys"] if key in item.assets), None)
            if chosen_key is None:
                errors.append(f"{collection}/{item.id}: expected asset keys absent; got {sorted(item.assets)}")
                continue
            asset = item.assets[chosen_key]
            assets.append({
                "item_id": item.id,
                "collection": collection,
                "asset_key": chosen_key,
                "href": asset.href,
                "durable_href": clean_url(asset.href),
                "bbox": item.bbox,
                "datetime": item.datetime.isoformat() if item.datetime else None,
                "properties": {key: value for key, value in item.properties.items() if key in {"gsd", "proj:epsg", "created", "updated", "platform", "instruments"}},
            })
        if assets:
            return {"available": True, "collection": collection, "assets": assets, "errors": errors}
    return {"available": False, "collection": None, "assets": [], "errors": errors}


def source_environment() -> rasterio.Env:
    return rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.TIF,.TIFF,.hgt",
        GDAL_HTTP_MULTIPLEX="YES",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        GDAL_CACHEMAX=512,
    )


def transformed_geometry(geom: Any, dst_crs: CRS) -> dict[str, Any]:
    return transform_geom("EPSG:4326", dst_crs.to_string(), mapping(geom), precision=12)


def transformed_shapely(geom: Any, dst_crs: CRS) -> Any:
    transformer = Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    return shapely_transform(transformer.transform, geom)


def normalise_array(array: np.ndarray, nodata: float | int | None) -> np.ndarray:
    result = array.astype("float32", copy=False)
    invalid = ~np.isfinite(result)
    if nodata is not None and np.isfinite(nodata):
        invalid |= np.isclose(result, float(nodata))
    invalid |= (result < -1000) | (result > 10000)
    result[invalid] = np.nan
    return result


def mosaic_native(assets: list[dict[str, Any]], parcel: MultiPolygon, model_dir: Path) -> tuple[Path, dict[str, Any]]:
    model_dir.mkdir(parents=True, exist_ok=True)
    temp_mosaic = model_dir / "_temporary_mosaic.tif"
    output_path = model_dir / "parcel_all_components_native.tif"
    with source_environment(), ExitStack() as stack:
        datasets = []
        open_errors = []
        for asset in assets:
            try:
                datasets.append(stack.enter_context(rasterio.open(asset["href"])))
            except Exception as exc:
                open_errors.append(f"{asset['item_id']}: {type(exc).__name__}: {exc}")
        if not datasets:
            raise RuntimeError("No STAC raster assets could be opened: " + " | ".join(open_errors))
        source_crs = datasets[0].crs
        compatible = [dataset for dataset in datasets if dataset.crs == source_crs and dataset.count >= 1]
        if not compatible:
            raise RuntimeError("No compatible single-band elevation datasets")
        parcel_source = transformed_shapely(parcel, source_crs)
        bounds = parcel_source.bounds
        pixel_x = abs(compatible[0].transform.a)
        pixel_y = abs(compatible[0].transform.e)
        pad_x = pixel_x * 2
        pad_y = pixel_y * 2
        bounds = (bounds[0] - pad_x, bounds[1] - pad_y, bounds[2] + pad_x, bounds[3] + pad_y)
        mosaic, transform = merge(compatible, bounds=bounds, nodata=NODATA, dtype="float32", resampling=Resampling.bilinear)
        data = normalise_array(mosaic[0], NODATA)
        inside = geometry_mask([transformed_geometry(parcel, source_crs)], out_shape=data.shape, transform=transform, invert=True, all_touched=True)
        data[~inside] = np.nan
        profile = compatible[0].profile.copy()
        profile.update(driver="GTiff", dtype="float32", count=1, height=data.shape[0], width=data.shape[1], transform=transform, nodata=NODATA, compress="DEFLATE", predictor=3, tiled=True, BIGTIFF="IF_SAFER")
        with rasterio.open(output_path, "w", **profile) as destination:
            destination.write(np.where(np.isfinite(data), data, NODATA).astype("float32"), 1)
            destination.update_tags(
                cadastral_number=CADASTRAL_NUMBER,
                processing="native-resolution mosaic clipped to public cadastral geometry; no reprojection",
                source_crs=str(source_crs),
            )
        metadata = {
            "native_crs": source_crs.to_string(),
            "native_pixel_size_x": pixel_x,
            "native_pixel_size_y": pixel_y,
            "native_width": data.shape[1],
            "native_height": data.shape[0],
            "valid_native_pixels": int(np.isfinite(data).sum()),
            "opened_asset_count": len(compatible),
            "asset_open_errors": open_errors,
        }
    return output_path, metadata


def clip_native_components(native_path: Path, components: list[Polygon], output_dir: Path) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    with rasterio.open(native_path) as source:
        data = normalise_array(source.read(1), source.nodata)
        for index, component in enumerate(components, start=1):
            component_source = transformed_shapely(component, source.crs)
            window = from_bounds(*component_source.bounds, transform=source.transform).round_offsets().round_lengths()
            window = window.intersection(Window(0, 0, source.width, source.height))
            if window.width <= 0 or window.height <= 0:
                rows.append({"component": index, "error": "component outside native raster"})
                continue
            row0, col0 = int(window.row_off), int(window.col_off)
            height, width = int(window.height), int(window.width)
            subset = data[row0:row0 + height, col0:col0 + width].copy()
            subset_transform = source.window_transform(window)
            mask = geometry_mask([transformed_geometry(component, source.crs)], out_shape=subset.shape, transform=subset_transform, invert=True, all_touched=True)
            subset[~mask] = np.nan
            path = output_dir / f"component_{index:02d}_native.tif"
            profile = source.profile.copy()
            profile.update(height=height, width=width, transform=subset_transform, dtype="float32", nodata=NODATA, compress="DEFLATE", predictor=3, tiled=False)
            with rasterio.open(path, "w", **profile) as destination:
                destination.write(np.where(np.isfinite(subset), subset, NODATA).astype("float32"), 1)
                destination.update_tags(component=index, cadastral_number=CADASTRAL_NUMBER, processing="native grid clip; no reprojection")
            rows.append({"component": index, "path": str(path.relative_to(OUTPUT_ROOT)), "valid_native_pixels": int(np.isfinite(subset).sum())})
    return rows


def common_grid(parcel: MultiPolygon, utm_epsg: int) -> tuple[CRS, Any, int, int, Any]:
    crs = CRS.from_epsg(utm_epsg)
    parcel_utm = transformed_shapely(parcel, crs)
    xmin, ymin, xmax, ymax = parcel_utm.bounds
    xmin = math.floor((xmin - COMMON_RESOLUTION_M) / COMMON_RESOLUTION_M) * COMMON_RESOLUTION_M
    ymin = math.floor((ymin - COMMON_RESOLUTION_M) / COMMON_RESOLUTION_M) * COMMON_RESOLUTION_M
    xmax = math.ceil((xmax + COMMON_RESOLUTION_M) / COMMON_RESOLUTION_M) * COMMON_RESOLUTION_M
    ymax = math.ceil((ymax + COMMON_RESOLUTION_M) / COMMON_RESOLUTION_M) * COMMON_RESOLUTION_M
    width = int(round((xmax - xmin) / COMMON_RESOLUTION_M))
    height = int(round((ymax - ymin) / COMMON_RESOLUTION_M))
    transform = from_origin(xmin, ymax, COMMON_RESOLUTION_M, COMMON_RESOLUTION_M)
    return crs, transform, width, height, parcel_utm


def reproject_common(native_path: Path, target_path: Path, parcel: MultiPolygon, dst_crs: CRS, dst_transform: Any, width: int, height: int) -> dict[str, Any]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    destination = np.full((height, width), np.nan, dtype="float32")
    with rasterio.open(native_path) as source:
        source_data = normalise_array(source.read(1), source.nodata)
        reproject(
            source=source_data,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
            num_threads=2,
        )
    parcel_mask = geometry_mask([transformed_geometry(parcel, dst_crs)], out_shape=destination.shape, transform=dst_transform, invert=True, all_touched=True)
    destination[~parcel_mask] = np.nan
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": dst_crs,
        "transform": dst_transform,
        "nodata": NODATA,
        "compress": "DEFLATE",
        "predictor": 3,
        "tiled": True,
        "BIGTIFF": "IF_SAFER",
    }
    with rasterio.open(target_path, "w", **profile) as destination_file:
        destination_file.write(np.where(np.isfinite(destination), destination, NODATA).astype("float32"), 1)
        destination_file.update_tags(cadastral_number=CADASTRAL_NUMBER, processing=f"bilinear reprojection to EPSG:{dst_crs.to_epsg()} at {COMMON_RESOLUTION_M:g} m for model comparison")
    return {"valid_common_pixels": int(np.isfinite(destination).sum()), "common_crs": dst_crs.to_string(), "common_resolution_m": COMMON_RESOLUTION_M}


def fill_nearest(array: np.ndarray) -> np.ndarray:
    valid = np.isfinite(array)
    if not valid.any():
        return np.zeros_like(array, dtype="float32")
    if valid.all():
        return array.astype("float32")
    indices = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
    return array[tuple(indices)].astype("float32")


def terrain_derivatives(elevation: np.ndarray, resolution: float) -> dict[str, np.ndarray]:
    filled = fill_nearest(elevation)
    grad_y, grad_x = np.gradient(filled, resolution, resolution)
    slope_radians = np.arctan(np.hypot(grad_x, grad_y))
    slope_degrees = np.degrees(slope_radians).astype("float32")
    slope_percent = (np.tan(slope_radians) * 100).astype("float32")
    aspect = ((np.degrees(np.arctan2(-grad_x, grad_y)) + 360) % 360).astype("float32")
    azimuth = math.radians(315)
    altitude = math.radians(45)
    hillshade = 255 * (np.cos(altitude) * np.cos(slope_radians) + np.sin(altitude) * np.sin(slope_radians) * np.cos(azimuth - np.radians(aspect)))
    hillshade = np.clip(hillshade, 0, 255).astype("float32")
    local_mean = ndimage.uniform_filter(filled, size=3, mode="nearest")
    ruggedness = np.sqrt(ndimage.uniform_filter((filled - local_mean) ** 2, size=3, mode="nearest")).astype("float32")
    invalid = ~np.isfinite(elevation)
    for output in (slope_degrees, slope_percent, aspect, hillshade, ruggedness):
        output[invalid] = np.nan
    return {
        "slope_degrees": slope_degrees,
        "slope_percent": slope_percent,
        "aspect_degrees": aspect,
        "hillshade": hillshade,
        "local_ruggedness_m": ruggedness,
    }


def raster_subset(path: Path, geom_utm: Polygon | MultiPolygon) -> tuple[np.ndarray, Any, CRS]:
    with rasterio.open(path) as source:
        window = from_bounds(*geom_utm.bounds, transform=source.transform).round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, source.width, source.height))
        data = normalise_array(source.read(1, window=window), source.nodata)
        transform = source.window_transform(window)
        mask = geometry_mask([mapping(geom_utm)], out_shape=data.shape, transform=transform, invert=True, all_touched=True)
        data[~mask] = np.nan
        return data, transform, source.crs


def write_raster(path: Path, array: np.ndarray, transform: Any, crs: CRS, tags: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": array.shape[0],
        "width": array.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "nodata": NODATA,
        "compress": "DEFLATE",
        "predictor": 3,
        "tiled": False,
    }
    with rasterio.open(path, "w", **profile) as destination:
        destination.write(np.where(np.isfinite(array), array, NODATA).astype("float32"), 1)
        if tags:
            destination.update_tags(**{key: str(value) for key, value in tags.items()})


def plot_raster(path: Path, array: np.ndarray, transform: Any, boundary: Any, title: str, cmap: str, label: str, vmin: float | None = None, vmax: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    xmin = transform.c
    ymax = transform.f
    xmax = xmin + transform.a * array.shape[1]
    ymin = ymax + transform.e * array.shape[0]
    aspect_ratio = max(0.65, min(1.8, (xmax - xmin) / max(ymax - ymin, 1)))
    fig_width = 10 if aspect_ratio >= 1 else 8
    fig_height = fig_width / aspect_ratio
    fig, axis = plt.subplots(figsize=(fig_width, fig_height), constrained_layout=True)
    image = axis.imshow(array, extent=[xmin, xmax, ymin, ymax], origin="upper", cmap=cmap, vmin=vmin, vmax=vmax)
    geoms = list(boundary.geoms) if isinstance(boundary, MultiPolygon) else [boundary]
    for geom in geoms:
        x, y = geom.exterior.xy
        axis.plot(x, y, linewidth=1.4, color="black")
        axis.plot(x, y, linewidth=0.7, color="white")
    axis.set_title(title)
    axis.set_aspect("equal")
    axis.set_xlabel("Easting, m")
    axis.set_ylabel("Northing, m")
    colorbar = fig.colorbar(image, ax=axis, shrink=0.85)
    colorbar.set_label(label)
    axis.text(0.01, 0.01, f"{CADASTRAL_NUMBER} | grid {COMMON_RESOLUTION_M:g} m", transform=axis.transAxes, fontsize=8, bbox={"facecolor": "white", "alpha": 0.75, "edgecolor": "none"})
    fig.savefig(path, dpi=220)
    plt.close(fig)


def numeric_stats(array: np.ndarray) -> dict[str, Any]:
    values = array[np.isfinite(array)]
    if values.size == 0:
        return {"valid_cells": 0}
    return {
        "valid_cells": int(values.size),
        "min": float(np.min(values)),
        "p10": float(np.percentile(values, 10)),
        "median": float(np.median(values)),
        "mean": float(np.mean(values)),
        "p90": float(np.percentile(values, 90)),
        "max": float(np.max(values)),
        "std": float(np.std(values)),
    }


def build_model_products(spec: dict[str, Any], discovery: dict[str, Any], parcel: MultiPolygon, components: list[Polygon], utm_epsg: int, dst_transform: Any, width: int, height: int) -> dict[str, Any]:
    model_dir = OUTPUT_ROOT / "models" / spec["id"]
    metadata: dict[str, Any] = {**{key: value for key, value in spec.items() if key != "collections"}, "discovery": discovery}
    if not discovery["available"]:
        metadata["status"] = "unavailable"
        write_json(model_dir / "metadata.json", metadata)
        return metadata
    try:
        native_path, native_meta = mosaic_native(discovery["assets"], parcel, model_dir / "native")
        component_native = clip_native_components(native_path, components, model_dir / "native" / "components")
        dst_crs, _, _, _, parcel_utm = common_grid(parcel, utm_epsg)
        common_path = model_dir / "common_grid" / "parcel_all_components_utm30m.tif"
        common_meta = reproject_common(native_path, common_path, parcel, dst_crs, dst_transform, width, height)
        metadata.update({"status": "included", "native": native_meta, "common_grid": common_meta, "component_native": component_native, "native_path": str(native_path.relative_to(OUTPUT_ROOT)), "common_path": str(common_path.relative_to(OUTPUT_ROOT))})
        stats_rows = []
        targets: list[tuple[str, Polygon | MultiPolygon]] = [("parcel_all_components", parcel_utm)]
        transformer = Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True).transform
        targets.extend((f"component_{index:02d}", shapely_transform(transformer, component)) for index, component in enumerate(components, start=1))
        for target_name, target_geometry in targets:
            elevation, transform, crs = raster_subset(common_path, target_geometry)
            derivatives = terrain_derivatives(elevation, COMMON_RESOLUTION_M)
            target_dir = model_dir / "derived" / target_name
            write_raster(target_dir / "elevation_utm30m.tif", elevation, transform, crs, {"product": spec["title"], "variable": "elevation_m"})
            for variable, array in derivatives.items():
                write_raster(target_dir / f"{variable}.tif", array, transform, crs, {"product": spec["title"], "variable": variable})
            plot_raster(target_dir / "elevation.png", elevation, transform, target_geometry, f"{spec['title']} — elevation — {target_name}", "terrain", "Elevation, m")
            plot_raster(target_dir / "hillshade.png", derivatives["hillshade"], transform, target_geometry, f"{spec['title']} — hillshade — {target_name}", "gray", "Illumination", 0, 255)
            plot_raster(target_dir / "slope_degrees.png", derivatives["slope_degrees"], transform, target_geometry, f"{spec['title']} — slope — {target_name}", "magma", "Slope, degrees", 0, min(45, float(np.nanpercentile(derivatives['slope_degrees'], 99)) if np.isfinite(derivatives['slope_degrees']).any() else 45))
            plot_raster(target_dir / "aspect_degrees.png", derivatives["aspect_degrees"], transform, target_geometry, f"{spec['title']} — aspect — {target_name}", "twilight", "Aspect, degrees", 0, 360)
            plot_raster(target_dir / "ruggedness.png", derivatives["local_ruggedness_m"], transform, target_geometry, f"{spec['title']} — local ruggedness — {target_name}", "viridis", "3×3 local ruggedness, m")
            row = {"model": spec["id"], "target": target_name}
            row.update({f"elevation_{key}": value for key, value in numeric_stats(elevation).items()})
            row.update({f"slope_deg_{key}": value for key, value in numeric_stats(derivatives["slope_degrees"]).items()})
            row.update({f"slope_pct_{key}": value for key, value in numeric_stats(derivatives["slope_percent"]).items()})
            row.update({f"ruggedness_{key}": value for key, value in numeric_stats(derivatives["local_ruggedness_m"]).items()})
            stats_rows.append(row)
            write_json(target_dir / "statistics.json", row)
        metadata["statistics"] = stats_rows
        with (model_dir / "statistics.csv").open("w", newline="", encoding="utf-8") as handle:
            fieldnames = sorted({key for row in stats_rows for key in row})
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(stats_rows)
    except Exception as exc:
        metadata["status"] = "failed"
        metadata["processing_error"] = f"{type(exc).__name__}: {exc}"
    write_json(model_dir / "metadata.json", metadata)
    return metadata


def read_common(path: Path) -> tuple[np.ndarray, Any, CRS]:
    with rasterio.open(path) as source:
        return normalise_array(source.read(1), source.nodata), source.transform, source.crs


def build_comparison(model_results: list[dict[str, Any]], parcel: MultiPolygon, utm_epsg: int) -> dict[str, Any]:
    included = [result for result in model_results if result.get("status") == "included"]
    comparison_dir = OUTPUT_ROOT / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)
    if len(included) < 2:
        result = {"status": "not_built", "reason": "fewer than two DEMs were successfully included"}
        write_json(comparison_dir / "comparison_metadata.json", result)
        return result
    arrays = []
    ids = []
    transform = None
    crs = None
    for result in included:
        array, array_transform, array_crs = read_common(OUTPUT_ROOT / result["common_path"])
        arrays.append(array)
        ids.append(result["id"])
        transform = array_transform
        crs = array_crs
    stack = np.stack(arrays)
    valid_count = np.isfinite(stack).sum(axis=0)
    consensus = np.nanmedian(stack, axis=0).astype("float32")
    consensus[valid_count < 2] = np.nan
    model_range = (np.nanmax(stack, axis=0) - np.nanmin(stack, axis=0)).astype("float32")
    model_range[valid_count < 2] = np.nan
    parcel_utm = transformed_shapely(parcel, CRS.from_epsg(utm_epsg))
    write_raster(comparison_dir / "consensus_median_elevation.tif", consensus, transform, crs, {"models": ",".join(ids), "variable": "median_elevation_m"})
    write_raster(comparison_dir / "model_range_max_minus_min.tif", model_range, transform, crs, {"models": ",".join(ids), "variable": "elevation_range_m"})
    plot_raster(comparison_dir / "consensus_median_elevation.png", consensus, transform, parcel_utm, "Median elevation across independent 30 m DEMs", "terrain", "Elevation, m")
    vmax = float(np.nanpercentile(model_range, 99)) if np.isfinite(model_range).any() else 10
    plot_raster(comparison_dir / "model_range_max_minus_min.png", model_range, transform, parcel_utm, "DEM disagreement: maximum minus minimum", "inferno", "Difference, m", 0, vmax)
    rows = []
    for model_id, array in zip(ids, arrays):
        difference = array - consensus
        difference[~np.isfinite(consensus)] = np.nan
        write_raster(comparison_dir / f"{model_id}_minus_consensus.tif", difference, transform, crs, {"model": model_id, "variable": "elevation_difference_m"})
        limit = float(np.nanpercentile(np.abs(difference), 99)) if np.isfinite(difference).any() else 10
        plot_raster(comparison_dir / f"{model_id}_minus_consensus.png", difference, transform, parcel_utm, f"{model_id} minus cross-model median", "coolwarm", "Difference, m", -limit, limit)
        row = {"model": model_id, **numeric_stats(difference)}
        rows.append(row)
    # Pairwise mean absolute and RMSE differences on mutually valid pixels.
    pairwise = []
    for first_index in range(len(arrays)):
        for second_index in range(first_index + 1, len(arrays)):
            first, second = arrays[first_index], arrays[second_index]
            valid = np.isfinite(first) & np.isfinite(second)
            if not valid.any():
                continue
            delta = first[valid] - second[valid]
            pairwise.append({
                "model_a": ids[first_index],
                "model_b": ids[second_index],
                "common_cells": int(valid.sum()),
                "mean_difference_m": float(delta.mean()),
                "mean_absolute_difference_m": float(np.abs(delta).mean()),
                "rmse_m": float(np.sqrt(np.mean(delta ** 2))),
                "p90_absolute_difference_m": float(np.percentile(np.abs(delta), 90)),
            })
    result = {
        "status": "built",
        "models": ids,
        "consensus_stats": numeric_stats(consensus),
        "range_stats": numeric_stats(model_range),
        "model_minus_consensus": rows,
        "pairwise": pairwise,
    }
    write_json(comparison_dir / "comparison_metadata.json", result)
    with (comparison_dir / "pairwise_comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(pairwise[0].keys()) if pairwise else ["model_a", "model_b"])
        writer.writeheader()
        writer.writerows(pairwise)
    return result


def build_contours(consensus_path: Path, parcel: MultiPolygon, utm_epsg: int) -> dict[str, Any]:
    if not consensus_path.exists():
        return {"status": "not_built"}
    array, transform, crs = read_common(consensus_path)
    if not np.isfinite(array).any():
        return {"status": "empty"}
    minimum = float(np.nanmin(array))
    maximum = float(np.nanmax(array))
    interval = 5 if maximum - minimum <= 250 else 10
    levels = np.arange(math.floor(minimum / interval) * interval, math.ceil(maximum / interval) * interval + interval, interval)
    x = transform.c + (np.arange(array.shape[1]) + 0.5) * transform.a
    y = transform.f + (np.arange(array.shape[0]) + 0.5) * transform.e
    filled = fill_nearest(array)
    fig, axis = plt.subplots()
    contour_set = axis.contour(x, y, filled, levels=levels)
    plt.close(fig)
    features = []
    parcel_utm = transformed_shapely(parcel, CRS.from_epsg(utm_epsg))
    # Matplotlib versions expose contour segments consistently through allsegs.
    for level, segments in zip(contour_set.levels, contour_set.allsegs):
        for segment in segments:
            if len(segment) < 2:
                continue
            line = LineString(segment)
            clipped = line.intersection(parcel_utm)
            if clipped.is_empty:
                continue
            geoms: Iterable[Any] = clipped.geoms if hasattr(clipped, "geoms") else [clipped]
            for geom in geoms:
                if isinstance(geom, LineString) and geom.length > COMMON_RESOLUTION_M:
                    features.append({"type": "Feature", "geometry": mapping(geom), "properties": {"elevation_m": float(level), "interval_m": interval}})
    payload = {"type": "FeatureCollection", "name": "consensus_contours", "crs": {"type": "name", "properties": {"name": crs.to_string()}}, "features": features}
    write_json(OUTPUT_ROOT / "comparison" / f"consensus_contours_{interval}m.geojson", payload)
    return {"status": "built", "interval_m": interval, "feature_count": len(features)}


def create_readme(model_results: list[dict[str, Any]], comparison: dict[str, Any], contours: dict[str, Any], component_count: int) -> None:
    included = [result for result in model_results if result.get("status") == "included"]
    failed = [result for result in model_results if result.get("status") != "included"]
    lines = [
        f"# Precise elevation supplement — {CADASTRAL_NUMBER}",
        "",
        f"This package contains {len(included)} independent elevation models for the full parcel and all {component_count} separate cadastral components.",
        "",
        "## Included models",
        "",
    ]
    for result in included:
        lines.append(f"- **{result['title']}** — {result['nominal_resolution']}; surface type: {result['surface']}. Native clipped GeoTIFFs are preserved without reprojection, plus a 30 m UTM comparison grid.")
    if failed:
        lines.extend(["", "## Sources attempted but not included", ""])
        for result in failed:
            reason = result.get("processing_error") or "; ".join(result.get("discovery", {}).get("errors", [])) or result.get("status")
            lines.append(f"- **{result['title']}** — {reason}")
    lines.extend([
        "",
        "## Folder structure",
        "",
        "- `geometry/`: original public cadastral geometry and 10 component GeoJSON files.",
        "- `models/<model>/native/`: source-grid clips; no reprojection and no artificial increase in resolution.",
        "- `models/<model>/common_grid/`: EPSG UTM 30 m grid used strictly for cross-model comparison.",
        "- `models/<model>/derived/<component>/`: elevation, slope in degrees and percent, aspect, hillshade and local ruggedness as GeoTIFF and PNG.",
        "- `comparison/`: cross-model median, disagreement range, per-model difference rasters and pairwise statistics.",
        "- `manifest.csv`: file sizes, SHA-256 checksums and paths.",
        "",
        "## Accuracy limits",
        "",
        "These are global approximately 30 m products. A 30×30 m cell covers 900 m². Small gullies, terraces, road cuts and drainage channels narrower than a cell may be absent. Copernicus GLO-30 and ALOS AW3D30 are DSM products and may retain vegetation/building effects. NASADEM is SRTM-derived. The cross-model disagreement map is therefore as important as any single attractive relief image.",
        "",
        "Public cadastral geometry is suitable for research overlays but does not replace official EGRN coordinate records or field surveying. None of the rasters is a substitute for engineering geodesy, drone photogrammetry or lidar where 1–5 m terrain detail is required.",
        "",
        f"Comparison status: `{comparison.get('status')}`. Contours: `{contours.get('status')}`.",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
    ])
    (OUTPUT_ROOT / "README.md").write_text("\n".join(lines), encoding="utf-8")


def create_manifest() -> None:
    rows = []
    for path in sorted(OUTPUT_ROOT.rglob("*")):
        if path.is_file():
            rows.append({"path": str(path.relative_to(OUTPUT_ROOT)), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    with (OUTPUT_ROOT / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["path", "size_bytes", "sha256"])
        writer.writeheader()
        writer.writerows(rows)
    write_json(OUTPUT_ROOT / "manifest.json", rows)


def package_output() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(OUTPUT_ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=str(Path(SAFE_CAD) / path.relative_to(OUTPUT_ROOT)))


def main() -> None:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    parcel, components, parcel_info = load_parcel()
    save_geometry_files(parcel, components, parcel_info)
    dst_crs, dst_transform, width, height, _ = common_grid(parcel, parcel_info["utm_epsg"])
    catalog = open_catalog()
    discoveries = {}
    model_results = []
    for spec in MODEL_SPECS:
        print(f"Discovering {spec['title']}...", flush=True)
        discovery = discover_model_assets(catalog, spec, parcel)
        discoveries[spec["id"]] = discovery
        print(f"  available={discovery['available']} assets={len(discovery['assets'])}", flush=True)
        result = build_model_products(spec, discovery, parcel, components, parcel_info["utm_epsg"], dst_transform, width, height)
        model_results.append(result)
        print(f"  processing status={result.get('status')}", flush=True)
    comparison = build_comparison(model_results, parcel, parcel_info["utm_epsg"])
    consensus_path = OUTPUT_ROOT / "comparison" / "consensus_median_elevation.tif"
    contours = build_contours(consensus_path, parcel, parcel_info["utm_epsg"])
    build_report = {
        "cadastral_number": CADASTRAL_NUMBER,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "component_count": len(components),
        "parcel_info": parcel_info,
        "common_grid": {"crs": dst_crs.to_string(), "resolution_m": COMMON_RESOLUTION_M, "width": width, "height": height},
        "models": model_results,
        "comparison": comparison,
        "contours": contours,
    }
    write_json(OUTPUT_ROOT / "build_report.json", build_report)
    create_readme(model_results, comparison, contours, len(components))
    create_manifest()
    package_output()
    print(json.dumps({"output": str(OUTPUT_ROOT), "zip": str(ZIP_PATH), "models_included": [result["id"] for result in model_results if result.get("status") == "included"], "zip_size": ZIP_PATH.stat().st_size}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
