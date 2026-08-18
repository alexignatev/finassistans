from __future__ import annotations

import html
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests

CADASTRAL_NUMBERS = [
    "22:43:010001:1229", "22:43:010001:1233", "22:43:010001:1246",
    "22:43:010001:1272", "22:43:040001:1313", "22:43:040001:1564",
    "22:43:040001:1571", "22:43:040001:1575", "22:43:010001:1227",
    "22:43:010001:1235", "22:43:010001:1236", "22:43:040001:1300",
    "22:43:040001:1301", "22:43:040001:1302", "22:43:040002:513",
]
BASE_PATH = "nspd.gov.ru/api/geoportal/v2/search/geoportal"
OUT = Path("cadastre_result")
RAW = OUT / "raw_nspd"
GEO = OUT / "geojson"
for directory in (OUT, RAW, GEO):
    directory.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "X-No-Cache": "true",
}


def extract_json(text: str) -> Any:
    text = html.unescape(text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    marker = "Markdown Content:"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    decoder = json.JSONDecoder()
    for token in ("{", "["):
        index = text.find(token)
        while index >= 0:
            try:
                value, _ = decoder.raw_decode(text[index:])
                return value
            except json.JSONDecodeError:
                index = text.find(token, index + 1)
    raise ValueError(f"No JSON found in response: {text[:300]}")


def features(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("features"), list):
            return payload["features"]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("features"), list):
            return data["features"]
        if isinstance(data, list):
            return data
    return payload if isinstance(payload, list) else []


def merged_properties(feature: dict[str, Any]) -> dict[str, Any]:
    value = feature.get("properties") or feature.get("attrs") or {}
    if not isinstance(value, dict):
        return {}
    options = value.get("options")
    return {**value, **options} if isinstance(options, dict) else value


def cadastral_number(feature: dict[str, Any]) -> str:
    values = merged_properties(feature)
    for key in ("cad_num", "cad_number", "cadastral_number", "descr", "label"):
        if values.get(key):
            return str(values[key]).strip()
    return ""


def polygon_parts(geometry: Any) -> list[Any]:
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "Polygon" and isinstance(coords, list):
        return [coords]
    if kind == "MultiPolygon" and isinstance(coords, list):
        return [item for item in coords if isinstance(item, list)]
    if kind == "GeometryCollection":
        result: list[Any] = []
        for item in geometry.get("geometries") or []:
            result.extend(polygon_parts(item))
        return result
    return []


def proxy_urls(cad: str, thematic_id: int) -> list[tuple[str, str]]:
    query = urlencode({"thematicSearchId": thematic_id, "query": cad, "limit": 20})
    https_target = f"https://{BASE_PATH}?{query}"
    http_target = f"http://{BASE_PATH}?{query}"
    return [
        ("jina_https", f"https://r.jina.ai/{https_target}"),
        ("jina_http", f"https://r.jina.ai/{http_target}"),
        ("allorigins", f"https://api.allorigins.win/raw?url={quote(https_target, safe='')}"),
        ("corsproxy", f"https://corsproxy.io/?url={quote(https_target, safe='')}"),
    ]


def query(cad: str, thematic_id: int) -> dict[str, Any]:
    errors: list[str] = []
    for source, url in proxy_urls(cad, thematic_id):
        try:
            response = requests.get(url, headers=HEADERS, timeout=(8, 35))
            if response.status_code != 200:
                errors.append(f"{source}: HTTP {response.status_code} {response.text[:120]}")
                continue
            payload = extract_json(response.text)
            found = features(payload)
            exact = [feature for feature in found if cadastral_number(feature) == cad]
            selected = exact or found
            polygons: list[dict[str, Any]] = []
            components = 0
            vertices = 0
            for feature in selected:
                parts = polygon_parts(feature.get("geometry"))
                if parts:
                    polygons.append(feature)
                    components += len(parts)
                    vertices += sum(len(ring) for polygon in parts for ring in polygon if isinstance(ring, list))
            return {
                "cadastral_number": cad,
                "source": source,
                "thematic_search_id": thematic_id,
                "payload": payload,
                "features": found,
                "selected_features": selected,
                "polygon_features": polygons,
                "feature_count": len(found),
                "exact_match_count": len(exact),
                "components": components,
                "vertices": vertices,
                "error": None,
            }
        except Exception as exc:
            errors.append(f"{source}: {exc!r}")
    return {
        "cadastral_number": cad, "source": "proxy_failed", "thematic_search_id": thematic_id,
        "payload": None, "features": [], "selected_features": [], "polygon_features": [],
        "feature_count": 0, "exact_match_count": 0, "components": 0, "vertices": 0,
        "error": " | ".join(errors),
    }


def parallel(cads: list[str], thematic_id: int) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(query, cad, thematic_id): cad for cad in cads}
        for future in as_completed(futures):
            row = future.result()
            result[row["cadastral_number"]] = row
            print(row["cadastral_number"], row["source"], row["thematic_search_id"], row["feature_count"], row["components"], row["error"], flush=True)
    return result


primary = parallel(CADASTRAL_NUMBERS, 1)
missing = [cad for cad, row in primary.items() if row["components"] == 0]
secondary = parallel(missing, 2) if missing else {}

summary: list[dict[str, Any]] = []
for cad in CADASTRAL_NUMBERS:
    candidates = [primary[cad]]
    if cad in secondary:
        candidates.append(secondary[cad])
    best = max(candidates, key=lambda row: (row["components"] > 0, row["exact_match_count"] > 0, row["components"], row["vertices"]))
    safe = cad.replace(":", "_")
    if best["payload"] is not None:
        (RAW / f"{safe}_{best['source']}_theme{best['thematic_search_id']}.json").write_text(json.dumps(best["payload"], ensure_ascii=False, indent=2), encoding="utf-8")
    if best["polygon_features"]:
        (GEO / f"{safe}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": best["polygon_features"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    properties = merged_properties(best["selected_features"][0]) if best["selected_features"] else {}
    summary.append({
        "cadastral_number": cad,
        "source": best["source"],
        "selected_thematic_search_id": best["thematic_search_id"],
        "components": best["components"],
        "vertices": best["vertices"],
        "exact_match_count": best["exact_match_count"],
        "feature_count": best["feature_count"],
        "properties": properties,
        "attempts": [{key: item[key] for key in ("source", "thematic_search_id", "feature_count", "exact_match_count", "components", "vertices", "error")} for item in candidates],
    })

summary.sort(key=lambda row: (row["components"], row["vertices"]), reverse=True)
result = {"github_run_id": os.getenv("GITHUB_RUN_ID"), "selected_candidate": summary[0] if summary else None, "all": summary}
(OUT / "probe_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("SELECTED", json.dumps(result["selected_candidate"], ensure_ascii=False), flush=True)
