"""Real-time local weather & daily outdoor/clothing tips (no API key required).

Primary source:
- Taiwan CWA Official Weather Stations on AWS S3 (`O-A0003-001.xml`, from WATCH list row A8)
  363 official Taiwan stations, zero API key, zero 429 rate-limiting, works cleanly on Render overseas IPs!
Fallback source:
- Open-Meteo public forecast API (when S3 is unreachable).
"""

from collections import defaultdict
from datetime import datetime, timedelta
import math
from typing import Any, Optional
import xml.etree.ElementTree as ET

import requests

from .common import TAIWAN_TZ, fetch_bytes, fetch_json, now_taiwan_iso, parse_iso_datetime
from .location import FALLBACK_TOWNSHIPS, format_distance, haversine_meters, normalize_taiwan_coords
from .scenarios import get_active_scenario

try:
    from google.adk.tools import ToolContext
except ImportError:  # pragma: no cover
    ToolContext = Any  # type: ignore[misc, assignment]

CWA_S3_WEATHER_URL = "https://cwaopendata.s3.ap-northeast-1.amazonaws.com/Observation/O-A0003-001.xml"

WMO_WEATHER_ZH: dict[int, tuple[str, str]] = {
    0: ("☀️", "晴朗"),
    1: ("🌤️", "大致晴朗"),
    2: ("⛅", "多雲時晴"),
    3: ("☁️", "陰天多雲"),
    45: ("🌫️", "有霧"),
    48: ("🌫️", "濃霧"),
    51: ("🌦️", "毛毛細雨"),
    53: ("🌦️", "細雨"),
    55: ("🌧️", "較密毛毛雨"),
    61: ("🌧️", "小雨"),
    63: ("🌧️", "中雨"),
    65: ("🌧️", "大雨"),
    80: ("🌦️", "短暫陣雨"),
    81: ("🌧️", "陣雨"),
    82: ("⛈️", "強陣雨"),
    95: ("⛈️", "雷陣雨"),
    96: ("⛈️", "雷雨伴隨冰雹"),
    99: ("⛈️", "劇烈雷雨"),
}


def _safe_float(val: str | None) -> float | None:
    if not val:
        return None
    try:
        num = float(val.strip())
        # 氣象署缺值常標為 -99 或 -999
        if num <= -90:
            return None
        return num
    except (ValueError, TypeError):
        return None


def _estimate_apparent_temp(temp_c: float | None, humidity: float | None, wind_ms: float | None) -> float | None:
    """Australian Bureau of Meteorology apparent temperature formula."""
    if temp_c is None:
        return None
    rh = humidity if humidity is not None else 65.0
    ws = max(0.0, wind_ms if wind_ms is not None else 1.5)
    vapor = (rh / 100.0) * 6.105 * math.exp((17.27 * temp_c) / (237.7 + temp_c))
    return round(temp_c + 0.33 * vapor - 0.70 * ws - 4.00, 1)


def _icon_for_cwa_weather(cond: str, rain_mm: float) -> str:
    if "雷" in cond:
        return "⛈️"
    if "大雨" in cond or "豪雨" in cond or rain_mm >= 5.0:
        return "🌧️"
    if "雨" in cond or rain_mm > 0:
        return "🌦️"
    if "霧" in cond or "靄" in cond:
        return "🌫️"
    if "晴" in cond and "雲" not in cond and "陰" not in cond:
        return "☀️"
    if "晴" in cond:
        return "🌤️"
    if "陰" in cond:
        return "☁️"
    return "⛅"


