"""Shared deterministic Taiwan disaster risk grading engine.

Used by BOTH:
- Stage 2 `RiskAssessmentAgent` in the interactive 3-agent pipeline
- Background `ProactiveMonitor` for zero-hallucination, zero-LLM-quota patrol alerts
"""

import json
from typing import Any

LEVEL_ORDER: dict[str, int] = {
    "LOW": 0,
    "MEDIUM": 1,
    "HIGH": 2,
    "CRITICAL": 3,
}


def grade_hazards(bundle: dict[str, Any], user_text: str = "") -> dict[str, Any]:
    """Deterministically grade official Taiwan disaster data into LOW/MEDIUM/HIGH/CRITICAL."""
    reasons: list[str] = []
    disaster_types: list[str] = []
    blocked_roads: list[str] = []
    score = 0  # 0=LOW, 1=MEDIUM, 2=HIGH, 3=CRITICAL

    def bump(level: int, reason: str, dtype: str = "") -> None:
        nonlocal score
        score = max(score, level)
        if reason not in reasons:
            reasons.append(reason)
        if dtype and dtype not in disaster_types:
            disaster_types.append(dtype)

    # 1. 水利署與 NCDR 淹水警戒
    flood_blob = json.dumps(bundle.get("flood_warnings", {}).get("data", []), ensure_ascii=False)
    ncdr_flood_blob = json.dumps(bundle.get("ncdr_flood", {}).get("data", []), ensure_ascii=False)
    combined_flood = flood_blob + ncdr_flood_blob
    if "一級警戒" in combined_flood:
        bump(3, "水利署／NCDR 發布當地淹水「一級警戒」（低窪區域已可能積淹水）。", "水災")
    elif "二級警戒" in combined_flood:
        bump(2, "水利署／NCDR 發布當地淹水「二級警戒」（3小時內可能積淹水）。", "水災")

    # 2. 水利署河川水位
    river_data = bundle.get("river_water_level", {}).get("data", [])
    if isinstance(river_data, list):
        for st in river_data:
            if not isinstance(st, dict):
                continue
            code = st.get("警戒等級代碼")
            name = st.get("測站名稱", "")
            basin = st.get("流域", "")
            wl = st.get("即時水位_m")
            top = st.get("堤頂高度_m")
            if code == 1:
                bump(3, f"河川水位達「一級警戒」：{basin} {name}（水位 {wl}m / 堤頂 {top}m）。", "河川溢堤")
            elif code == 2:
                bump(2, f"河川水位達「二級警戒」：{basin} {name}（水位 {wl}m / 堤頂 {top}m）。", "河川溢堤")
            elif code == 3:
                bump(1, f"河川水位達「三級警戒」：{basin} {name}。", "河川溢堤")

    # 3. NCDR 與農村水保署土石流／大崩塌警戒
    debris_blob = json.dumps(bundle.get("ncdr_debris_flow", {}).get("data", []), ensure_ascii=False)
    landslide_blob = json.dumps(bundle.get("landslide_alerts", {}).get("data", []), ensure_ascii=False)
    combined_debris = debris_blob + landslide_blob
    if "紅色警戒" in combined_debris:
        bump(3, "農村水保署／NCDR 發布「土石流／大規模崩塌紅色警戒」（須勸告或強制撤離）。", "土石流")
    elif "黃色警戒" in combined_debris:
        bump(2, "農村水保署／NCDR 發布「土石流／大規模崩塌黃色警戒」（須預防性疏散避難）。", "土石流")

    # 4. 民生公共物聯網 IoT 淹水感測器（路面實測水深公分）
    iot_data = bundle.get("iot_flood_sensors", {}).get("data", [])
    if isinstance(iot_data, list):
        max_cm = 0.0
        max_iot_name = ""
        for sensor in iot_data:
            if isinstance(sensor, dict):
                cm = sensor.get("淹水深度_cm")
                if isinstance(cm, (int, float)) and cm > max_cm:
                    max_cm = float(cm)
                    max_iot_name = str(sensor.get("測站名稱", ""))
        if max_cm >= 30:
            bump(3, f"附近 IoT 淹水感測器「{max_iot_name}」實測路面淹水達 {max_cm} 公分（超過成人小腿危險深度）。", "水災")
        elif max_cm >= 15:
            bump(2, f"附近 IoT 淹水感測器「{max_iot_name}」實測路面淹水達 {max_cm} 公分。", "水災")

    # 5. 水利署水庫放流警戒
    reservoir_data = bundle.get("reservoir_warnings", {}).get("data", [])
    if isinstance(reservoir_data, list) and reservoir_data:
        names = "、".join(
            str(r.get("水庫名稱", "")) for r in reservoir_data if isinstance(r, dict) and r.get("水庫名稱")
        )
        if names:
            bump(2, f"水利署通報影響區水庫放流警戒：{names}（請下游沿岸民眾遠離河床）。", "河川溢堤")

    # 6. NCDR 道路封閉
    road_data = bundle.get("ncdr_road", {}).get("data", [])
    if isinstance(road_data, list) and road_data:
        for entry in road_data:
            if isinstance(entry, dict):
                title = entry.get("標題", "")
                content = entry.get("內容", "")
                if content:
                    blocked_roads.append(f"{title}：{content}")
                elif title:
                    blocked_roads.append(title)
        bump(2, f"當地或聯外道路通報 {len(road_data)} 筆封閉／積水管制警戒。", "道路阻斷")

    # 7. 氣象署雨量站
    rain_data = bundle.get("rainfall", {}).get("data", [])
    if isinstance(rain_data, list):
        max_1h = 0.0
        max_station = ""
        for st in rain_data:
            if isinstance(st, dict):
                val = st.get("1小時雨量_mm")
                if isinstance(val, (int, float)) and val > max_1h:
                    max_1h = float(val)
                    max_station = str(st.get("測站名稱", ""))
        if max_1h >= 80:
            bump(3, f"{max_station}雨量站 1 小時降雨量達 {max_1h} mm（極端豪雨等級）。", "水災")
        elif max_1h >= 60:
            bump(2, f"{max_station}雨量站 1 小時降雨量達 {max_1h} mm（強降雨警戒）。", "水災")
        elif max_1h >= 40:
            bump(1, f"{max_station}雨量站 1 小時降雨量達 {max_1h} mm（大雨等級）。", "水災")

    # 8. 使用者現場描述（若有）
    if user_text and any(k in user_text for k in ("淹到小腿", "淹到膝蓋", "水淹進來", "落石", "土石滑落", "受困")):
        bump(3, "使用者回報現場已出現明顯淹水或落石等立即威脅。", "水災")

    level_map = {0: "LOW", 1: "MEDIUM", 2: "HIGH", 3: "CRITICAL"}
    risk_level = level_map[score]
    if risk_level == "CRITICAL":
        if "土石流" in disaster_types:
            strategy = "立即撤離至安全避難收容處所（遠離溪流與邊坡，避開封閉山區道路）"
        else:
            strategy = "若室外積水過深或達小腿以上，優先「立即垂直避難（往二樓以上移動）」；若仍安全可通行，立即避開封閉路段前往最近安全避難收容處所"
    elif risk_level == "HIGH":
        strategy = "高度警戒：建議儘速完成預防性撤離至最近安全避難收容處所，或隨時準備垂直避難"
    elif risk_level == "MEDIUM":
        strategy = "中度關注：備妥緊急避難包、確認最近避難所位置並持續關注警戒更新"
    else:
        strategy = "目前無官方生效警戒，保持日常關注即可"

    return {
        "risk_level": risk_level,
        "disaster_types": disaster_types or ["無明顯災害"],
        "strategy": strategy,
        "blocked_roads": blocked_roads,
        "should_auto_alert": risk_level in ("HIGH", "CRITICAL"),
        "reasons": reasons or ["官方監測系統目前查無當地生效之淹水、土石流、河川或道路警戒。"],
    }
