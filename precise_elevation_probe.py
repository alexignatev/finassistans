from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

OUT = Path("precise_elevation_probe_output")
OUT.mkdir(exist_ok=True)
BBOX = [83.8489, 51.8516, 83.9492, 51.8925]
KEYWORDS = ("dem", "elevation", "terrain", "nasadem", "fabdem", "alos", "srtm", "copernicus")

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 Chrome/151 Safari/537.36", "Accept": "application/json,*/*"})
report: dict[str, Any] = {"bbox": BBOX, "checks": [], "planetary_computer": {}, "copernicus_dataspace": {}}


def get_json(url: str, **kwargs: Any) -> Any:
    response = session.get(url, timeout=60, **kwargs)
    response.raise_for_status()
    return response.json()


def record_url(url: str) -> None:
    try:
        r = session.get(url, timeout=60, allow_redirects=True)
        report["checks"].append({
            "url": url,
            "status": r.status_code,
            "content_type": r.headers.get("content-type"),
            "content_length": r.headers.get("content-length"),
            "downloaded_bytes": len(r.content),
            "final_url": r.url,
            "head": r.text[:300] if "text" in (r.headers.get("content-type") or "") else None,
        })
    except Exception as exc:
        report["checks"].append({"url": url, "error": repr(exc)})


# Microsoft Planetary Computer: enumerate all candidate elevation collections.
pc_root = "https://planetarycomputer.microsoft.com/api/stac/v1"
try:
    collections = get_json(f"{pc_root}/collections").get("collections", [])
    matches = []
    for c in collections:
        haystack = " ".join(str(c.get(k, "")) for k in ("id", "title", "description", "keywords")).lower()
        if any(word in haystack for word in KEYWORDS):
            matches.append({
                "id": c.get("id"),
                "title": c.get("title"),
                "description": c.get("description"),
                "extent": c.get("extent"),
                "item_assets": c.get("item_assets"),
                "summaries": c.get("summaries"),
            })
    report["planetary_computer"]["candidate_collections"] = matches

    searches = {}
    for c in matches:
        collection_id = c.get("id")
        try:
            payload = get_json(
                f"{pc_root}/search",
                params={
                    "collections": collection_id,
                    "bbox": ",".join(map(str, BBOX)),
                    "limit": 10,
                },
            )
            items = payload.get("features", [])
            searches[collection_id] = [{
                "id": item.get("id"),
                "bbox": item.get("bbox"),
                "properties": item.get("properties"),
                "assets": item.get("assets"),
            } for item in items]
        except Exception as exc:
            searches[collection_id] = {"error": repr(exc)}
    report["planetary_computer"]["bbox_searches"] = searches
except Exception as exc:
    report["planetary_computer"]["error"] = repr(exc)


# Copernicus Data Space STAC: enumerate collection IDs containing DEM/elevation.
cdse_root = "https://stac.dataspace.copernicus.eu/v1"
try:
    collections = get_json(f"{cdse_root}/collections").get("collections", [])
    matches = []
    for c in collections:
        haystack = " ".join(str(c.get(k, "")) for k in ("id", "title", "description", "keywords")).lower()
        if any(word in haystack for word in KEYWORDS):
            matches.append({
                "id": c.get("id"),
                "title": c.get("title"),
                "description": c.get("description"),
                "extent": c.get("extent"),
                "item_assets": c.get("item_assets"),
                "summaries": c.get("summaries"),
            })
    report["copernicus_dataspace"]["candidate_collections"] = matches
    searches = {}
    for c in matches:
        collection_id = c.get("id")
        try:
            payload = get_json(
                f"{cdse_root}/search",
                params={
                    "collections": collection_id,
                    "bbox": ",".join(map(str, BBOX)),
                    "limit": 10,
                },
            )
            searches[collection_id] = payload.get("features", [])
        except Exception as exc:
            searches[collection_id] = {"error": repr(exc)}
    report["copernicus_dataspace"]["bbox_searches"] = searches
except Exception as exc:
    report["copernicus_dataspace"]["error"] = repr(exc)


# Known open terrain/elevation file endpoints for the 51N 83E tile.
for url in [
    "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N51/N51E083.hgt.gz",
    "https://elevation-tiles-prod.s3.amazonaws.com/skadi/N51/N51E083.hgt.gz",
    "https://copernicus-dem-30m.s3.amazonaws.com/readme.html",
    "https://copernicus-dem-30m.s3.amazonaws.com/",
    "https://prism-dem-open.copernicus.eu/pd-desk-open-access/prismDownload/COP-DEM_GLO-30-DGED__2023_1/",
    "https://portal.opentopography.org/API/globaldem?demtype=COP30&south=51.8516&north=51.8925&west=83.8489&east=83.9492&outputFormat=GTiff",
    "https://portal.opentopography.org/API/globaldem?demtype=NASADEM&south=51.8516&north=51.8925&west=83.8489&east=83.9492&outputFormat=GTiff",
    "https://portal.opentopography.org/API/globaldem?demtype=AW3D30&south=51.8516&north=51.8925&west=83.8489&east=83.9492&outputFormat=GTiff",
    "https://portal.opentopography.org/API/globaldem?demtype=SRTMGL1&south=51.8516&north=51.8925&west=83.8489&east=83.9492&outputFormat=GTiff",
]:
    record_url(url)

(OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({
    "pc_candidates": len(report.get("planetary_computer", {}).get("candidate_collections", [])),
    "cdse_candidates": len(report.get("copernicus_dataspace", {}).get("candidate_collections", [])),
    "checks": len(report["checks"]),
}, ensure_ascii=False))