def _clothing_and_outdoor_tips(
    temp: float | None,
    apparent: float | None,
    rain_mm: float,
    rain_prob: int | None,
    wind_kmh: float | None,
    gust_kmh: float | None,
    cond_text: str,
) -> list[str]:
    tips: list[str] = []
    feels = apparent if apparent is not None else temp

    # 1. 降雨與雨具提醒
    if "雷" in cond_text:
        tips.append("⛈️ 有雷雨發展，外出請帶雨具並避免停留空曠處、大樹下或溪邊。")
    elif rain_mm >= 5.0 or "大雨" in cond_text:
        tips.append(f"🌧️ 目前雨勢明顯（累積 {rain_mm:.1f} mm），務必帶傘或穿雨衣、防滑防水鞋，騎車放慢注意積水打滑。")
    elif rain_mm > 0 or "雨" in cond_text or (rain_prob is not None and rain_prob >= 50):
        extra = f"（降雨機率約 {rain_prob}%）" if rain_prob is not None else (f"（目前雨量 {rain_mm:.1f} mm）" if rain_mm > 0 else "")
        tips.append(f"☂️ 有下雨或降雨機會{extra}，出門建議隨身帶把折疊傘或輕便雨衣。")
    elif rain_prob is not None and rain_prob >= 30:
        tips.append(f"🌦️ 未來幾小時有局部短暫雨機會（約 {rain_prob}%），長時間外出可備輕便傘。")

    # 2. 氣溫與具體穿著建議
    if feels is not None:
        if feels <= 13.0:
            tips.append(f"🥶 天氣寒冷（體感約 {feels:.1f}°C）：建議穿發熱衣＋毛衣＋羽絨或厚防風外套，配戴圍巾保暖，長輩注意心血管保暖。")
        elif feels <= 18.0:
            tips.append(f"🧥 天氣偏冷（體感約 {feels:.1f}°C）：建議採洋蔥式穿搭（長袖上衣＋防風外套或針織衫），早晚騎車記得加擋風外衣。")
        elif feels <= 22.5:
            tips.append(f"🧣 氣溫微涼舒適（體感約 {feels:.1f}°C）：白天穿長袖或短袖配薄外套最剛好，早晚轉涼建議隨身帶一件輕薄外套。")
        elif feels <= 27.5:
            tips.append(f"👕 氣溫舒適溫暖（體感約 {feels:.1f}°C）：穿透氣短袖或輕薄長袖即可，室內冷氣房可備一件薄襯衫。")
        elif feels <= 32.5:
            tips.append(f"☀️ 天氣偏熱（體感約 {feels:.1f}°C）：建議穿淺色透氣短袖、做好防曬並適時補充水分。")
        else:
            tips.append(f"🥵 高溫炎熱（體感約 {feels:.1f}°C）：穿寬鬆排汗衣物、戴帽防曬並頻繁補水，避免正午烈日下劇烈運動以防中暑。")

    # 3. 風勢提醒
    peak_wind = max(wind_kmh or 0.0, gust_kmh or 0.0)
    if peak_wind >= 40.0:
        tips.append(f"💨 風勢偏強（約 {peak_wind:.0f} km/h），騎車請留意側風，空曠處與海邊注意強陣風。")
    elif peak_wind >= 25.0:
        tips.append(f"🍃 稍微有風（約 {peak_wind:.0f} km/h），體感會略涼一些，騎車建議加防風外套。")

    # 4. 濃霧提醒
    if "霧" in cond_text:
        tips.append("🌫️ 局部有霧能見度較差，行車請開霧燈並保持安全車距。")

    if not tips:
        tips.append("✨ 目前天氣穩定、無防災警戒，適合正常戶外活動。")
    return tips


