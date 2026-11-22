"""Taiwan official disaster open-data API tools with Historical Scenario Replay support.

Data sources (from WATCH platform list):
- WRA (水利署): Flood warnings & real-time river water levels (no API key required)
- NCDR (國家災害防救科技中心): CAP alerts for flood, debris flow, river level, road (API key optional)
- CWA (中央氣象署): Automatic rainfall stations (free CWA_API_KEY required in live mode)

When a historical scenario is active (via in-chat `set_disaster_scenario` or `.env`
`DISASTER_SCENARIO`), all 4 tools seamlessly serve the snapshot data for that timestamp
and flag `mode: "simulation"` so the agent always discloses drill mode.
"""

from datetime import timedelta
from typing import Any, Optional
from urllib.parse import quote
import xml.etree.ElementTree as ET

from .common import (
    fetch_bytes,
    fetch_json,
    filter_by_place,
    find_list,
    get_env,
    now_taiwan,
    now_taiwan_iso,
    parse_iso_datetime,
    truncate_data,
)
from .location import haversine_meters, resolve_gps_location
from .scenarios import get_active_scenario
from .weather import get_outdoor_weather_tips

try:
    from google.adk.tools import ToolContext
except ImportError:  # pragma: no cover
    ToolContext = Any  # type: ignore[misc, assignment]

NCDR_ALERT_TYPES: dict[str, int] = {
    "淹水": 8,
    "土石流": 9,
    "河川水位": 11,
    "道路": 13,
}

# 水利署河川水位 WarningLevel 代碼對應
WRA_WARNING_LABELS: dict[int, str] = {
    1: "一級警戒",
    2: "二級警戒",
    3: "三級警戒",
    10: "正常",
    0: "無警戒基準",
}

# NCDR Atom feed 會保留已過期的歷史示警，即時模式預設過濾超過 48 小時的舊訊息
NCDR_LIVE_MAX_AGE_HOURS = 48


def _simulation_meta(scenario: dict[str, Any], base_source: str) -> dict[str, Any]:
    return {
        "mode": "simulation",
        "scenario_id": scenario["scenario_id"],
        "scenario_title": scenario.get("title", scenario["scenario_id"]),
        "simulated_time": scenario.get("simulated_time", ""),
        "source": f"[歷史演練快照] {base_source}",
        "queried_at": scenario.get("simulated_time", now_taiwan_iso()),
    }


