"""Taiwan official live CCTV locator & frame fetcher (from WATCH data source list).

All 3 sources are 100% free and require NO API keys:
1. WRA (水利署河川／抽水站／水庫 CCTV): fhyv.wra.gov.tw (station list + 10-min JPG timelapse)
2. MOA / ARDSWC (農村水保署崩塌／土石流 CCTV): data.moa.gov.tw (direct live image URL)
3. Freeway Bureau (高公局國道即時 CCTV): tisvcloud.freeway.gov.tw (live MJPEG stream)
"""

from typing import Any
import xml.etree.ElementTree as ET

from .common import fetch_bytes, fetch_json, now_taiwan_iso
from .location import (
    compass_direction,
    format_distance,
    haversine_meters,
    normalize_taiwan_coords,
)

WRA_CCTV_STATION_URL = "https://fhyv.wra.gov.tw/FhyWeb/v1/Api/CCTV/WRA/Station?$format=JSON"
MOA_LANDSLIDE_CCTV_URL = "https://data.moa.gov.tw/Service/OpenData/DataFileService.aspx?UnitId=111&IsTransData=1"
FREEWAY_CCTV_URL = "https://tisvcloud.freeway.gov.tw/history/motc20/CCTV.xml"

STATION_CACHE_TTL = 1800  # 30 分鐘快取站點清單，點地圖瞬間回應


