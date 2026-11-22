"""Context-aware multi-turn Taiwan disaster Q&A assistant (for LINE Bot & Web Chat).

Handles any general disaster-preparedness or follow-up question (evacuation kit,
earthquake actions, flood/basement safety, debris-flow signs, elderly/pet evacuation,
power/water outage, follow-ups about the user's current GPS & nearest shelters).
Includes multi-model quota fallback + built-in Taiwan disaster knowledge fallback so
it NEVER crashes even when free-tier LLM quotas are exhausted.
"""

import os
from typing import Any

from google import genai
from google.genai import types

from .common import now_taiwan
from .weather import WEEKDAY_ZH, get_weather_forecast

FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
]

FORECAST_KEYWORDS = (
    "明天", "後天", "大後天", "下週", "下周", "這週", "這周", "本週", "本周",
    "週末", "周末", "禮拜", "星期", "未來", "預報", "一週", "一周", "幾天",
    "會不會下雨", "會下雨嗎", "會變冷", "變冷", "冷氣團", "天氣如何", "天氣怎麼樣",
)

UNSUPPORTED_REALTIME_TOPICS: list[tuple[tuple[str, ...], str]] = [
    (
        ("停班", "停課", "颱風假", "上班上課"),
        "📢 本系統**尚未串接即時停班停課資料**（為避免誤導，我不做臆測）。\n"
        "請直接查詢官方權威網站：\n"
        "• 行政院人事行政總處《天然災害停止上班及上課情形》：https://www.dgpa.gov.tw/typh/daily/nds.html",
    ),
    (
        ("哪裡停電", "停電查詢", "復電", "停水查詢", "哪裡停水"),
        "📢 本系統**尚未串接即時停電／停水區域清單**（為避免誤導，我不做臆測）。\n"
        "請直接透過官方管道查詢或通報：\n"
        "• 台電停電查詢／報修專線：**1911**（或台灣電力 APP）\n"
        "• 台水停水公告／客服專線：**1910**（https://wateroff.water.gov.tw）",
    ),
]


def is_forecast_question(text: str) -> bool:
    return any(k in (text or "") for k in FORECAST_KEYWORDS)

# 當雲端免費額度暫時用完時的台灣防災知識庫備援（依關鍵字命中）
OFFLINE_KNOWLEDGE: list[tuple[tuple[str, ...], str]] = [
    (
        ("避難包", "急救包", "緊急包", "準備什麼", "帶什麼"),
        "🎒 **台灣緊急避難包必備清單（建議放門口隨手可得處，每半年檢查效期）**\n"
        "1. **水與糧食**：每人至少 3 公升飲用水、乾糧／能量棒／罐頭（3 日份）\n"
        "2. **醫療衛生**：個人慢性病處方藥、簡易急救包、口罩、濕紙巾、哨子\n"
        "3. **通訊照明**：行動電源、充電線、手電筒＋備用電池、手搖／電池收音機\n"
        "4. **重要證件與現金**：身分證／健保卡影本、少量零錢現金（停電時刷卡機不能用）\n"
        "5. **保暖與衣物**：輕便雨衣、保暖衣物、鋁箔保暖毯、粗棉手套\n"
        "6. **特殊成員加配**：嬰兒奶粉尿布、長輩助聽器電池／輪椅配件、寵物飼料與牽繩。",
    ),
    (
        ("地震", "搖", "餘震", "海嘯"),
        "🌍 **地震發生當下保命步驟（趴下、掩護、穩住）**\n"
        "1. **立刻『趴下、掩護、穩住』**：躲在堅固桌下護住頭頸，遠離窗戶、書櫃與吊燈；**不要**在劇烈搖晃時衝去開門或跑下樓。\n"
        "2. **晃動停止後**：檢查瓦斯、電源與出口是否變形；若聞到瓦斯味，**切勿開關任何電燈或電器**，立刻開窗離開。\n"
        "3. **切勿搭電梯**：走樓梯疏散至戶外空曠處，注意外牆磁磚與招牌掉落。\n"
        "4. **沿海地區**：若強震搖晃超過 30 秒或收到海嘯警報，立刻往高處或堅固建物 3 樓以上移動。\n"
        "⚠️ 緊急受困請撥 **119**。",
    ),
    (
        ("地下室", "牽車", "開車", "涉水", "淹水", "積水", "垂直避難"),
        "🌊 **淹水與積水保命關鍵原則**\n"
        "1. **絕對不要進地下室牽車**：積水灌入地下室只需數分鐘，水壓會讓門打不開且極易滅頂。\n"
        "2. **水深達半個輪胎（約 25~30cm）禁止開車通行**：車輛極易熄火拋錨或被水流漂走；切勿闖入地下道與陸橋下。\n"
        "3. **室外水深達腳踝／小腿且流速快時**：優先**「垂直避難」**往堅固建築物 2 樓以上移動，不要冒險涉水。\n"
        "4. **屋內進水前**：在安全無觸電前提下關閉總電源與瓦斯主開關。\n"
        "⚠️ 危急請立即撥 **119**。",
    ),
    (
        ("土石流", "崩塌", "山崩", "前兆", "山區"),
        "⛰️ **土石流與大規模崩塌前兆及保命原則**\n"
        "1. **4 大危險前兆**：溪水突然變混濁或夾帶樹枝、溪水突然異常斷流、山谷傳來轟隆異常聲響、山坡出現泥水滲出或樹木傾斜。\n"
        "2. **黃色警戒**＝預防性疏散避難；**紅色警戒**＝強制撤離，務必配合村里長與警消撤往安全收容所。\n"
        "3. **逃生方向**：若不幸目擊土石流，應朝**「垂直於溪流流向的兩側高處」**迅速撤離，絕對不要順著河谷往下游跑。\n"
        "4. 切勿強行通過已預警封閉的山區省道（如台20、台24、台29線等）。",
    ),
    (
        ("停電", "停水", "瓦斯", "冰箱"),
        "💡 **颱風／災時停電停水處置建議**\n"
        "1. **停電**：先拔除高功率電器插頭避免復電瞬間突波損壞；優先用手電筒而非蠟燭（避免火災或瓦斯外洩引燃）；冰箱盡量少開門以維持低溫。\n"
        "2. **停水／濁度升高**：災前先儲水，水退或復水後飲用水務必徹底煮沸。\n"
        "3. **通報管道**：台電搶修專線 **1911**、台水客服 **1910**、緊急災害救援 **119**。",
    ),
    (
        ("長輩", "老人", "輪椅", "嬰兒", "行動不便", "寵物", "毛孩", "貓", "狗"),
        "👨‍👩‍👧 **特殊族群與毛孩撤離建議**\n"
        "1. **長輩／行動不便者**：務必比一般人更早於「二級／黃色警戒」階段就完成預防性撤離；優先選擇標示「適合避難弱者安置」的無障礙室內收容所，並備妥 3~7 日慢性病處方藥與病歷摘要。\n"
        "2. **洗腎／居家氧氣機患者**：颱風前先聯繫醫院與衛生所確認緊急安置與備用電源。\n"
        "3. **寵物同行**：請備妥外出籠、牽繩、3 日飼料水與晶片／狂犬病證明；出發前先致電目標避難收容處所確認寵物安置規定。",
    ),
    (
        ("119", "求救", "受困", "簡訊", "報平安"),
        "🆘 **受困求援與 119 簡訊報案範本**\n"
        "若電話打不通或訊號微弱，可直接複製發送簡訊至 **119**（或告知親友代報）：\n"
        "「【緊急求救】位置：(填地址或GPS經緯度)｜所在樓層：(例:2樓)｜受困人數：(例:2大1小)｜現場狀況：(例:1樓淹水至腰部/停電)｜目前人員安全狀態：(例:意識清楚/手機剩20%電)」\n"
        "• 盡量保持手機省電（關藍牙、降亮度），輪流開機聯繫。",
    ),
]