def _normalize_river_station(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract compact, readable fields from verbose WRA river station JSON."""
    station_name = raw.get("StationName")
    if isinstance(station_name, dict):
        station_name = station_name.get("zh_TW") or station_name.get("En") or ""

    city = raw.get("City")
    if isinstance(city, dict):
        city = city.get("zh_TW") or city.get("En") or ""

    basin = raw.get("Basin")
    if isinstance(basin, dict):
        basin = basin.get("zh_TW") or basin.get("En") or ""

    raw_level = raw.get("WarningLevel")
    try:
        level_code = int(raw_level) if raw_level is not None else -1
    except (ValueError, TypeError):
        level_code = -1

    return {
        "測站編號": raw.get("StationNo", ""),
        "測站名稱": station_name or "",
        "縣市": city or "",
        "流域": basin or "",
        "即時水位_m": raw.get("WaterLevel"),
        "堤頂高度_m": raw.get("TopLevel"),
        "警戒等級代碼": level_code,
        "警戒狀態": WRA_WARNING_LABELS.get(level_code, f"代碼 {level_code}"),
        "觀測時間": raw.get("Time", ""),
    }


def _river_sort_key(station: dict[str, Any]) -> tuple[int, str]:
    """Sort stations with active warnings (1 -> 2 -> 3) first, then normal (10)."""
    code = station.get("警戒等級代碼", 99)
    priority = code if code in (1, 2, 3) else 50
    return (priority, str(station.get("測站名稱", "")))


def get_flood_warnings(
    place: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢水利署目前的淹水警戒（依雨量判定）。

    Args:
        place: 縣市或鄉鎮市區名稱，例如「新北市」、「汐止」、「高雄三民區」或「屏東東港」。

    Returns:
        包含查詢模式、資料來源、查詢時間與警戒資料的字典。
    """
    base_source = "經濟部水利署 淹水警戒 API (fhyv.wra.gov.tw)"
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        items = scenario.get("flood_warnings", [])
        hits = filter_by_place(items, place)
        meta = _simulation_meta(scenario, base_source)
        if not hits:
            return {
                "status": "ok",
                **meta,
                "count": 0,
                "message": f"在此演練時間點，水利署沒有「{place}」的淹水警戒資料。",
                "data": [],
            }
        return {
            "status": "ok",
            **meta,
            "count": len(hits),
            "data": truncate_data(hits),
        }

    queried_at = now_taiwan_iso()
    try:
        url = (
            "https://fhyv.wra.gov.tw/FhyWeb/v1/Api/Rainfall/Warning/City/"
            + quote("全部")
            + "?$format=JSON"
        )
        items = find_list(fetch_json(url)) or []
        hits = filter_by_place(items, place)
        if not hits:
            return {
                "status": "ok",
                "mode": "live",
                "source": base_source,
                "queried_at": queried_at,
                "count": 0,
                "message": f"水利署目前沒有「{place}」的淹水警戒資料（通常代表目前無淹水警戒）。",
                "data": [],
            }
        return {
            "status": "ok",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "count": len(hits),
            "data": truncate_data(hits),
        }
    except Exception as e:
        return {
            "status": "error",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "error": f"查詢水利署淹水警戒失敗：{e}",
        }


def get_river_water_level(
    place: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢水利署河川水位測站的即時水位與警戒狀況（有警戒的測站會優先排在前面）。

    Args:
        place: 縣市、鄉鎮或河川/測站名稱，例如「新北市」、「基隆河」、「高雄市」或「愛河」。

    Returns:
        包含查詢模式、資料來源、查詢時間與精簡後的測站即時水位資料的字典。
    """
    base_source = "經濟部水利署 河川水位即時資料 API (fhyv.wra.gov.tw)"
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        items = scenario.get("river_water_level", [])
        hits = filter_by_place(items, place)
        hits.sort(key=_river_sort_key)
        meta = _simulation_meta(scenario, base_source)
        if not hits:
            return {
                "status": "ok",
                **meta,
                "count": 0,
                "message": f"在此演練時間點，找不到「{place}」的河川水位測站資料。",
                "data": [],
            }
        return {
            "status": "ok",
            **meta,
            "count": len(hits),
            "returned": min(len(hits), 20),
            "data": truncate_data(hits[:20]),
        }

    queried_at = now_taiwan_iso()
    try:
        url = (
            "https://fhyv.wra.gov.tw/FhyWeb/v1/Api/WaterLevel/RealTimeInfo/City/"
            + quote("全部")
            + "?$format=JSON"
        )
        raw_items = find_list(fetch_json(url)) or []
        raw_hits = filter_by_place(raw_items, place)
        if not raw_hits:
            return {
                "status": "ok",
                "mode": "live",
                "source": base_source,
                "queried_at": queried_at,
                "count": 0,
                "message": f"找不到「{place}」的河川水位測站資料。",
                "data": [],
            }

        normalized = [_normalize_river_station(st) for st in raw_hits]
        normalized.sort(key=_river_sort_key)
        warning_count = sum(1 for st in normalized if st["警戒等級代碼"] in (1, 2, 3))
        return {
            "status": "ok",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "count": len(normalized),
            "warning_station_count": warning_count,
            "returned": min(len(normalized), 20),
            "data": truncate_data(normalized[:20]),
        }
    except Exception as e:
        return {
            "status": "error",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "error": f"查詢河川水位失敗：{e}",
        }


def get_ncdr_alerts(
    alert_kind: str,
    place: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢 NCDR 防災示警平台目前生效的 CAP 警戒（即時模式會自動濾除超過 48 小時的過期舊示警）。

    Args:
        alert_kind: 警戒類型，只能是「淹水」、「土石流」、「河川水位」、「道路」之一。
        place: 選填，用來篩選縣市或鄉鎮市區名稱（例如「高雄市」、「三民區」、「屏東」）。留空則回傳全台生效警戒。

    Returns:
        包含查詢模式、資料來源、查詢時間與生效示警列表的字典。
    """
    base_source = f"NCDR 民生示警公開資料平台 ({alert_kind} CAP)"
    if alert_kind not in NCDR_ALERT_TYPES:
        return {
            "status": "error",
            "source": base_source,
            "queried_at": now_taiwan_iso(),
            "error": f"alert_kind 參數無效，只能是：{'、'.join(NCDR_ALERT_TYPES)}",
        }

    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        all_alerts = scenario.get("ncdr_alerts", {}).get(alert_kind, [])
        entries = filter_by_place(all_alerts, place) if place else list(all_alerts)
        meta = _simulation_meta(scenario, base_source)
        if not entries:
            scope = f"「{place}」的" if place else ""
            return {
                "status": "ok",
                **meta,
                "count": 0,
                "message": f"在此演練時間點，NCDR 沒有{scope}{alert_kind}警戒。",
                "data": [],
            }
        return {
            "status": "ok",
            **meta,
            "count": len(entries),
            "data": truncate_data(entries),
        }

    queried_at = now_taiwan_iso()
    try:
        url = (
            "https://alerts.ncdr.nat.gov.tw/webapi/RssAtomFeed.ashx"
            f"?AlertType={NCDR_ALERT_TYPES[alert_kind]}"
        )
        ncdr_api_key = get_env("NCDR_API_KEY")
        if ncdr_api_key:
            url += f"&apikey={ncdr_api_key}"

        root = ET.fromstring(fetch_bytes(url))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        cutoff = now_taiwan() - timedelta(hours=NCDR_LIVE_MAX_AGE_HOURS)

        active_entries = []
        filtered_expired_count = 0
        for e in root.findall("a:entry", ns):
            updated_str = (e.findtext("a:updated", "", ns) or "").strip()
            updated_dt = parse_iso_datetime(updated_str)
            if updated_dt is not None and updated_dt < cutoff:
                filtered_expired_count += 1
                continue
            active_entries.append(
                {
                    "標題": (e.findtext("a:title", "", ns) or "").strip(),
                    "內容": (e.findtext("a:summary", "", ns) or "").strip(),
                    "更新時間": updated_str,
                }
            )

        if place:
            active_entries = filter_by_place(active_entries, place)

        if not active_entries:
            scope = f"「{place}」的" if place else ""
            return {
                "status": "ok",
                "mode": "live",
                "source": base_source,
                "queried_at": queried_at,
                "count": 0,
                "filtered_expired_count": filtered_expired_count,
                "message": (
                    f"NCDR 目前（近 {NCDR_LIVE_MAX_AGE_HOURS} 小時內）沒有{scope}{alert_kind}生效警戒"
                    + (
                        f"（已自動略過 {filtered_expired_count} 筆過期舊示警）。"
                        if filtered_expired_count
                        else "。"
                    )
                ),
                "data": [],
            }
        return {
            "status": "ok",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "count": len(active_entries),
            "filtered_expired_count": filtered_expired_count,
            "data": truncate_data(active_entries),
        }
    except Exception as e:
        return {
            "status": "error",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "error": f"查詢 NCDR {alert_kind}警戒失敗：{e}（若持續出現 429 限流，建議在 .env 設定免費的 NCDR_API_KEY）",
        }


def get_rainfall(
    county: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢中央氣象署自動雨量站的即時雨量（含 10 分鐘、1 小時、24 小時累積雨量）。

    Args:
        county: 縣市或鄉鎮名稱，例如「臺北市」、「新北市」、「高雄三民區」或「屏東東港」。

    Returns:
        包含查詢模式、資料來源、查詢時間與雨量站觀測資料的字典。
    """
    base_source = "中央氣象署 自動雨量站 O-A0002-001 (opendata.cwa.gov.tw)"
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        items = scenario.get("rainfall", [])
        hits = filter_by_place(items, county)
        meta = _simulation_meta(scenario, base_source)
        if not hits:
            return {
                "status": "ok",
                **meta,
                "count": 0,
                "message": f"在此演練時間點，找不到「{county}」的雨量站資料。",
                "data": [],
            }
        return {
            "status": "ok",
            **meta,
            "count": len(hits),
            "data": truncate_data(hits[:20]),
        }

    queried_at = now_taiwan_iso()
    cwa_api_key = get_env("CWA_API_KEY")
    if not cwa_api_key:
        return {
            "status": "unavailable",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "message": "尚未設定氣象署金鑰 CWA_API_KEY，目前無法查詢即時雨量（可在 opendata.cwa.gov.tw 免費申請後填入 .env）。",
        }
    try:
        url = (
            "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-001"
            f"?Authorization={cwa_api_key}&format=JSON"
        )
        items = find_list(fetch_json(url).get("records", {})) or []
        hits = filter_by_place(items, county)
        if not hits:
            return {
                "status": "ok",
                "mode": "live",
                "source": base_source,
                "queried_at": queried_at,
                "count": 0,
                "message": f"找不到「{county}」的雨量站資料。",
                "data": [],
            }
        return {
            "status": "ok",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "count": len(hits),
            "returned": min(len(hits), 20),
            "data": truncate_data(hits[:20]),
        }
    except Exception as e:
        return {
            "status": "error",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "error": f"查詢氣象署雨量失敗：{e}",
        }


def get_landslide_alerts(
    place: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢農業部農村水保署的大崩塌與土石流警戒（免金鑰）。

    Args:
        place: 選填，縣市或鄉鎮市區名稱（例如「高雄市」、「茂林區」、「屏東縣」）。留空查全台。

    Returns:
        包含查詢模式、資料來源、查詢時間與大崩塌／土石流警戒資料的字典。
    """
    base_source = "農業部農村水保署 大崩塌／土石流警戒 API (data.moa.gov.tw)"
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        items = scenario.get("landslide_alerts", [])
        hits = filter_by_place(items, place) if place else list(items)
        meta = _simulation_meta(scenario, base_source)
        if not hits:
            return {
                "status": "ok",
                **meta,
                "count": 0,
                "message": f"在此演練時間點，農村水保署沒有「{place or '全台'}」的大崩塌／土石流警戒。",
                "data": [],
            }
        return {"status": "ok", **meta, "count": len(hits), "data": truncate_data(hits)}

    queried_at = now_taiwan_iso()
    try:
        url = "https://data.moa.gov.tw/Service/OpenData/TransService.aspx?UnitId=ofvJwoWC5wum"
        raw_items = find_list(fetch_json(url)) or []
        active = [
            it
            for it in raw_items
            if isinstance(it, dict)
            and str(it.get("LandslideName", "")).strip() != "目前無警戒"
            and str(it.get("AlertLevel", "-")).strip() not in ("", "-")
        ]
        hits = filter_by_place(active, place) if place else active
        if not hits:
            return {
                "status": "ok",
                "mode": "live",
                "source": base_source,
                "queried_at": queried_at,
                "count": 0,
                "message": f"農村水保署目前沒有「{place or '全台'}」的大崩塌或土石流警戒。",
                "data": [],
            }
        return {
            "status": "ok",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "count": len(hits),
            "data": truncate_data(hits[:30]),
        }
    except Exception as e:
        return {
            "status": "error",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "error": f"查詢農村水保署大崩塌／土石流警戒失敗：{e}",
        }


def get_reservoir_warnings(
    place: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢經濟部水利署的水庫放流與洩洪警戒（免金鑰）。

    Args:
        place: 選填，縣市、鄉鎮或水庫名稱。留空回傳全台放流警戒水庫。

    Returns:
        包含查詢模式、資料來源、查詢時間與水庫放流警戒資訊的字典。
    """
    base_source = "經濟部水利署 水庫放流警戒 API (fhyv.wra.gov.tw)"
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        items = scenario.get("reservoir_warnings", [])
        hits = filter_by_place(items, place) if place else list(items)
        meta = _simulation_meta(scenario, base_source)
        if not hits:
            return {
                "status": "ok",
                **meta,
                "count": 0,
                "message": f"在此演練時間點，沒有影響「{place or '全台'}」的水庫放流警戒。",
                "data": [],
            }
        return {"status": "ok", **meta, "count": len(hits), "data": truncate_data(hits)}

    queried_at = now_taiwan_iso()
    try:
        url = "https://fhyv.wra.gov.tw/FhyWeb/v1/Api/Map/Reservoir/SumInfo?$format=JSON"
        raw = fetch_json(url)
        warnings: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict) and isinstance(entry.get("Warning"), list):
                    for w in entry["Warning"]:
                        if isinstance(w, dict) and w.get("Name"):
                            warnings.append({"水庫名稱": w["Name"], "警戒狀態": "放流／警戒中"})
        hits = filter_by_place(warnings, place) if place else warnings
        if not hits:
            note = (
                f"（全台目前有 {len(warnings)} 座水庫放流警戒：{'、'.join(w['水庫名稱'] for w in warnings[:5])}）"
                if (place and warnings)
                else ""
            )
            return {
                "status": "ok",
                "mode": "live",
                "source": base_source,
                "queried_at": queried_at,
                "count": 0,
                "all_taiwan_warning_count": len(warnings),
                "message": f"水利署目前沒有直接標註「{place or '全台'}」的水庫放流警戒。{note}",
                "data": [],
            }
        return {
            "status": "ok",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "count": len(hits),
            "data": truncate_data(hits),
        }
    except Exception as e:
        return {
            "status": "error",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "error": f"查詢水利署水庫放流警戒失敗：{e}",
        }


def get_iot_flood_sensors(
    place: str = "",
    latitude: float = 0.0,
    longitude: float = 0.0,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢民生公共物聯網 IoT 路面淹水感測器（實際積水公分數，免金鑰）。

    Args:
        place: 選填，縣市或鄉鎮市區名稱。
        latitude: 選填，使用者 GPS 緯度（若有提供會優先標註距離並排序）。
        longitude: 選填，使用者 GPS 經度。

    Returns:
        包含查詢模式、資料來源與附近積淹水感測點位（含經緯度與水深公分）的字典。
    """
    base_source = "民生公共物聯網 水利署 IoT 淹水感測器 (sta.colife.org.tw)"
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        items = list(scenario.get("iot_flood_sensors", []))
        hits = filter_by_place(items, place) if place else items
        if latitude and longitude and hits:
            enriched = []
            for it in hits:
                s_lat, s_lon = it.get("緯度"), it.get("經度")
                dist = (
                    int(round(haversine_meters(latitude, longitude, s_lat, s_lon)))
                    if s_lat and s_lon
                    else 999999
                )
                enriched.append({**it, "距使用者_公尺": dist})
            enriched.sort(key=lambda x: x["距使用者_公尺"])
            hits = enriched
        meta = _simulation_meta(scenario, base_source)
        if not hits:
            return {
                "status": "ok",
                **meta,
                "count": 0,
                "message": f"在此演練時間點，「{place or '附近'}」沒有 IoT 淹水感測器異常積水回報。",
                "data": [],
            }
        return {"status": "ok", **meta, "count": len(hits), "data": truncate_data(hits)}

    queried_at = now_taiwan_iso()
    try:
        url = (
            "https://sta.colife.org.tw/STA_WaterResource_v2/v1.0/Datastreams"
            "?$top=200&$expand=Thing,Thing/Locations,Observations($orderby=phenomenonTime%20desc;$top=1)"
            "&$filter=substringof('%E6%B7%B9%E6%B0%B4%E6%B7%B1%E5%BA%A6',description)"
        )
        raw = fetch_json(url, timeout=10, cache_ttl=60)
        values = raw.get("value", []) if isinstance(raw, dict) else []
        parsed: list[dict[str, Any]] = []
        for ds in values:
            thing = ds.get("Thing") or {}
            props = thing.get("properties") or {}
            obs_list = ds.get("Observations") or []
            depth = obs_list[0].get("result") if obs_list else None
            obs_time = obs_list[0].get("phenomenonTime", "") if obs_list else ""
            locs = thing.get("Locations") or []
            coords = (
                locs[0].get("location", {}).get("coordinates", [])
                if locs and isinstance(locs[0], dict)
                else []
            )
            lon = coords[0] if len(coords) >= 2 else None
            lat = coords[1] if len(coords) >= 2 else None
            # 僅回報有積水（> 0 cm）或指定地區之測站
            entry = {
                "測站名稱": props.get("stationName") or thing.get("name") or "",
                "縣市": props.get("city") or "",
                "鄉鎮": props.get("town") or "",
                "緯度": lat,
                "經度": lon,
                "淹水深度_cm": depth,
                "觀測時間": obs_time,
            }
            if isinstance(depth, (int, float)) and depth > 0:
                parsed.append(entry)

        hits = filter_by_place(parsed, place) if place else parsed
        return {
            "status": "ok",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "count": len(hits),
            "message": (
                f"IoT 淹水感測器目前查無「{place or '全台'}」大於 0 公分之積水回報。"
                if not hits
                else ""
            ),
            "data": truncate_data(hits[:20]),
        }
    except Exception as e:
        return {
            "status": "unavailable",
            "mode": "live",
            "source": base_source,
            "queried_at": queried_at,
            "message": f"民生公共物聯網 IoT 淹水感測器目前無法連線（上游憑證或服務異常：{e}）。",
            "data": [],
        }


def collect_location_hazards(
    place: str = "",
    latitude: float = 0.0,
    longitude: float = 0.0,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """一次完成 GPS 反向定位與所有台灣官方災害資料查詢（淹水、IoT感測器、河川水位、水庫放流、大崩塌/土石流、道路封閉、雨量）。

    Args:
        place: 選填，縣市或鄉鎮市區名稱（例如「高雄三民區」、「新北汐止」）。
        latitude: 選填，使用者 GPS 緯度（若有提供會優先反查精確行政區與路名）。
        longitude: 選填，使用者 GPS 經度。

    Returns:
        整合 GPS 定位結果與全部官方災害監測結果的完整字典。
    """
    gps_info = None
    query_place = place.strip()

    if latitude and longitude and abs(latitude) > 1 and abs(longitude) > 1:
        gps_info = resolve_gps_location(latitude, longitude)
        if gps_info.get("status") == "ok" and gps_info.get("place"):
            query_place = gps_info["place"]

    if not query_place:
        return {
            "status": "error",
            "error": "請提供地點名稱（place）或有效的 GPS 經緯度（latitude, longitude）。",
        }

    scenario = get_active_scenario(tool_context)
    bundle = {
        "status": "ok",
        "mode": "simulation" if scenario is not None else "live",
        "scenario_id": scenario["scenario_id"] if scenario else None,
        "scenario_title": scenario.get("title") if scenario else None,
        "simulated_time": scenario.get("simulated_time") if scenario else None,
        "queried_at": scenario.get("simulated_time") if scenario else now_taiwan_iso(),
        "query_place": query_place,
        "gps_location": gps_info,
        "flood_warnings": get_flood_warnings(query_place, tool_context=tool_context),
        "iot_flood_sensors": get_iot_flood_sensors(
            query_place, latitude=latitude, longitude=longitude, tool_context=tool_context
        ),
        "river_water_level": get_river_water_level(query_place, tool_context=tool_context),
        "reservoir_warnings": get_reservoir_warnings(query_place, tool_context=tool_context),
        "landslide_alerts": get_landslide_alerts(query_place, tool_context=tool_context),
        "ncdr_flood": get_ncdr_alerts("淹水", query_place, tool_context=tool_context),
        "ncdr_debris_flow": get_ncdr_alerts("土石流", query_place, tool_context=tool_context),
        "ncdr_river": get_ncdr_alerts("河川水位", query_place, tool_context=tool_context),
        "ncdr_road": get_ncdr_alerts("道路", query_place, tool_context=tool_context),
        "rainfall": get_rainfall(query_place, tool_context=tool_context),
        "outdoor_weather": get_outdoor_weather_tips(
            latitude=latitude, longitude=longitude, place=query_place, tool_context=tool_context
        ),
    }
    if tool_context is not None and hasattr(tool_context, "state"):
        tool_context.state["last_hazard_bundle"] = bundle
    return bundle
