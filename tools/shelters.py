"""Taiwan emergency shelter loader & nearest-shelter recommendation tool.

Automatically loads:
1. Any official NFA shelter CSV files placed in `data/` (standard Taiwan open-data schema)
2. Built-in verified Taiwan shelter dataset (`data/shelters_builtin.json`)
"""

import csv
from functools import lru_cache
import json
from typing import Any
from urllib.parse import urlencode

from .common import PROJECT_ROOT, filter_by_place, now_taiwan_iso
from .location import (
    compass_direction,
    format_distance,
    get_evacuation_route,
    haversine_meters,
    normalize_taiwan_coords,
)

DATA_DIR = PROJECT_ROOT / "data"
BUILTIN_SHELTERS_PATH = DATA_DIR / "shelters_builtin.json"


def _parse_float(val: Any) -> float | None:
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _parse_int(val: Any) -> int:
    try:
        return int(float(str(val).strip().replace(",", "")))
    except (ValueError, TypeError):
        return 0


def _normalize_shelter_row(row: dict[str, Any]) -> dict[str, Any] | None:
    name = (
        row.get("避難收容處所名稱")
        or row.get("名稱")
        or row.get("name")
        or ""
    ).strip()
    if not name:
        return None

    lat = _parse_float(row.get("緯度") or row.get("lat") or row.get("Y"))
    lon = _parse_float(row.get("經度") or row.get("lon") or row.get("X"))
    if lat is not None and lon is not None:
        lat, lon, _ = normalize_taiwan_coords(lat, lon)

    area = (row.get("縣市及鄉鎮市區") or row.get("行政區") or "").strip()
    village = (row.get("村里") or "").strip()
    address = (row.get("避難收容處所地址") or row.get("地址") or "").strip()
    disaster_types = (row.get("適用災害類別") or "水災,震災,風災").strip()
    phone = (row.get("管理人電話") or row.get("電話") or "").strip()

    return {
        "編號": str(row.get("序號") or "").strip(),
        "名稱": name,
        "縣市及鄉鎮市區": area,
        "村里": village,
        "地址": address,
        "緯度": lat,
        "經度": lon,
        "預計收容人數": _parse_int(row.get("預計收容人數")),
        "適用災害類別": disaster_types,
        "室內": str(row.get("室內") or "是").strip(),
        "適合避難弱者": str(row.get("適合避難弱者安置") or "是").strip(),
        "管理人電話": phone,
    }


@lru_cache(maxsize=1)
def load_shelters() -> tuple[list[dict[str, Any]], list[str]]:
    """Load shelters from official CSV files in data/ (if present) and built-in JSON."""
    shelters: list[dict[str, Any]] = []
    sources: list[str] = []
    seen: set[tuple[str, str]] = set()

    if DATA_DIR.exists():
        # 1. 優先讀取使用者放入 data/ 的消防署官方 CSV 檔
        for csv_path in sorted(DATA_DIR.glob("*.csv")):
            count_before = len(shelters)
            for encoding in ("utf-8-sig", "utf-8", "big5", "cp950"):
                try:
                    with csv_path.open("r", encoding=encoding, newline="") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            item = _normalize_shelter_row(row)
                            if item:
                                key = (item["名稱"], item["地址"])
                                if key not in seen:
                                    seen.add(key)
                                    shelters.append(item)
                    break
                except UnicodeDecodeError:
                    continue
                except Exception:
                    break
            if len(shelters) > count_before:
                sources.append(f"官方 CSV ({csv_path.name})")

        # 2. 載入內建台灣重點避難收容處所資料
        if BUILTIN_SHELTERS_PATH.exists():
            try:
                raw_list = json.loads(BUILTIN_SHELTERS_PATH.read_text(encoding="utf-8"))
                count_before = len(shelters)
                for row in raw_list:
                    item = _normalize_shelter_row(row)
                    if item:
                        key = (item["名稱"], item["地址"])
                        if key not in seen:
                            seen.add(key)
                            shelters.append(item)
                if len(shelters) > count_before:
                    sources.append("內建台灣避難收容處所點位庫 (data/shelters_builtin.json)")
            except Exception:
                pass

    return shelters, sources


def _matches_disaster_type(shelter: dict[str, Any], disaster_type: str) -> bool:
    if not disaster_type or not disaster_type.strip():
        return True
    dt = disaster_type.strip()
    categories = shelter.get("適用災害類別", "")
    # 同義詞對應
    if any(k in dt for k in ("淹水", "水災", "豪雨", "溢堤", "積水")):
        return "水災" in categories or "風災" in categories
    if any(k in dt for k in ("土石流", "崩塌", "山崩")):
        return "土石流" in categories
    if any(k in dt for k in ("地震", "震災")):
        return "震災" in categories
    if "海嘯" in dt:
        return "海嘯" in categories
    return dt in categories


