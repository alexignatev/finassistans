from __future__ import annotations

import html
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

CADASTRAL_NUMBERS = [
    "22:43:010001:1229", "22:43:010001:1233", "22:43:010001:1246",
    "22:43:010001:1272", "22:43:040001:1313", "22:43:040001:1564",
    "22:43:040001:1571", "22:43:040001:1575", "22:43:010001:1227",
    "22:43:010001:1235", "22:43:010001:1236", "22:43:040001:1300",
    "22:43:040001:1301", "22:43:040001:1302", "22:43:040002:513",
]
URL = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
MAPRU_URL = "https://mapruapp.ru/api/cadastre/search"
OUT = Path("cadastre_result")
RAW = OUT / "raw_nspd"
GEO = OUT / "geojson"
for directory in (OUT, RAW, GEO):
    directory.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "Origin": "https://nspd.gov.ru",
    "Referer": "https://nspd.gov.ru/map?thematic=PKK",
}


def extract_features(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("features"), list):
            return payload["features"]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("features"), list):
            return data["features"]
        if isinstance(data, list):
            return data
    return payload if isinstance(payload, list) else []


def props(feature: dict[str, Any]) -> dict[str, Any]:
    value = feature.get("properties") or feature.get("attrs") or {}
    if not isinstance(value, dict):
        return {}
    options = value.get("options")
    return {**value, **options} if isinstance(options, dict) else value


def cad_of(feature: dict[str, Any]) -> str:
    p = props(feature)
    for key in ("cad_num", "cad_number", "cadastral_number", "descr", "label"):
        if p.get(key):
            return str(p[key]).strip()
    return ""


def polygon_parts(geometry: Any) -> list[Any]:
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon" and isinstance(coordinates, list):
        return [coordinates]
    if kind == "MultiPolygon" and isinstance(coordinates, list):
        return [item for item in coordinates if isinstance(item, list)]
    if kind == "GeometryCollection":
        result: list[Any] = []
        for item in geometry.get("geometries") or []:
            result.extend(polygon_parts(item))
        return result
    return []


def inspect_payload(cad: str, payload: Any, source: str, thematic_id: int | None) -> dict[str, Any]:
    features = extract_features(payload)
    exact = [feature for feature in features if cad_of(feature) == cad]
    selected = exact or features
    polygon_features: list[dict[str, Any]] = []
    components = 0
    vertices = 0
    for feature in selected:
        parts = polygon_parts(feature.get("geometry"))
        if not parts:
            continue
        polygon_features.append(feature)
        components += len(parts)
        vertices += sum(len(ring) for polygon in parts for ring in polygon if isinstance(ring, list))
    return {
        "cadastral_number": cad,
        "source": source,
        "thematic_search_id": thematic_id,
        "payload": payload,
        "features": features,
        "selected_features": selected,
        "polygon_features": polygon_features,
        "feature_count": len(features),
        "exact_match_count": len(exact),
        "components": components,
        "vertices": vertices,
        "error": None,
    }


def query_nspd(cad: str, thematic_id: int) -> dict[str, Any]:
    try:
        response = requests.get(
            URL,
            params={"thematicSearchId": thematic_id, "query": cad, "limit": 20},
            headers=HEADERS,
            timeout=(5, 12),
        )
        text = html.unescape(response.text)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {text[:250]}")
        return inspect_payload(cad, json.loads(text), "nspd", thematic_id)
    except Exception as exc:
        return {
            "cadastral_number": cad, "source": "nspd", "thematic_search_id": thematic_id,
            "payload": None, "features": [], "selected_features": [], "polygon_features": [],
            "feature_count": 0, "exact_match_count": 0, "components": 0, "vertices": 0,
            "error": repr(exc),
        }