def _fetch_cwa_s3_weather(lat: float, lon: float, place: str) -> dict[str, Any] | None:
    """Fetch nearest official Taiwan CWA weather station from public AWS S3 XML."""
    raw = fetch_bytes(CWA_S3_WEATHER_URL, timeout=10, cache_ttl=600)
    root = ET.fromstring(raw)
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

    best_dist = float("inf")
    best_st: dict[str, Any] | None = None

    for st in root.findall(".//Station"):
        s_lat, s_lon = None, None
        for coord in st.findall("./GeoInfo/Coordinates"):
            cname = (coord.findtext("CoordinateName") or "").strip().upper()
            clat = _safe_float(coord.findtext("StationLatitude"))
            clon = _safe_float(coord.findtext("StationLongitude"))
            if clat is not None and clon is not None:
                if cname == "WGS84":
                    s_lat, s_lon = clat, clon
                    break
                if s_lat is None:
                    s_lat, s_lon = clat, clon
        if s_lat is None or s_lon is None:
            continue

        we = st.find("WeatherElement")
        if we is None:
            continue
        temp = _safe_float(we.findtext("AirTemperature"))
        if temp is None:
            continue

        dist = haversine_meters(lat, lon, s_lat, s_lon)
        if dist < best_dist:
            best_dist = dist
            cond = (we.findtext("Weather") or "多雲").strip()
            if cond in ("-99", "-", ""):
                cond = "多雲"
            rh = _safe_float(we.findtext("RelativeHumidity"))
            ws_ms = _safe_float(we.findtext("WindSpeed"))
            gust_ms = _safe_float(we.findtext("./GustInfo/PeakGustSpeed"))
            rain_mm = _safe_float(we.findtext("./Now/Precipitation")) or 0.0
            best_st = {
                "station_name": (st.findtext("StationName") or "").strip(),
                "county": (st.findtext("./GeoInfo/CountyName") or "").strip(),
                "town": (st.findtext("./GeoInfo/TownName") or "").strip(),
                "obs_time": (st.findtext("./ObsTime/DateTime") or "").strip(),
                "temp": temp,
                "rh": rh,
                "ws_ms": ws_ms,
                "gust_ms": gust_ms,
                "rain_mm": rain_mm,
                "cond": cond,
                "dist_m": dist,
            }

    if not best_st:
        return None

    temp = best_st["temp"]
    rh = best_st["rh"]
    ws_kmh = round(best_st["ws_ms"] * 3.6, 1) if best_st["ws_ms"] is not None else None
    gust_kmh = round(best_st["gust_ms"] * 3.6, 1) if best_st["gust_ms"] is not None else None
    apparent = _estimate_apparent_temp(temp, rh, best_st["ws_ms"])
    cond = best_st["cond"]
    rain_mm = best_st["rain_mm"]
    icon = _icon_for_cwa_weather(cond, rain_mm)

    tips = _clothing_and_outdoor_tips(
        temp=temp,
        apparent=apparent,
        rain_mm=rain_mm,
        rain_prob=None,
        wind_kmh=ws_kmh,
        gust_kmh=gust_kmh,
        cond_text=cond,
    )
    feels_str = f"（體感 {apparent:.1f}°C）" if apparent is not None else ""
    rh_str = f"｜濕度 {int(round(rh))}%" if rh is not None else ""
    rain_str = f"｜今日累積雨量 {rain_mm:.1f}mm" if rain_mm > 0 else ""
    st_label = f"{best_st['station_name']}站（距{format_distance(best_st['dist_m'])}）"
    headline = f"{icon} {cond} {temp:.1f}°C{feels_str}{rh_str}{rain_str}"

    return {
        "status": "ok",
        "mode": "live",
        "source": f"中央氣象署官方測站 O-A0003-001 ({st_label})",
        "queried_at": now_taiwan_iso(),
        "observed_time": best_st["obs_time"],
        "station_name": best_st["station_name"],
        "place": place or f"{best_st['county']}{best_st['town']}",
        "condition": cond,
        "icon": icon,
        "temperature_c": temp,
        "apparent_temperature_c": apparent,
        "humidity_pct": int(round(rh)) if rh is not None else None,
        "precipitation_mm": rain_mm,
        "wind_speed_kmh": ws_kmh,
        "wind_gusts_kmh": gust_kmh,
        "headline": headline,
        "outdoor_tips": tips,
    }