def find_nearest_shelters(
    latitude: float = 0.0,
    longitude: float = 0.0,
    place: str = "",
    disaster_type: str = "",
    limit: int = 3,
) -> dict:
    """查詢距離使用者最近、且適合當前災害類型的台灣避難收容處所。

    Args:
        latitude: 選填，使用者目前 GPS 緯度（若有提供將精確計算距離與方位）。
        longitude: 選填，使用者目前 GPS 經度。
        place: 選填，縣市或鄉鎮市區名稱（例如「高雄三民區」、「屏東三地門鄉」）。
        disaster_type: 選填，目前災害類型（例如「水災」、「淹水」、「土石流」、「地震」），用來優先篩選適用收容所。
        limit: 回傳筆數上限，預設 3 筆。

    Returns:
        包含推薦避難收容處所清單（含距離、方位、地址、座標、收容人數、電話與 Google Maps 連結）的字典。
    """
    shelters, sources = load_shelters()
    queried_at = now_taiwan_iso()
    limit = max(1, min(int(limit or 3), 10))

    has_gps = bool(latitude and longitude and abs(latitude) > 1 and abs(longitude) > 1)
    user_lat, user_lon = (0.0, 0.0)
    if has_gps:
        user_lat, user_lon, _ = normalize_taiwan_coords(float(latitude), float(longitude))

    candidates = shelters
    if not has_gps and place:
        candidates = filter_by_place(shelters, place)

    # 先嘗試篩選符合災害類別的避難所；若該區沒有標註則保留全部並標註提醒
    disaster_filtered = [s for s in candidates if _matches_disaster_type(s, disaster_type)]
    used_disaster_filter = bool(disaster_type and disaster_filtered)
    pool = disaster_filtered if disaster_filtered else candidates

    results: list[dict[str, Any]] = []
    if has_gps:
        scored = []
        for s in pool:
            s_lat, s_lon = s.get("緯度"), s.get("經度")
            if s_lat is None or s_lon is None:
                continue
            # 排除官方 CSV 中偶見的 0,0 或台灣範圍外異常座標
            if not (21.0 <= s_lat <= 26.8 and 117.5 <= s_lon <= 123.5):
                continue
            dist_m = haversine_meters(user_lat, user_lon, s_lat, s_lon)
            bearing = compass_direction(user_lat, user_lon, s_lat, s_lon)
            scored.append((dist_m, bearing, s))

        scored.sort(key=lambda item: item[0])
        for dist_m, bearing, s in scored[:limit]:
            nav_url = "https://www.google.com/maps/dir/?" + urlencode(
                {
                    "api": "1",
                    "origin": f"{user_lat:.6f},{user_lon:.6f}",
                    "destination": f"{s['緯度']:.6f},{s['經度']:.6f}",
                    "travelmode": "walking",
                }
            )
            results.append(
                {
                    **s,
                    "直線距離_公尺": int(round(dist_m)),
                    "距離與方位": f"往{bearing} {format_distance(dist_m)}",
                    "google_maps_導航連結": nav_url,
                }
            )
    else:
        for s in pool[:limit]:
            map_url = (
                f"https://www.google.com/maps/search/?api=1&query={s['緯度']},{s['經度']}"
                if s.get("緯度") and s.get("經度")
                else ""
            )
            results.append({**s, "google_maps_地圖連結": map_url})

    if not results:
        return {
            "status": "ok",
            "source": "、".join(sources) or "避難收容處所資料庫",
            "queried_at": queried_at,
            "count": 0,
            "message": (
                f"目前資料庫中查無「{place}」的收容所點位。"
                "建議請使用者提供 GPS 經緯度，或至消防署「消防防災e點通」查詢全國完整收容處所，"
                "亦可將政府資料開放平台的「避難收容處所點位檔案.csv」放入 data/ 資料夾以涵蓋全台。"
            ),
            "data": [],
        }

    return {
        "status": "ok",
        "source": "、".join(sources),
        "queried_at": queried_at,
        "search_mode": "gps" if has_gps else "place_name",
        "disaster_filter_applied": disaster_type if used_disaster_filter else "無（顯示所有收容所）",
        "count": len(results),
        "data": results,
    }


def find_shelters_and_route(
    latitude: float = 0.0,
    longitude: float = 0.0,
    place: str = "",
    disaster_type: str = "",
    blocked_roads: str = "",
    limit: int = 3,
) -> dict:
    """一次查詢最近的合適避難收容處所，並直接規劃前往第 1 名最近避難所的步行路線與 Google Maps 導航連結。

    Args:
        latitude: 選填，使用者目前 GPS 緯度。
        longitude: 選填，使用者目前 GPS 經度。
        place: 選填，行政區名稱（如「高雄市三民區」）。
        disaster_type: 選填，主要災害類型（如「水災」、「土石流」、「地震」）。
        blocked_roads: 選填，已知封閉或積水路段（逗號或頓號分隔）。
        limit: 回傳避難所筆數上限，預設 3 筆。

    Returns:
        包含推薦避難所清單與第 1 名避難所詳細路線導引的字典。
    """
    shelters_res = find_nearest_shelters(
        latitude=latitude,
        longitude=longitude,
        place=place,
        disaster_type=disaster_type,
        limit=limit,
    )
    route_res = None
    has_gps = bool(latitude and longitude and abs(latitude) > 1 and abs(longitude) > 1)
    top_list = shelters_res.get("data") or []
    if has_gps and top_list:
        top = top_list[0]
        if top.get("緯度") is not None and top.get("經度") is not None:
            raw_route = get_evacuation_route(
                origin_lat=latitude,
                origin_lon=longitude,
                dest_lat=top["緯度"],
                dest_lon=top["經度"],
                dest_name=top["名稱"],
                travel_mode="walking",
                blocked_roads=blocked_roads,
            )
            # 移除大量座標點位陣列避免浪費 LLM token（地圖前端直接呼叫 get_evacuation_route 取得折線）
            route_res = {k: v for k, v in raw_route.items() if k != "route_coords"}

    return {
        "status": "ok",
        "shelters": shelters_res,
        "primary_route": route_res,
    }