def query_mapru(cad: str) -> dict[str, Any]:
    try:
        response = requests.get(MAPRU_URL, params={"q": cad}, timeout=(5, 15))
        response.raise_for_status()
        body = response.json()
        records = body.get("data") if isinstance(body, dict) else None
        features = []
        for record in records or []:
            if str(record.get("cad_number", "")).strip() != cad:
                continue
            raw = record.get("contours_json")
            contours = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
            if not contours:
                continue
            polygons = []
            for contour in contours:
                ring = []
                for point in contour:
                    if isinstance(point, dict):
                        x, y = point.get("x"), point.get("y")
                    else:
                        x, y = point[0], point[1]
                    ring.append([x, y])
                if ring and ring[0] != ring[-1]:
                    ring.append(ring[0])
                if ring:
                    polygons.append([ring])
            geometry = {"type": "MultiPolygon", "coordinates": polygons} if len(polygons) > 1 else {"type": "Polygon", "coordinates": polygons[0]}
            features.append({"type": "Feature", "geometry": geometry, "properties": record})
        return inspect_payload(cad, {"features": features}, "mapruapp", None)
    except Exception as exc:
        return {
            "cadastral_number": cad, "source": "mapruapp", "thematic_search_id": None,
            "payload": None, "features": [], "selected_features": [], "polygon_features": [],
            "feature_count": 0, "exact_match_count": 0, "components": 0, "vertices": 0,
            "error": repr(exc),
        }


def run_batch(jobs: list[tuple[str, int]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {cad: [] for cad in CADASTRAL_NUMBERS}
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(query_nspd, cad, theme): cad for cad, theme in jobs}
        for future in as_completed(futures):
            row = future.result()
            result[row["cadastral_number"]].append(row)
            print(row["cadastral_number"], row["source"], row["thematic_search_id"], row["components"], row["error"], flush=True)
    return result


all_attempts = run_batch([(cad, 2) for cad in CADASTRAL_NUMBERS])
missing = [cad for cad in CADASTRAL_NUMBERS if not any(row["components"] for row in all_attempts[cad])]
if missing:
    fallback = run_batch([(cad, 1) for cad in missing])
    for cad in missing:
        all_attempts[cad].extend(fallback[cad])

still_missing = [cad for cad in CADASTRAL_NUMBERS if not any(row["components"] for row in all_attempts[cad])]
if still_missing:
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(query_mapru, cad): cad for cad in still_missing}
        for future in as_completed(futures):
            row = future.result()
            all_attempts[row["cadastral_number"]].append(row)
            print(row["cadastral_number"], row["source"], row["components"], row["error"], flush=True)

summary: list[dict[str, Any]] = []
for cad in CADASTRAL_NUMBERS:
    attempts = all_attempts[cad]
    best = max(attempts, key=lambda row: (row["components"] > 0, row["exact_match_count"] > 0, row["components"], row["vertices"]))
    safe = cad.replace(":", "_")
    if best["payload"] is not None:
        (RAW / f"{safe}_{best['source']}.json").write_text(json.dumps(best["payload"], ensure_ascii=False, indent=2), encoding="utf-8")
    if best["polygon_features"]:
        (GEO / f"{safe}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": best["polygon_features"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    selected_properties = props(best["selected_features"][0]) if best["selected_features"] else {}
    summary.append({
        "cadastral_number": cad,
        "source": best["source"],
        "selected_thematic_search_id": best["thematic_search_id"],
        "components": best["components"],
        "vertices": best["vertices"],
        "exact_match_count": best["exact_match_count"],
        "feature_count": best["feature_count"],
        "properties": selected_properties,
        "attempts": [{key: row[key] for key in ("source", "thematic_search_id", "feature_count", "exact_match_count", "components", "vertices", "error")} for row in attempts],
    })

summary.sort(key=lambda row: (row["components"], row["vertices"]), reverse=True)
result = {"github_run_id": os.getenv("GITHUB_RUN_ID"), "selected_candidate": summary[0] if summary else None, "all": summary}
(OUT / "probe_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("SELECTED", json.dumps(result["selected_candidate"], ensure_ascii=False), flush=True)