def _fetch_open_meteo_weather(lat: float, lon: float, place: str) -> dict[str, Any] | None:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        "precipitation,rain,weather_code,wind_speed_10m,wind_gusts_10m"
        "&hourly=precipitation_probability"
        "&forecast_days=1&timezone=Asia%2FTaipei"
    )
    raw = fetch_json(url, timeout=6, cache_ttl=600)
    cur = raw.get("current") or {}
    hourly = raw.get("hourly") or {}
    temp = cur.get("temperature_2m")
    if temp is None:
        return None
    apparent = cur.get("apparent_temperature")
    humidity = cur.get("relative_humidity_2m")
    rain_mm = float(cur.get("precipitation") or cur.get("rain") or 0.0)
    wcode = int(cur.get("weather_code") or 0)
    wind_kmh = cur.get("wind_speed_10m")
    gust_kmh = cur.get("wind_gusts_10m")

    probs = hourly.get("precipitation_probability") or []
    cur_time = str(cur.get("time") or "")
    times = hourly.get("time") or []
    rain_prob = None
    if probs:
        start_idx = 0
        if cur_time and times:
            prefix = cur_time[:13]
            for idx, t in enumerate(times):
                if str(t).startswith(prefix):
                    start_idx = idx
                    break
        window = [p for p in probs[start_idx : start_idx + 6] if isinstance(p, (int, float))]
        if window:
            rain_prob = int(max(window))

    icon, cond_zh = WMO_WEATHER_ZH.get(wcode, ("🌤️", "多雲"))
    tips = _clothing_and_outdoor_tips(
        temp=temp,
        apparent=apparent,
        rain_mm=rain_mm,
        rain_prob=rain_prob,
        wind_kmh=wind_kmh,
        gust_kmh=gust_kmh,
        cond_text=cond_zh,
    )
    feels_str = f"（體感 {apparent:.1f}°C）" if isinstance(apparent, (int, float)) else ""
    prob_str = f"｜未來6小時降雨機率 {rain_prob}%" if rain_prob is not None else ""
    headline = f"{icon} {cond_zh} {temp:.1f}°C{feels_str}{prob_str}"

    return {
        "status": "ok",
        "mode": "live",
        "source": "Open-Meteo 即時氣象 API (open-meteo.com)",
        "queried_at": now_taiwan_iso(),
        "observed_time": cur_time,
        "place": place,
        "condition": cond_zh,
        "icon": icon,
        "temperature_c": temp,
        "apparent_temperature_c": apparent,
        "humidity_pct": humidity,
        "precipitation_mm": rain_mm,
        "rain_probability_6h_pct": rain_prob,
        "wind_speed_kmh": wind_kmh,
        "wind_gusts_kmh": gust_kmh,
        "headline": headline,
        "outdoor_tips": tips,
    }


