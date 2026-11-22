"""GPS reverse geocoding, distance/bearing calculation, and evacuation routing tools."""

import math
from typing import Any
from urllib.parse import urlencode

from .common import fetch_json, now_taiwan_iso

# 離線或 Nominatim 逾時時的台灣重點鄉鎮中心點備援表
FALLBACK_TOWNSHIPS: list[tuple[str, str, float, float]] = [
    ("高雄市", "三民區", 22.6465, 120.3145),
    ("高雄市", "鼓山區", 22.6550, 120.2850),
    ("高雄市", "左營區", 22.6850, 120.2940),
    ("高雄市", "仁武區", 22.6950, 120.3480),
    ("高雄市", "美濃區", 22.9000, 120.5420),
    ("高雄市", "岡山區", 22.7960, 120.2960),
    ("高雄市", "茂林區", 22.8900, 120.6650),
    ("高雄市", "桃源區", 23.1570, 120.7610),
    ("屏東縣", "東港鎮", 22.4650, 120.4560),
    ("屏東縣", "鹽埔鄉", 22.7540, 120.5730),
    ("屏東縣", "三地門鄉", 22.7140, 120.6540),
    ("屏東縣", "霧臺鄉", 22.7490, 120.7280),
    ("新北市", "汐止區", 25.0660, 121.6600),
    ("新北市", "板橋區", 25.0120, 121.4620),
    ("臺北市", "信義區", 25.0330, 121.5640),
    ("臺北市", "大安區", 25.0260, 121.5430),
    ("宜蘭縣", "南澳鄉", 24.4650, 121.7980),
    ("花蓮縣", "秀林鄉", 24.1160, 121.6220),
    ("花蓮縣", "花蓮市", 23.9870, 121.6010),
    ("臺中市", "西屯區", 24.1630, 120.6400),
    ("臺南市", "安南區", 23.0480, 120.1850),
    ("桃園市", "桃園區", 24.9930, 121.3010),
]

COMPASS_8 = ["北方", "東北方", "東方", "東南方", "南方", "西南方", "西方", "西北方"]

MANEUVER_ZH: dict[str, str] = {
    "depart": "出發",
    "arrive": "抵達目的地",
    "turn": "轉彎",
    "new name": "續行",
    "merge": "匯入",
    "fork": "分岔路口",
    "roundabout": "圓環",
    "continue": "直行",
    "end of road": "路口底轉向",
}

MODIFIER_ZH: dict[str, str] = {
    "left": "左轉",
    "right": "右轉",
    "slight left": "微左轉",
    "slight right": "微右轉",
    "sharp left": "急左轉",
    "sharp right": "急右轉",
    "straight": "直行",
    "uturn": "迴轉",
}


def normalize_taiwan_coords(latitude: float, longitude: float) -> tuple[float, float, bool]:
    """Auto-correct accidentally swapped (lon, lat) pairs for Taiwan."""
    if 118.0 <= latitude <= 123.5 and 21.0 <= longitude <= 26.5:
        return longitude, latitude, True
    return latitude, longitude, False


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in meters between two WGS84 points."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compass_direction(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Compute human-readable Chinese 8-point compass bearing from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    idx = int((bearing + 22.5) // 45) % 8
    return COMPASS_8[idx]


def format_distance(meters: float) -> str:
    if meters < 1000:
        return f"約 {int(round(meters))} 公尺"
    return f"約 {meters / 1000:.2f} 公里"


def build_google_maps_dir_url(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    travel_mode: str = "walking",
) -> str:
    params = urlencode(
        {
            "api": "1",
            "origin": f"{origin_lat:.6f},{origin_lon:.6f}",
            "destination": f"{dest_lat:.6f},{dest_lon:.6f}",
            "travelmode": travel_mode,
        }
    )
    return f"https://www.google.com/maps/dir/?{params}"


