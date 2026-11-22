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

FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
]

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


def _offline_kb_answer(question: str, context_summary: str) -> str:
    for keywords, answer in OFFLINE_KNOWLEDGE:
        if any(k in question for k in keywords):
            extra = f"\n\n📍 **你目前紀錄的位置資訊**：\n{context_summary}" if context_summary else ""
            return answer + extra

    base = (
        "🛡️ 我是台灣防災多 Agent 助理！你可以問我任何防災與應變問題，例如：\n"
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
) -> dict[str, Any]:
    """Answer general Taiwan disaster/safety/follow-up questions with multi-model & offline fallback."""
    q = (question or "").strip()
    if not q:
        return {"status": "ok", "reply": _offline_kb_answer("", context_summary), "model": "offline_kb"}

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return {"status": "ok", "reply": _offline_kb_answer(q, context_summary), "model": "offline_kb"}

    history_lines = []
    for turn in (history or [])[-6:]:
        role = "使用者" if turn.get("role") == "user" else "防災助理"
        history_lines.append(f"{role}：{turn.get('text', '')[:300]}")
    history_block = "\n".join(history_lines)

    prompt = (
        "你是親切、專業且務實的「台灣防災與日常安全助理」。\n"
        "回答原則：\n"
        "1. 使用繁體中文、條列清晰、句子簡短好讀（適合手機閱讀，盡量控制在 250~400 字內）。\n"
        "2. 符合台灣防災實務（例如：地震『趴下、掩護、穩住』、淹水勿進地下室牽車與勿闖地下道、土石流黃紅警戒定義、119／110／1911／1910 專線、中央氣象署與水利署標準）。\n"
        "3. 若下方提供了「使用者目前位置與最近查詢背景」，請自然結合該背景回答追問（例如回答該避難所資訊、是否該出門、當地天氣穿著等）；但絕對不要捏造沒查過的即時災情數據！若使用者問到另一個新地點的即時災情，請引導他直接輸入該台灣縣市鄉鎮或分享位置。\n"
        "4. 若話題涉及緊急生命危險，結尾提醒撥打 119；若是一般日常天氣或防範知識，語氣友善自然即可。\n\n"
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
                config=types.GenerateContentConfig(temperature=0.3),
            )
            text = (resp.text or "").strip()
            if text:
                return {"status": "ok", "reply": text, "model": model_name}
        except Exception:
            continue

    return {
        "status": "ok",
        "reply": _offline_kb_answer(q, context_summary),
        "model": "offline_kb_fallback",
    }
