from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import cv2
import numpy as np
import requests
from pyproj import Transformer

CADASTRAL_NUMBERS = [
    "22:43:010001:1229", "22:43:010001:1233", "22:43:010001:1246",
    "22:43:010001:1272", "22:43:040001:1313", "22:43:040001:1564",
    "22:43:040001:1571", "22:43:040001:1575", "22:43:010001:1227",
    "22:43:010001:1235", "22:43:010001:1236", "22:43:040001:1300",
    "22:43:040001:1301", "22:43:040001:1302", "22:43:040002:513",
]

HOSTS = [
    "https://pkk5.rosreestr.ru",
    "http://pkk5.rosreestr.ru",
    "https://pkk.rosreestr.ru",
    "http://pkk.rosreestr.ru",
]

SERVICES = [
    "/arcgis/rest/services/Cadastre/CadastreSelected/MapServer/export",
    "/arcgis/rest/services/PKK6/CadastreObjects/MapServer/export",
    "/arcgis/rest/services/Cadastre/Cadastre/MapServer/export",
]

OUT = Path("cadastre_result")
META_DIR = OUT / "legacy_metadata"
IMG_DIR = OUT / "legacy_outlines"
GEO_DIR = OUT / "geojson"
for folder in (OUT, META_DIR, IMG_DIR, GEO_DIR):
    folder.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "application/json,image/png,image/*;q=0.8,*/*;q=0.5",
    "Referer": "https://pkk5.rosreestr.ru/",
}

TO_WGS84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def clear_code(code: str) -> str:
    return ":".join(str(int(part)) for part in code.split(":"))


def get_json(url: str, params: dict[str, Any] | None = None) -> tuple[Any | None, str | None]:
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=(8, 25), allow_redirects=True)
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}: {response.text[:180]}"
        return response.json(), None
    except Exception as exc:
        return None, repr(exc)


def extract_feature(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    feature = payload.get("feature")
    if isinstance(feature, dict):
        return feature
    features = payload.get("features")
    if isinstance(features, list) and features:
        return features[0] if isinstance(features[0], dict) else None
    return None


def lookup_metadata(cad: str) -> dict[str, Any]:
    variants = list(dict.fromkeys([cad, clear_code(cad)]))
    errors: list[str] = []
    for host in HOSTS:
        for variant in variants:
            endpoints = [
                (f"{host}/api/features/1/{quote(variant, safe=':')}", None),
                (f"{host}/api/features/1", {"text": variant, "limit": 20, "tolerance": 4}),
            ]
            for url, params in endpoints:
                payload, error = get_json(url, params)
                if error:
                    errors.append(f"{url}: {error}")
                    continue
                feature = extract_feature(payload)
                if not feature:
                    errors.append(f"{url}: no feature in {str(payload)[:150]}")
                    continue
                attrs = feature.get("attrs") if isinstance(feature.get("attrs"), dict) else {}
                extent = feature.get("extent") if isinstance(feature.get("extent"), dict) else None
                center = feature.get("center") if isinstance(feature.get("center"), dict) else None
                feature_id = str(attrs.get("id") or variant)
                return {
                    "cadastral_number": cad,
                    "host": host,
                    "lookup_url": url,
                    "lookup_params": params,
                    "feature_id": feature_id,
                    "attrs": attrs,
                    "extent": extent,
                    "center": center,
                    "raw": payload,
                    "error": None,
                }
    return {
        "cadastral_number": cad,
        "host": None,
        "feature_id": None,
        "attrs": {},
        "extent": None,
        "center": None,
        "raw": None,
        "error": " | ".join(errors),
    }


def buffered_extent(extent: dict[str, Any]) -> list[float]:
    xmin, ymin, xmax, ymax = map(float, (extent["xmin"], extent["ymin"], extent["xmax"], extent["ymax"]))
    dx = max(xmax - xmin, 1.0)
    dy = max(ymax - ymin, 1.0)
    pad = max(dx, dy) * 0.04
    return [xmin - pad, ymin - pad, xmax + pad, ymax + pad]


def image_size(bbox: list[float]) -> tuple[int, int]:
    dx = max(bbox[2] - bbox[0], 1.0)
    dy = max(bbox[3] - bbox[1], 1.0)
    long_side = 1800
    if dx >= dy:
        width = long_side
        height = max(700, round(long_side * dy / dx))
    else:
        height = long_side
        width = max(700, round(long_side * dx / dy))
    return min(width, 2048), min(height, 2048)


def render_outline(metadata: dict[str, Any]) -> dict[str, Any]:
    cad = metadata["cadastral_number"]
    extent = metadata.get("extent")
    if not extent:
        return {**metadata, "render_error": "metadata has no extent", "components": 0}
    bbox = buffered_extent(extent)
    width, height = image_size(bbox)
    feature_id = clear_code(str(metadata.get("feature_id") or cad))
    layers = list(range(0, 30))
    layer_defs = {str(layer): f"ID = '{feature_id}'" for layer in layers}
    params = {
        "dpi": 96,
        "transparent": "true",
        "format": "png32",
        "layers": "show:" + ",".join(map(str, layers)),
        "bbox": ",".join(f"{value:.8f}" for value in bbox),
        "bboxSR": 102100,
        "imageSR": 102100,
        "size": f"{width},{height}",
        "layerDefs": json.dumps(layer_defs, ensure_ascii=False, separators=(",", ":")),
        "f": "image",
    }
    errors: list[str] = []
    host_order = list(dict.fromkeys([metadata.get("host"), *HOSTS]))
    safe = cad.replace(":", "_")
    for host in filter(None, host_order):
        for service in SERVICES:
            url = f"{host}{service}"
            try:
                response = requests.get(url, params=params, headers=HEADERS, timeout=(10, 45), allow_redirects=True)
                content_type = response.headers.get("content-type", "")
                if response.status_code != 200 or "image" not in content_type.lower() or len(response.content) < 200:
                    errors.append(f"{url}: HTTP {response.status_code} {content_type} {response.text[:120]}")
                    continue
                image_path = IMG_DIR / f"{safe}.png"
                image_path.write_bytes(response.content)
                geometry = trace_geometry(response.content, bbox)
                if geometry["components"] == 0:
                    errors.append(f"{url}: image downloaded but no significant contours")
                    continue
                geojson = {
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "geometry": geometry["geometry"],
                        "properties": {
                            "cadastral_number": cad,
                            "source": url,
                            "feature_id": feature_id,
                            "reconstructed_from_selected_raster": True,
                        },
                    }],
                }
                (GEO_DIR / f"{safe}.geojson").write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")
                return {
                    **metadata,
                    "render_url": response.url,
                    "render_service": url,
                    "outline_image": str(image_path),
                    "bbox_3857": bbox,
                    "image_width": width,
                    "image_height": height,
                    "components": geometry["components"],
                    "vertices": geometry["vertices"],
                    "geojson": str(GEO_DIR / f"{safe}.geojson"),
                    "render_error": None,
                }
            except Exception as exc:
                errors.append(f"{url}: {exc!r}")
    return {**metadata, "bbox_3857": bbox, "components": 0, "render_error": " | ".join(errors)}