def _parse_float(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _in_taiwan(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and 21.0 <= lat <= 26.8
        and 117.5 <= lon <= 123.5
    )


def _load_wra_stations() -> list[dict[str, Any]]:
    try:
        raw = fetch_json(WRA_CCTV_STATION_URL, timeout=12, cache_ttl=STATION_CACHE_TTL)
        if not isinstance(raw, list):
            return []
        items: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            lat = _parse_float(row.get("lat"))
            lon = _parse_float(row.get("lng"))
            if not _in_taiwan(lat, lon):
                continue
            items.append(
                {
                    "category": "wra",
                    "category_label": "水利署河川／防汛 CCTV",
                    "id": f"wra-{row.get('SourceId')}-{row.get('ID')}",
                    "source_id": row.get("SourceId"),
                    "station_id": str(row.get("ID", "")),
                    "name": str(row.get("Name") or "水利署測站").strip(),
                    "area": str(row.get("Town_name") or row.get("Counname") or "").strip(),
                    "basin": str(row.get("Basin_name") or "").strip(),
                    "lat": lat,
                    "lon": lon,
                    "stream_type": "wra_dynamic",
                    "image_url": "",
                }
            )
        return items
    except Exception:
        return []


def _load_landslide_stations() -> list[dict[str, Any]]:
    try:
        raw = fetch_json(MOA_LANDSLIDE_CCTV_URL, timeout=12, cache_ttl=STATION_CACHE_TTL)
        if not isinstance(raw, list):
            return []
        items: list[dict[str, Any]] = []
        for idx, row in enumerate(raw):
            if not isinstance(row, dict):
                continue
            lat = _parse_float(row.get("緯度"))
            lon = _parse_float(row.get("經度"))
            img = str(row.get("影像連結網址") or "").strip()
            if not _in_taiwan(lat, lon) or not img:
                continue
            county = str(row.get("縣市") or "").strip()
            town = str(row.get("行政區") or "").strip()
            items.append(
                {
                    "category": "landslide",
                    "category_label": "農村水保署崩塌／土石流 CCTV",
                    "id": f"moa-{idx}",
                    "name": str(row.get("攝影機名稱") or "崩塌監測攝影機").strip(),
                    "area": f"{county}{town}",
                    "location_desc": str(row.get("架設或拍攝地點") or "").strip(),
                    "lat": lat,
                    "lon": lon,
                    "stream_type": "image",
                    "image_url": img,
                }
            )
        return items
    except Exception:
        return []


def _load_freeway_stations() -> list[dict[str, Any]]:
    try:
        raw = fetch_bytes(FREEWAY_CCTV_URL, timeout=15, cache_ttl=STATION_CACHE_TTL)
        root = ET.fromstring(raw)
        ns = {"t": "http://traffic.transportdata.tw/standard/traffic/schema/"}
        items: list[dict[str, Any]] = []
        for el in root.findall(".//t:CCTV", ns):
            lat = _parse_float(el.findtext("t:PositionLat", "", ns))
            lon = _parse_float(el.findtext("t:PositionLon", "", ns))
            stream = (el.findtext("t:VideoStreamURL", "", ns) or "").strip()
            if not _in_taiwan(lat, lon) or not stream:
                continue
            road = (el.findtext("t:RoadName", "", ns) or "").strip()
            mile = (el.findtext("t:LocationMile", "", ns) or "").strip()
            direction = (el.findtext("t:RoadDirection", "", ns) or "").strip()
            cctv_id = (el.findtext("t:CCTVID", "", ns) or "").strip()
            dir_zh = {"N": "北向", "S": "南向", "E": "東向", "W": "西向"}.get(direction, direction)
            name = f"{road} {dir_zh} {mile}".strip() or cctv_id
            items.append(
                {
                    "category": "freeway",
                    "category_label": "高公局國道即時 CCTV",
                    "id": f"fw-{cctv_id}",
                    "name": name,
                    "area": road,
                    "lat": lat,
                    "lon": lon,
                    "stream_type": "mjpeg",
                    "image_url": stream,
                }
            )
        return items
    except Exception:
        return []


def find_nearby_cctvs(
    latitude: float,
    longitude: float,
    limit: int = 8,
    max_distance_km: float = 25.0,
) -> dict[str, Any]:
    """依使用者 GPS 座標找出附近的水利署河川、農村水保署崩塌地與國道即時 CCTV 監視器。"""
    lat, lon, _ = normalize_taiwan_coords(float(latitude), float(longitude))
    max_m = max_distance_km * 1000.0
    limit = max(1, min(int(limit or 8), 20))

    wra = _load_wra_stations()
    landslide = _load_landslide_stations()
    freeway = _load_freeway_stations()

    def _nearest(pool: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
        scored = []
        for st in pool:
            d = haversine_meters(lat, lon, st["lat"], st["lon"])
            if d <= max_m:
                scored.append((d, st))
        scored.sort(key=lambda x: x[0])
        out = []
        for d, st in scored[:cap]:
            bearing = compass_direction(lat, lon, st["lat"], st["lon"])
            out.append(
                {
                    **st,
                    "distance_meters": int(round(d)),
                    "distance_text": f"往{bearing} {format_distance(d)}",
                }
            )
        return out

    # 兼顧多元監測類型：優先保留附近的水利署河川、崩塌地與國道攝影機
    near_wra = _nearest(wra, limit)
    near_landslide = _nearest(landslide, min(3, limit))
    near_freeway = _nearest(freeway, min(3, limit))

    combined = near_wra[:4] + near_landslide[:2] + near_freeway[:2]
    seen_ids = {c["id"] for c in combined}
    for extra in sorted(near_wra + near_landslide + near_freeway, key=lambda x: x["distance_meters"]):
        if len(combined) >= limit:
            break
        if extra["id"] not in seen_ids:
            seen_ids.add(extra["id"])
            combined.append(extra)

    combined.sort(key=lambda x: x["distance_meters"])
    return {
        "status": "ok",
        "queried_at": now_taiwan_iso(),
        "total_catalog": {
            "wra": len(wra),
            "landslide": len(landslide),
            "freeway": len(freeway),
        },
        "count": len(combined[:limit]),
        "data": combined[:limit],
    }


def get_wra_camera_frames(source_id: int, station_id: str) -> dict[str, Any]:
    """即時取得指定水利署 CCTV 測站的最新畫面與近 10 分鐘輪播圖清單。"""
    url = f"https://fhyv.wra.gov.tw/FhyWeb/v1/Api/CCTV/WRA/Cameras/{source_id}/{station_id}?$format=JSON"
    try:
        raw = fetch_json(url, timeout=10, cache_ttl=30)
        if not isinstance(raw, list) or not raw:
            return {"status": "empty", "message": "此測站目前沒有回傳影像。", "cameras": []}

        station = raw[0]
        cameras = []
        for cam in station.get("cameras") or []:
            imgs = [u for u in (cam.get("images") or []) if isinstance(u, str) and u.startswith("http")]
            if imgs:
                cameras.append(
                    {
                        "camera_id": str(cam.get("id", "")),
                        "latest_image": imgs[-1],
                        "images": imgs,
                    }
                )
        return {
            "status": "ok" if cameras else "empty",
            "queried_at": now_taiwan_iso(),
            "station_name": station.get("Name", ""),
            "basin": station.get("Basin_name", ""),
            "cameras": cameras,
            "message": "" if cameras else "此測站暫無即時影像畫面（可能維護中）。",
        }
    except Exception as e:
        return {
            "status": "error",
            "queried_at": now_taiwan_iso(),
            "message": f"取得水利署即時影像失敗：{e}",
            "cameras": [],
        }