def resolve_gps_location(latitude: float, longitude: float) -> dict:
    """將使用者的 GPS 經緯度轉換成台灣縣市、鄉鎮市區與附近道路名稱。

    Args:
        latitude: 緯度（台灣約 21.5 ~ 26.5，例如 22.6465）。
        longitude: 經度（台灣約 118.0 ~ 122.5，例如 120.3208）。

    Returns:
        包含標準化座標、縣市、鄉鎮市區、道路／完整地址與定位來源的字典。
    """
    lat, lon, swapped = normalize_taiwan_coords(float(latitude), float(longitude))
    queried_at = now_taiwan_iso()

    if not (21.0 <= lat <= 26.8 and 117.5 <= lon <= 123.5):
        return {
            "status": "error",
            "queried_at": queried_at,
            "error": f"座標 ({lat}, {lon}) 不在台灣本島及離島常見範圍內，請確認經緯度是否正確。",
        }

    # 1. 優先嘗試 OpenStreetMap Nominatim 反向地理編碼（繁體中文）
    try:
        url = (
            "https://nominatim.openstreetmap.org/reverse"
            f"?format=jsonv2&lat={lat}&lon={lon}&accept-language=zh-TW&zoom=16"
        )
        data = fetch_json(url, timeout=6, cache_ttl=300)
        addr = data.get("address", {})
        county = addr.get("city") or addr.get("county") or ""
        township = (
            addr.get("suburb")
            or addr.get("town")
            or addr.get("city_district")
            or addr.get("district")
            or addr.get("village")
            or ""
        )
        road = addr.get("road") or addr.get("neighbourhood") or ""
        if county or township:
            full_place = f"{county}{township}"
            return {
                "status": "ok",
                "source": "OpenStreetMap Nominatim 反向地理編碼",
                "queried_at": queried_at,
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "coords_auto_swapped": swapped,
                "county": county,
                "township": township,
                "road": road,
                "place": full_place,
                "display_name": data.get("display_name", full_place),
            }
    except Exception:
        pass

    # 2. 離線或逾時備援：找最近的台灣鄉鎮中心點
    nearest = min(
        FALLBACK_TOWNSHIPS,
        key=lambda item: haversine_meters(lat, lon, item[2], item[3]),
    )
    dist = haversine_meters(lat, lon, nearest[2], nearest[3])
    county, township = nearest[0], nearest[1]
    return {
        "status": "ok",
        "source": "本機台灣鄉鎮座標備援比對（離線備援）",
        "queried_at": queried_at,
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "coords_auto_swapped": swapped,
        "county": county,
        "township": township,
        "road": "",
        "place": f"{county}{township}",
        "note": f"已依最近行政區中心推估為 {county}{township}（距中心 {format_distance(dist)}）。",
    }


def _describe_osrm_step(step: dict[str, Any]) -> str:
    maneuver = step.get("maneuver", {})
    m_type = maneuver.get("type", "")
    modifier = maneuver.get("modifier", "")
    street = (step.get("name") or "").strip()
    dist = float(step.get("distance") or 0)

    if m_type == "arrive":
        return "抵達避難收容處所"
    action = MODIFIER_ZH.get(modifier) or MANEUVER_ZH.get(m_type, "前進")
    if m_type == "depart":
        action = "出發前往"
    street_part = f"「{street}」" if street else "前方道路"
    return f"{action}{street_part}（{format_distance(dist)}）"


