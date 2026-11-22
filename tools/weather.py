"""Real-time local weather & daily outdoor/clothing tips (no API key required).

Primary source:
- Taiwan CWA Official Weather Stations on AWS S3 (`O-A0003-001.xml`, from WATCH list row A8)
  363 official Taiwan stations, zero API key, zero 429 rate-limiting, works cleanly on Render overseas IPs!
Fallback source:
- Open-Meteo public forecast API (when S3 is unreachable).
"""

import math
from typing import Any, Optional
import xml.etree.ElementTree as ET

from .common import fetch_bytes, fetch_json, now_taiwan_iso
from .location import format_distance, haversine_meters, normalize_taiwan_coords
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
