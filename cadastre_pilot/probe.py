from __future__ import annotations

import html
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

CADASTRAL_NUMBERS = [
    "22:43:010001:1229",
    "22:43:010001:1233",
    "22:43:010001:1246",
    "22:43:010001:1272",
    "22:43:040001:1313",
    "22:43:040001:1564",
    "22:43:040001:1571",
    "22:43:040001:1575",
    "22:43:010001:1227",
    "22:43:010001:1235",
    "22:43:010001:1236",
    "22:43:040001:1300",
    "22:43:040001:1301",
    "22:43:040001:1302",
    "22:43:040002:513",
]
THEMATIC_IDS = (1, 2)
URL = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
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
    "Referer": "https://nspd.gov.ru/map?thematic=PKK&baseLayerId=235&theme_id=1",
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
    if isinstance(payload, list):
        return payload
    return []


def merged_properties(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or feature.get("attrs") or {}
    if not isinstance(props, dict):
        return {}
    options = props.get("options")
    return {**props, **options} if isinstance(options, dict) else props


def feature_cad(feature: dict[str, Any]) -> str:
    props = merged_properties(feature)
    for key in ("cad_num", "cad_number", "cadastral_number", "descr", "label"):
        value = props.get(key)
        if value:
            return str(value).strip()
    return ""


def polygon_parts(geometry: Any) -> list[list[list[list[float]]]]:
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon" and isinstance(coords, list):
        return [coords]
    if gtype == "MultiPolygon" and isinstance(coords, list):
        return [part for part in coords if isinstance(part, list)]
    if gtype == "GeometryCollection":
        parts: list[list[list[list[float]]]] = []
        for item in geometry.get("geometries") or []:
            parts.extend(polygon_parts(item))
        return parts
    return []


def vertex_count(parts: list[list[list[list[float]]]]) -> int:
    return sum(len(ring) for polygon in parts for ring in polygon if isinstance(ring, list))


def fetch_one(cad: str, thematic_id: int) -> dict[str, Any]:
    params = {"thematicSearchId": thematic_id, "query": cad, "limit": 20}
    last_error: str | None = None
    for attempt in range(2):
        try:
            response = requests.get(
                URL,
                params=params,
                headers=HEADERS,
                timeout=(10, 25),
            )
            text = html.unescape(response.text)
            if response.status_code == 200:
                payload = json.loads(text)
                features = extract_features(payload)
                exact = [feature for feature in features if feature_cad(feature) == cad]
                selected = exact or features
                polygon_features = []
                parts = []
                for feature in selected:
                    feature_parts = polygon_parts(feature.get("geometry"))
                    if feature_parts:
                        polygon_features.append(feature)
                        parts.extend(feature_parts)
                return {
                    "cadastral_number": cad,
                    "thematic_search_id": thematic_id,
                    "http_status": response.status_code,
                    "error": None,
                    "payload": payload,
                    "features": features,
                    "selected_features": selected,
                    "polygon_features": polygon_features,
                    "exact_match_count": len(exact),
                    "feature_count": len(features),
                    "components": len(parts),
                    "vertices": vertex_count(parts),
                }
            last_error = f"HTTP {response.status_code}: {text[:300]}"
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = repr(exc)
        if attempt == 0:
            time.sleep(0.8)
    return {
        "cadastral_number": cad,
        "thematic_search_id": thematic_id,
        "http_status": 0,
        "error": last_error,
        "payload": None,
        "features": [],
        "selected_features": [],
        "polygon_features": [],
        "exact_match_count": 0,
        "feature_count": 0,
        "components": 0,
        "vertices": 0,
    }


results: dict[str, list[dict[str, Any]]] = {cad: [] for cad in CADASTRAL_NUMBERS}
with ThreadPoolExecutor(max_workers=4) as pool:
    futures = {
        pool.submit(fetch_one, cad, thematic_id): (cad, thematic_id)
        for cad in CADASTRAL_NUMBERS
        for thematic_id in THEMATIC_IDS
    }
    for future in as_completed(futures):
        row = future.result()
        results[row["cadastral_number"]].append(row)
        print(
            row["cadastral_number"],
            "theme", row["thematic_search_id"],
            "features", row["feature_count"],
            "components", row["components"],
            "error", row["error"],
            flush=True,
        )

summary: list[dict[str, Any]] = []
for cad in CADASTRAL_NUMBERS:
    attempts = results[cad]
    best = max(
        attempts,
        key=lambda row: (
            row["components"] > 0,
            row["exact_match_count"] > 0,
            row["components"],
            row["vertices"],
        ),
    )
    safe = cad.replace(":", "_")
    if best["payload"] is not None:
        (RAW / f"{safe}_theme{best['thematic_search_id']}.json").write_text(
            json.dumps(best["payload"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if best["polygon_features"]:
        feature_collection = {
            "type": "FeatureCollection",
            "features": best["polygon_features"],
        }
        (GEO / f"{safe}.geojson").write_text(
            json.dumps(feature_collection, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    properties = merged_properties(best["selected_features"][0]) if best["selected_features"] else {}
    record = {
        "cadastral_number": cad,
        "selected_thematic_search_id": best["thematic_search_id"],
        "components": best["components"],
        "vertices": best["vertices"],
        "exact_match_count": best["exact_match_count"],
        "feature_count": best["feature_count"],
        "properties": properties,
        "attempts": [
            {
                "thematic_search_id": attempt["thematic_search_id"],
                "http_status": attempt["http_status"],
                "error": attempt["error"],
                "feature_count": attempt["feature_count"],
                "exact_match_count": attempt["exact_match_count"],
                "components": attempt["components"],
                "vertices": attempt["vertices"],
            }
            for attempt in sorted(attempts, key=lambda item: item["thematic_search_id"])
        ],
    }
    summary.append(record)

summary.sort(key=lambda row: (row["components"], row["vertices"]), reverse=True)
result = {
    "github_run_id": os.getenv("GITHUB_RUN_ID"),
    "selected_candidate": summary[0] if summary else None,
    "all": summary,
}
(OUT / "probe_summary.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print("SELECTED", json.dumps(result["selected_candidate"], ensure_ascii=False), flush=True)