def get_evacuation_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    dest_name: str = "避難收容處所",
    travel_mode: str = "walking",
    blocked_roads: str = "",
) -> dict:
    """規劃從使用者 GPS 位置前往指定避難收容處所的路線指引與一鍵 Google Maps 導航連結。

    Args:
        origin_lat: 使用者目前緯度。
        origin_lon: 使用者目前經度。
        dest_lat: 避難所緯度。
        dest_lon: 避難所經度。
        dest_name: 避難收容處所名稱。
        travel_mode: 移動方式，「walking」（步行，災時最建議）或「driving」（開車）。
        blocked_roads: 選填，已知封閉或積水危險路段（逗號或頓號分隔），系統會交叉比對提醒避開。

    Returns:
        包含路線距離、預估時間、逐步引導、封路警示與 Google Maps 導航網址的字典。
    """
    o_lat, o_lon, _ = normalize_taiwan_coords(float(origin_lat), float(origin_lon))
    d_lat, d_lon, _ = normalize_taiwan_coords(float(dest_lat), float(dest_lon))
    mode = "driving" if travel_mode.lower() in {"driving", "car", "開車"} else "walking"
    osrm_profile = "driving" if mode == "driving" else "foot"

    straight_m = haversine_meters(o_lat, o_lon, d_lat, d_lon)
    bearing = compass_direction(o_lat, o_lon, d_lat, d_lon)
    gmaps_walk = build_google_maps_dir_url(o_lat, o_lon, d_lat, d_lon, "walking")
    gmaps_drive = build_google_maps_dir_url(o_lat, o_lon, d_lat, d_lon, "driving")

    blocked_tokens = [
        tok.strip()
        for tok in blocked_roads.replace("、", ",").replace("；", ",").split(",")
        if len(tok.strip()) >= 2
    ]

    # 嘗試使用公開 OSRM 取得真實街道逐步導引與完整路線折線（供地圖繪製）
    try:
        url = (
            f"https://router.project-osrm.org/route/v1/{osrm_profile}/"
            f"{o_lon:.6f},{o_lat:.6f};{d_lon:.6f},{d_lat:.6f}"
            "?overview=full&geometries=geojson&steps=true"
        )
        res = fetch_json(url, timeout=8, cache_ttl=120)
        routes = res.get("routes") or []
        if routes:
            route = routes[0]
            route_dist_m = float(route.get("distance") or straight_m)
            # 災時步行速度採保守估計（約 3.6 km/h = 1 m/s），避免過度樂觀
            if mode == "walking":
                est_min = max(1, int(round(route_dist_m / 60.0)))
            else:
                est_min = max(1, int(round(float(route.get("duration") or 60) / 60.0)))

            raw_steps = []
            for leg in route.get("legs", []):
                raw_steps.extend(leg.get("steps", []))

            # GeoJSON coordinates 為 [lon, lat]，轉成 Leaflet 用的 [lat, lon]
            geom_coords = route.get("geometry", {}).get("coordinates") or []
            route_coords = [[round(pt[1], 6), round(pt[0], 6)] for pt in geom_coords if len(pt) >= 2]

            steps_zh = [_describe_osrm_step(s) for s in raw_steps[:12]]
            route_street_blob = " ".join((s.get("name") or "") for s in raw_steps)
            warnings = [
                f"⚠️ 注意：初步路線可能行經高風險／封閉路段「{tok}」，現場務必改道繞行，切勿強行通過！"
                for tok in blocked_tokens
                if tok in route_street_blob
            ]
            if blocked_tokens and not warnings:
                warnings.append(
                    f"已比對已知封閉路段（{'、'.join(blocked_tokens)}），初步主線未直接重疊，但現場仍請留意積水與警方交管。"
                )

            return {
                "status": "ok",
                "source": "OSRM 開放路網規劃 + Google Maps 導航連結",
                "destination": dest_name,
                "travel_mode": "步行" if mode == "walking" else "開車",
                "direction_bearing": bearing,
                "route_distance_meters": int(round(route_dist_m)),
                "route_distance_text": format_distance(route_dist_m),
                "estimated_minutes": est_min,
                "steps": steps_zh,
                "blocked_road_warnings": warnings,
                "google_maps_walking_url": gmaps_walk,
                "google_maps_driving_url": gmaps_drive,
                "route_coords": route_coords,
            }
    except Exception:
        pass

    # 備援：直線距離＋方位＋Google Maps 一鍵導航
    est_walk_min = max(1, int(round((straight_m * 1.3) / 60.0)))
    warnings = (
        [f"請務必避開目前通報的封閉或嚴重積水路段：{'、'.join(blocked_tokens)}。"]
        if blocked_tokens
        else []
    )
    return {
        "status": "ok",
        "source": "GPS 方位推估 + Google Maps 導航連結（路網 API 暫時無法連線，已自動切換備援）",
        "destination": dest_name,
        "travel_mode": "步行" if mode == "walking" else "開車",
        "direction_bearing": bearing,
        "straight_distance_meters": int(round(straight_m)),
        "route_distance_text": f"直線{format_distance(straight_m)}（往{bearing}）",
        "estimated_minutes": est_walk_min,
        "steps": [
            f"朝{bearing}前往「{dest_name}」（直線距離 {format_distance(straight_m)}，保守步行約 {est_walk_min} 分鐘）。",
            "請點擊下方 Google Maps 導航連結查看即時街道轉彎指引，並優先選擇地勢較高的主要幹道。",
        ],
        "blocked_road_warnings": warnings,
        "google_maps_walking_url": gmaps_walk,
        "google_maps_driving_url": gmaps_drive,
        "route_coords": [[round(o_lat, 6), round(o_lon, 6)], [round(d_lat, 6), round(d_lon, 6)]],
    }