def _get_models() -> list[str]:
    models: list[str] = []
    for key in ("CHAT_MODEL", "PLANNER_MODEL", "GEMINI_MODEL"):
        val = os.getenv(key, "").strip()
        if val and val not in models:
            models.append(val)
    for m in FALLBACK_MODELS:
        if m not in models:
            models.append(m)
    return models


def _format_forecast_table(fc: dict[str, Any]) -> str:
    daily = fc.get("daily") or []
    if not daily:
        return ""
    lines = [
        f"📅 **未來天氣預報（{fc.get('place', '')}｜{fc.get('coverage_start')} ～ {fc.get('coverage_end')}）**"
    ]
    for d in daily:
        rain = f"｜預估雨量 {d['預估累積雨量_mm']}mm" if d.get("預估累積雨量_mm", 0) > 0 else "｜無明顯降雨"
        lines.append(
            f"- {d['日期']}：{d['天氣']}，{d['最低溫_C']}～{d['最高溫_C']}°C{rain}"
        )
    lines.append(f"資料來源：{fc.get('source', '')}")
    return "\n".join(lines)


def _offline_kb_answer(question: str, context_summary: str, forecast_block: str = "") -> str:
    if forecast_block:
        return forecast_block + "\n\n*(註：超過上述涵蓋日期的更長期天氣無法準確預測，請屆時參考中央氣象署官網 cwa.gov.tw。)*"

    for keywords, answer in UNSUPPORTED_REALTIME_TOPICS:
        if any(k in question for k in keywords):
            return answer

    for keywords, answer in OFFLINE_KNOWLEDGE:
        if any(k in question for k in keywords):
            extra = f"\n\n📍 **你目前紀錄的位置資訊**：\n{context_summary}" if context_summary else ""
            return answer + extra

    base = (
        "🛡️ 我是台灣防災多 Agent 助理！你可以問我任何防災與天氣問題，例如：\n"
        "• 📅 明天或下週天氣如何？（提供真實未來 7～9 天數值預報）\n"
        "• 🎒 緊急避難包要準備什麼？\n"
        "• 🌍 地震來了怎麼保命？\n"
        "• 🌊 淹水時可以進地下室牽車或開車嗎？\n"
        "• ⛰️ 土石流有哪些前兆？\n"
        "• 👨‍👩‍👧 家裡有長輩或寵物怎麼撤離？\n"
        "• 📍 或直接傳送 GPS 位置／輸入台灣地名（如「高雄三民區」），幫你查即時官方警戒與最近避難所！"
    )
    if context_summary:
        base += f"\n\n📍 **目前已知你的位置摘要**：\n{context_summary}"
    return base


