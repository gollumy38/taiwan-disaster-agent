"""Real-time local weather & daily outdoor tips (no API key required via Open-Meteo)."""

from typing import Any, Optional

from .common import fetch_json, now_taiwan_iso
from .location import normalize_taiwan_coords
from .scenarios import get_active_scenario

try:
    from google.adk.tools import ToolContext
except ImportError:  # pragma: no cover
    ToolContext = Any  # type: ignore[misc, assignment]

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


def _generate_outdoor_tips(
    temp: float | None,
    apparent: float | None,
    rain_mm: float,
    rain_prob: int | None,
    wind_kmh: float | None,
    gust_kmh: float | None,
    weather_code: int,
) -> list[str]:
    tips: list[str] = []
    feels = apparent if apparent is not None else temp

    # 1. 降雨與路面提醒
    if weather_code in (95, 96, 99):
        tips.append("⛈️ 目前有雷雨發展，戶外活動請留意雷擊，避免停留在空曠處、樹下或溪邊。")
    elif rain_mm >= 5.0 or weather_code in (63, 65, 81, 82):
        tips.append("🌧️ 雨勢較明顯，外出請攜帶雨具、穿防滑防水鞋，騎車與開車請放慢車速留意積水打滑。")
    elif rain_mm > 0 or weather_code in (51, 53, 55, 61, 80) or (rain_prob is not None and rain_prob >= 50):
        prob_str = f"（未來幾小時降雨機率約 {rain_prob}%）" if rain_prob is not None else ""
        tips.append(f"☂️ 有下雨或降雨機會{prob_str}，出門建議隨身帶折疊傘或輕便雨衣。")
    elif rain_prob is not None and rain_prob >= 30:
        tips.append(f"🌦️ 未來幾小時有局部短暫雨機會（約 {rain_prob}%），長時間外出可備把輕便傘。")

    # 2. 氣溫與穿著提醒
    if feels is not None:
        if feels <= 12.0:
            tips.append(f"🥶 天氣非常寒冷（體感約 {feels:.1f}°C），務必穿保暖羽絨／防風厚外套並戴圍巾帽子，長輩與心血管族群注意保暖。")
        elif feels <= 17.5:
            tips.append(f"🧥 天氣偏冷（體感約 {feels:.1f}°C），外出建議採洋蔥式穿搭並加件防風外套，騎車注意保暖。")
        elif feels <= 21.0:
            tips.append(f"🧣 氣候微涼舒適（體感約 {feels:.1f}°C），早晚偏涼建議攜帶薄長袖或輕外套。")
        elif feels >= 35.0:
            tips.append(f"🥵 體感高溫炎熱（約 {feels:.1f}°C），戶外務必多補充水分、做好防曬，盡量避開正午烈日下劇烈活動以防中暑。")
        elif feels >= 31.0:
            tips.append(f"☀️ 天氣偏熱（體感約 {feels:.1f}°C），外出記得防曬、戴遮陽帽並適時補水。")

    # 3. 風勢提醒
    peak_wind = max(wind_kmh or 0.0, gust_kmh or 0.0)
    if peak_wind >= 45.0:
        tips.append(f"💨 陣風偏強（約 {peak_wind:.0f} km/h），騎車請留意側風，海邊與空曠高處注意強風及招牌／樹枝掉落。")
    elif peak_wind >= 30.0:
        tips.append(f"🍃 風勢稍大（陣風約 {peak_wind:.0f} km/h），體感會比氣溫更涼，建議加件防風外衣。")

    # 4. 濃霧提醒
    if weather_code in (45, 48):
        tips.append("🌫️ 局部有霧能見度較差，行車請開霧燈並保持安全車距。")

    if not tips:
        tips.append("✨ 目前天氣舒適穩定、無防災警戒，適合正常戶外活動；也歡迎隨時留意附近避難收容處所位置以備不時之需。")

    return tips


def get_outdoor_weather_tips(
    latitude: float = 0.0,
    longitude: float = 0.0,
    place: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """查詢指定 GPS 位置的即時天氣（氣溫、體感、降雨、風速）並產出日常戶外活動與穿著提示（免金鑰）。

    Args:
        latitude: GPS 緯度。
        longitude: GPS 經度。
        place: 選填，地點名稱。

    Returns:
        包含即時氣象數值、一句話摘要與條列式戶外提示的字典。
    """
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
            "source": "Open-Meteo 即時氣象 API",
            "queried_at": queried_at,
            "headline": "未提供 GPS 座標，暫無法取得當地即時氣溫提示。",
            "outdoor_tips": [],
        }

    lat, lon, _ = normalize_taiwan_coords(float(latitude), float(longitude))
    try:
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
        apparent = cur.get("apparent_temperature")
        humidity = cur.get("relative_humidity_2m")
        rain_mm = float(cur.get("precipitation") or cur.get("rain") or 0.0)
        wcode = int(cur.get("weather_code") or 0)
        wind_kmh = cur.get("wind_speed_10m")
        gust_kmh = cur.get("wind_gusts_10m")

        # 取未來 6 小時內最高降雨機率
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
        tips = _generate_outdoor_tips(
            temp=temp,
            apparent=apparent,
            rain_mm=rain_mm,
            rain_prob=rain_prob,
            wind_kmh=wind_kmh,
            gust_kmh=gust_kmh,
            weather_code=wcode,
        )

        temp_str = f"{temp:.1f}°C" if isinstance(temp, (int, float)) else ""
        feels_str = f"（體感 {apparent:.1f}°C）" if isinstance(apparent, (int, float)) else ""
        prob_str = f"｜未來6小時降雨機率 {rain_prob}%" if rain_prob is not None else ""
        headline = f"{icon} {cond_zh} {temp_str}{feels_str}{prob_str}"

        return {
            "status": "ok",
            "mode": "live",
            "source": "Open-Meteo 即時氣象 API (open-meteo.com)",
            "queried_at": queried_at,
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
    except Exception as e:
        return {
            "status": "unavailable",
            "mode": "live",
            "source": "Open-Meteo 即時氣象 API",
            "queried_at": queried_at,
            "headline": "暫時無法取得即時氣溫與戶外天氣資料。",
            "error": str(e),
            "outdoor_tips": [],
        }