def trace_geometry(image_bytes: bytes, bbox: list[float]) -> dict[str, Any]:
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_UNCHANGED)
    if image is None:
        return {"components": 0, "vertices": 0, "geometry": None}
    if image.ndim == 3 and image.shape[2] == 4:
        mask = image[:, :, 3]
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        mask = 255 - gray
    _, binary = cv2.threshold(mask, 12, 255, cv2.THRESH_BINARY)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = binary.shape
    polygons: list[list[list[list[float]]]] = []
    vertex_count = 0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < max(40.0, w * h * 0.00001):
            continue
        epsilon = max(1.0, 0.0015 * cv2.arcLength(contour, True))
        approx = cv2.approxPolyDP(contour, epsilon, True)
        ring: list[list[float]] = []
        for point in approx[:, 0, :]:
            px, py = float(point[0]), float(point[1])
            x = bbox[0] + px / max(w - 1, 1) * (bbox[2] - bbox[0])
            y = bbox[3] - py / max(h - 1, 1) * (bbox[3] - bbox[1])
            lon, lat = TO_WGS84.transform(x, y)
            ring.append([round(lon, 8), round(lat, 8)])
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            polygons.append([ring])
            vertex_count += len(ring)
    polygons.sort(key=lambda polygon: -abs(signed_area(polygon[0])))
    if not polygons:
        return {"components": 0, "vertices": 0, "geometry": None}
    geometry = {"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1 else {"type": "MultiPolygon", "coordinates": polygons}
    return {"components": len(polygons), "vertices": vertex_count, "geometry": geometry}


def signed_area(ring: list[list[float]]) -> float:
    return 0.5 * sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1))


metadata_rows: list[dict[str, Any]] = []
with ThreadPoolExecutor(max_workers=8) as executor:
    futures = {executor.submit(lookup_metadata, cad): cad for cad in CADASTRAL_NUMBERS}
    for future in as_completed(futures):
        row = future.result()
        metadata_rows.append(row)
        print("META", row["cadastral_number"], row.get("host"), bool(row.get("extent")), row.get("error"), flush=True)
        safe = row["cadastral_number"].replace(":", "_")
        (META_DIR / f"{safe}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")

render_rows: list[dict[str, Any]] = []
with ThreadPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(render_outline, row): row["cadastral_number"] for row in metadata_rows if row.get("extent")}
    for future in as_completed(futures):
        row = future.result()
        render_rows.append(row)
        print("RENDER", row["cadastral_number"], row.get("components"), row.get("render_service"), row.get("render_error"), flush=True)

by_cad = {row["cadastral_number"]: row for row in render_rows}
summary = []
for metadata in metadata_rows:
    row = by_cad.get(metadata["cadastral_number"], metadata)
    summary.append({key: row.get(key) for key in (
        "cadastral_number", "host", "feature_id", "attrs", "extent", "center",
        "components", "vertices", "outline_image", "geojson", "render_service",
        "error", "render_error",
    )})
summary.sort(key=lambda row: (row.get("components") or 0, row.get("vertices") or 0), reverse=True)
result = {"github_run_id": os.getenv("GITHUB_RUN_ID"), "selected_candidate": summary[0] if summary else None, "all": summary}
(OUT / "probe_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("SELECTED", json.dumps(result["selected_candidate"], ensure_ascii=False), flush=True)
