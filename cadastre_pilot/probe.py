from __future__ import annotations

import html
import json
import os
import random
import time
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

URL = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
OUT = Path("cadastre_result")
OUT.mkdir(parents=True, exist_ok=True)


def headers() -> dict[str, str]:
    zoom = random.uniform(6, 16)
    x = random.uniform(8_831_457, 13_982_833)
    y = random.uniform(11_197_632, 16_220_338)
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        "Origin": "https://nspd.gov.ru",
        "Referer": (
            "https://nspd.gov.ru/map?thematic=PKK&baseLayerId=235&theme_id=1"
            f"&zoom={zoom}&coordinate_x={x}&coordinate_y={y}&is_copy_url=true"
        ),
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


def cad_from_feature(feature: dict[str, Any]) -> str:
    props = feature.get("properties") or feature.get("attrs") or {}
    options = props.get("options") if isinstance(props, dict) else None
    merged = {**props, **options} if isinstance(options, dict) else props
    if not isinstance(merged, dict):
        return ""
    for key in ("cad_num", "cad_number", "cadastral_number", "descr", "label"):
        value = merged.get(key)
        if value:
            return str(value).strip()
    return ""


def geom_count(geometry: Any) -> tuple[str | None, int, int]:
    if not isinstance(geometry, dict):
        return None, 0, 0
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon" and isinstance(coords, list):
        return gtype, 1, len(coords)
    if gtype == "MultiPolygon" and isinstance(coords, list):
        return gtype, len(coords), sum(len(p) for p in coords if isinstance(p, list))
    if gtype == "GeometryCollection":
        geoms = geometry.get("geometries") or []
        count = 0
        rings = 0
        for g in geoms:
            _, c, r = geom_count(g)
            count += c
            rings += r
        return gtype, count, rings
    return str(gtype) if gtype else None, 0, 0


def request_one(cad: str, thematic_id: int) -> tuple[Any, int, str | None]:
    params = {"thematicSearchId": thematic_id, "query": cad, "limit": 10}
    last_error: str | None = None
    for attempt in range(4):
        try:
            response = requests.get(
                URL,
                params=params,
                headers=headers(),
                timeout=40,
                verify=True,
            )
            text = html.unescape(response.text)
            if response.status_code == 200:
                try:
                    return json.loads(text), response.status_code, None
                except json.JSONDecodeError as exc:
                    last_error = f"invalid JSON: {exc}; body={text[:300]}"
            else:
                last_error = f"HTTP {response.status_code}: {text[:300]}"
        except requests.RequestException as exc:
            last_error = repr(exc)
        time.sleep(2 ** attempt)
    return None, 0, last_error


summary: list[dict[str, Any]] = []
raw_dir = OUT / "raw_nspd"
raw_dir.mkdir(exist_ok=True)

for cad in CADASTRAL_NUMBERS:
    best: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    for thematic_id in (1, 2, 3, 4, 5):
        payload, status, error = request_one(cad, thematic_id)
        features = extract_features(payload)
        exact = [f for f in features if cad_from_feature(f) == cad]
        candidates = exact or features
        feature_summaries = []
        for idx, feature in enumerate(candidates):
            gtype, components, rings = geom_count(feature.get("geometry"))
            feature_summaries.append(
                {
                    "index": idx,
                    "cad_from_feature": cad_from_feature(feature),
                    "geometry_type": gtype,
                    "components": components,
                    "rings": rings,
                }
            )
            score = (components > 0, components, rings, bool(exact))
            if best is None or score > tuple(best["score"]):
                best = {
                    "score": list(score),
                    "thematic_search_id": thematic_id,
                    "feature": feature,
                    "payload": payload,
                    "geometry_type": gtype,
                    "components": components,
                    "rings": rings,
                }
        attempts.append(
            {
                "thematic_search_id": thematic_id,
                "http_status": status,
                "error": error,
                "feature_count": len(features),
                "exact_match_count": len(exact),
                "features": feature_summaries,
            }
        )
        if best and best["components"] > 1:
            break
        time.sleep(1.0)

    record: dict[str, Any] = {"cadastral_number": cad, "attempts": attempts}
    if best:
        record.update(
            {
                "selected_thematic_search_id": best["thematic_search_id"],
                "geometry_type": best["geometry_type"],
                "components": best["components"],
                "rings": best["rings"],
                "properties": best["feature"].get("properties") or best["feature"].get("attrs"),
            }
        )
        safe = cad.replace(":", "_")
        (raw_dir / f"{safe}.json").write_text(
            json.dumps(best["payload"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        record.update({"geometry_type": None, "components": 0, "rings": 0})
    summary.append(record)
    print(cad, record.get("geometry_type"), record.get("components"), flush=True)

summary_sorted = sorted(
    summary,
    key=lambda row: (row.get("components", 0), row.get("rings", 0)),
    reverse=True,
)
result = {
    "github_run_id": os.getenv("GITHUB_RUN_ID"),
    "selected_candidate": summary_sorted[0] if summary_sorted else None,
    "all": summary,
}
(OUT / "probe_summary.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result["selected_candidate"], ensure_ascii=False, indent=2))