def answer_disaster_question(
    question: str,
    context_summary: str = "",
    history: list[dict[str, str]] | None = None,
    latitude: float = 0.0,
    longitude: float = 0.0,
    place: str = "",
    scenario_id: str = "",
) -> dict[str, Any]:
    """Answer general Taiwan disaster/weather/follow-up questions with strict anti-hallucination guardrails."""
    q = (question or "").strip()
    if not q:
        return {"status": "ok", "reply": _offline_kb_answer("", context_summary), "model": "offline_kb"}

    # 1. 對明確未串接的即時官方公告直接給誠實答覆，零幻覺！
    for keywords, fixed_reply in UNSUPPORTED_REALTIME_TOPICS:
        if any(k in q for k in keywords):
            return {"status": "ok", "reply": fixed_reply, "model": "guardrail_direct"}

    now_dt = now_taiwan()
    today_str = f"{now_dt.strftime('%Y-%m-%d')}（星期{WEEKDAY_ZH[now_dt.weekday()]}）{now_dt.strftime('%H:%M')} UTC+8"

    # 2. 若詢問未來天氣／明天／下週，自動撈取真實 9 天數值預報（絕不讓 LLM 瞎編！）
    forecast_block = ""
    forecast_warning = ""
    if is_forecast_question(q):
        if scenario_id:
            forecast_warning = "使用者目前處於歷史災難演練模式，無法查真實未來預報；請提醒他切回「即時模式」再查未來一週天氣，嚴禁編造。"
        else:
            fc = get_weather_forecast(latitude=latitude, longitude=longitude, place=place, days=9)
            if fc.get("status") == "ok" and fc.get("daily"):
                forecast_block = _format_forecast_table(fc)
            else:
                forecast_warning = (
                    f"【重要限制】目前無法取得未來天氣預報資料（原因：{fc.get('message') or '查無座標或連線失敗'}）。"
                    "你絕對不可以編造任何未來日期的溫度、天氣或降雨機率！請誠實告知暫時無法查到，並引導使用者指定台灣縣市／GPS 或前往中央氣象署官網 (cwa.gov.tw)。"
                )

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "ok",
            "reply": _offline_kb_answer(q, context_summary, forecast_block=forecast_block),
            "model": "offline_kb",
        }

    history_lines = []
    for turn in (history or [])[-6:]:
        role = "使用者" if turn.get("role") == "user" else "防災助理"
        history_lines.append(f"{role}：{turn.get('text', '')[:300]}")
    history_block = "\n".join(history_lines)

    prompt = (
        "你是親切、專業且「絕對誠實、零幻覺」的台灣防災與氣象助理。\n"
        f"現在真實台灣時間：{today_str}\n\n"
        "【嚴格反幻覺鐵律（最高優先級，違反即嚴重錯誤）】：\n"
        "1. 只能根據下方【已查獲的真實背景／預報數據】回答任何數字（氣溫、雨量、風速、日期、警戒等級、距離）。\n"
        "2. 絕對不可以憑空編造或猜測任何沒有出現在下方數據裡的未來天氣、降雨機率、颱風路徑、地震預測、停班停課、停電或停水名單！\n"
        "3. 若使用者問的時間超出【已查獲的真實未來天氣預報】涵蓋的最後日期（例如問兩週後、下個月、明年天氣），必須明確說明預報僅涵蓋上述日期範圍，更長期無法準確預測，請屆時查中央氣象署官網 (cwa.gov.tw)。\n"
        "4. 若未提供預報數據且使用者問未來天氣，必須誠實說「目前沒有查到該地點／該時間的預報資料」，切勿自己編造。\n"
        "5. 回答未來天氣時，請清楚對照「現在真實台灣時間」推算明天／週末／下週對應的真實日期，並註明資料來源。\n"
        "6. 使用繁體中文、條列清晰、簡短好讀（約 200~400 字）；涉及立即生命危險時結尾提醒撥打 119。\n\n"
        + (f"【已查獲的真實未來天氣預報】\n{forecast_block}\n\n" if forecast_block else "")
        + (f"{forecast_warning}\n\n" if forecast_warning else "")
        + (f"【使用者目前位置與最近查詢背景】\n{context_summary}\n\n" if context_summary else "")
        + (f"【先前對話紀錄】\n{history_block}\n\n" if history_block else "")
        + f"【使用者最新提問】\n{q}"
    )

    client = genai.Client(api_key=api_key)
    for model_name in _get_models():
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1),
            )
            text = (resp.text or "").strip()
            if text:
                return {"status": "ok", "reply": text, "model": model_name}
        except Exception:
            continue

    return {
        "status": "ok",
        "reply": _offline_kb_answer(q, context_summary, forecast_block=forecast_block),
        "model": "offline_kb_fallback",
    }