def get_outdoor_weather_tips(
    latitude: float = 0.0,
    longitude: float = 0.0,
    place: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢指定 GPS 位置的即時天氣（氣溫、體感、濕度、降雨、風速）並產出日常戶外活動與穿著提示（免金鑰）。"""
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        return {
            "status": "ok",
            "mode": "simulation",
            "source": f"[歷史演練快照] {scenario.get('title', '')}",
            "queried_at": scenario.get("simulated_time", now_taiwan_iso()),
            "headline": "⛈️ 演練模式：颱風強風豪雨期間，請勿進行任何非必要戶外活動！",
            "outdoor_tips": [
                "⛈️ 目前處於颱風豪雨演練情境，風強雨驟，請以防災安全與室內／高處避難為優先，勿安排戶外活動。"
            ],
        }

    queried_at = now_taiwan_iso()
    if not (latitude and longitude and abs(latitude) > 1 and abs(longitude) > 1):
        return {
            "status": "unavailable",
            "mode": "live",
            "source": "中央氣象署即時氣象",
            "queried_at": queried_at,
            "headline": "未提供 GPS 座標，暫無法取得當地即時氣溫提示。",
            "outdoor_tips": [],
        }

    lat, lon, _ = normalize_taiwan_coords(float(latitude), float(longitude))
    errors: list[str] = []

    # 1. 優先使用中央氣象署官方 AWS S3 363 測站（海外主機絕不被 429 擋！）
    try:
        cwa_res = _fetch_cwa_s3_weather(lat, lon, place)
        if cwa_res:
            return cwa_res
    except Exception as e:
        errors.append(f"CWA_S3: {e}")

    # 2. 備援：Open-Meteo
    try:
        om_res = _fetch_open_meteo_weather(lat, lon, place)
        if om_res:
            return om_res
    except Exception as e:
        errors.append(f"OpenMeteo: {e}")

    return {
        "status": "unavailable",
        "mode": "live",
        "source": "中央氣象署 / Open-Meteo",
        "queried_at": queried_at,
        "headline": "暫時無法取得即時氣溫與戶外天氣資料。",
        "error": " | ".join(errors),
        "outdoor_tips": [],
    }


WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def _met_symbol_to_zh(symbol: str) -> str:
    s = (symbol or "").lower()
    if "thunder" in s:
        return "雷雨"
    if "heavyrain" in s:
        return "大雨"
    if "lightrain" in s:
        return "小雨"
    if "rainshowers" in s:
        return "陣雨"
    if "rain" in s:
        return "有雨"
    if "fog" in s:
        return "有霧"
    if "clearsky" in s:
        return "晴朗"
    if "fair" in s:
        return "晴時多雲"
    if "partlycloudy" in s:
        return "多雲時晴"
    if "cloudy" in s:
        return "陰天多雲"
    return "多雲"


def _resolve_forecast_coords(latitude: float, longitude: float, place: str) -> tuple[float, float, str]:
    if latitude and longitude and abs(latitude) > 1 and abs(longitude) > 1:
        lat, lon, _ = normalize_taiwan_coords(float(latitude), float(longitude))
        return lat, lon, place
    cleaned = (place or "").replace("台", "臺").strip()
    if cleaned:
        for county, town, lat, lon in FALLBACK_TOWNSHIPS:
            if town in cleaned or county in cleaned or county[:2] in cleaned:
                return lat, lon, place or f"{county}{town}"
    # 沒有提供地點時不臆測，回傳 0,0 讓呼叫端決定
    return 0.0, 0.0, place


_forecast_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def get_weather_forecast(
    latitude: float = 0.0,
    longitude: float = 0.0,
    place: str = "",
    days: int = 8,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """查詢台灣未來 7~9 天真實數值天氣預報（挪威氣象局 MET Norway 全球預報，免金鑰、不限流）。

    Args:
        latitude: GPS 緯度。
        longitude: GPS 經度。
        place: 選填，台灣縣市或鄉鎮名稱（無 GPS 時自動對應代表座標）。
        days: 查詢天數（預設 8 天，涵蓋本週與下週初）。

    Returns:
        包含每日真實最高／最低溫、預估累積雨量、風速與天氣狀況的字典。
    """
    scenario = get_active_scenario(tool_context)
    if scenario is not None:
        return {
            "status": "simulation",
            "source": f"[歷史演練快照] {scenario.get('title', '')}",
            "message": "目前處於歷史災難演練模式，不提供真實未來一週預報；切回即時模式即可查詢未來天氣。",
            "daily": [],
        }

    lat, lon, resolved_place = _resolve_forecast_coords(latitude, longitude, place)
    queried_at = now_taiwan_iso()
    if not (lat and lon):
        return {
            "status": "unavailable",
            "source": "MET Norway 全球數值天氣預報",
            "queried_at": queried_at,
            "message": "尚未提供查詢縣市或 GPS 座標，無法取得未來天氣預報。",
            "daily": [],
        }

    cache_key = f"{lat:.2f},{lon:.2f}"
    import time as _time
    cached = _forecast_cache.get(cache_key)
    if cached and (_time.monotonic() - cached[0]) < 1800:
        return cached[1]

    try:
        url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat:.4f}&lon={lon:.4f}"
        resp = requests.get(
            url,
            timeout=10,
            headers={
                "User-Agent": "taiwan-disaster-agent/0.5 (https://github.com/gollumy38/taiwan-disaster-agent)"
            },
        )
        resp.raise_for_status()
        raw = resp.json()
        timeseries = raw.get("properties", {}).get("timeseries") or []

        by_date: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"temps": [], "rain_mm": 0.0, "winds": [], "symbols": []}
        )
        for item in timeseries:
            dt = parse_iso_datetime(item.get("time", ""))
            if dt is None:
                continue
            local_dt = dt.astimezone(TAIWAN_TZ)
            day_key = local_dt.strftime("%Y-%m-%d")
            inst = item.get("data", {}).get("instant", {}).get("details", {})
            temp = inst.get("air_temperature")
            ws = inst.get("wind_speed")
            if isinstance(temp, (int, float)):
                by_date[day_key]["temps"].append(float(temp))
            if isinstance(ws, (int, float)):
                by_date[day_key]["winds"].append(float(ws) * 3.6)

            # 取 1h 或 6h 降雨量與天氣符號（避免重複加總，以 1h 優先，無 1h 才取 6h）
            dnode = item.get("data", {})
            n1 = dnode.get("next_1_hours")
            n6 = dnode.get("next_6_hours")
            if n1:
                amt = (n1.get("details") or {}).get("precipitation_amount")
                if isinstance(amt, (int, float)):
                    by_date[day_key]["rain_mm"] += float(amt)
                sym = (n1.get("summary") or {}).get("symbol_code")
                if sym:
                    by_date[day_key]["symbols"].append(sym)
            elif n6:
                amt = (n6.get("details") or {}).get("precipitation_amount")
                if isinstance(amt, (int, float)):
                    by_date[day_key]["rain_mm"] += float(amt)
                sym = (n6.get("summary") or {}).get("symbol_code")
                if sym:
                    by_date[day_key]["symbols"].append(sym)

        daily: list[dict[str, Any]] = []
        for day_key in sorted(by_date.keys())[: max(1, min(days, 10))]:
            bucket = by_date[day_key]
            if not bucket["temps"]:
                continue
            dt_obj = datetime.strptime(day_key, "%Y-%m-%d")
            wday = WEEKDAY_ZH[dt_obj.weekday()]
            # 挑最具代表性（有雨優先，否則取最常出現）的天氣描述
            zh_syms = [_met_symbol_to_zh(s) for s in bucket["symbols"]]
            rain_syms = [z for z in zh_syms if "雨" in z]
            if rain_syms and bucket["rain_mm"] >= 0.5:
                cond = max(set(rain_syms), key=rain_syms.count)
            elif zh_syms:
                cond = max(set(zh_syms), key=zh_syms.count)
            else:
                cond = "多雲"

            daily.append(
                {
                    "日期": f"{day_key} ({wday})",
                    "天氣": cond,
                    "最低溫_C": round(min(bucket["temps"]), 1),
                    "最高溫_C": round(max(bucket["temps"]), 1),
                    "預估累積雨量_mm": round(bucket["rain_mm"], 1),
                    "最大風速_kmh": round(max(bucket["winds"]), 1) if bucket["winds"] else None,
                }
            )

        result = {
            "status": "ok" if daily else "unavailable",
            "mode": "live",
            "source": "挪威氣象局 MET Norway 全球數值天氣預報（實際台灣天氣請以中央氣象署最新預報為準）",
            "queried_at": queried_at,
            "place": resolved_place or f"{lat:.4f}, {lon:.4f}",
            "coverage_start": daily[0]["日期"] if daily else "",
            "coverage_end": daily[-1]["日期"] if daily else "",
            "daily": daily,
        }
        _forecast_cache[cache_key] = (_time.monotonic(), result)
        return result
    except Exception as e:
        return {
            "status": "unavailable",
            "mode": "live",
            "source": "MET Norway 全球數值天氣預報",
            "queried_at": queried_at,
            "message": f"暫時無法取得未來天氣預報資料（{e}）。",
            "daily": [],
        }
