"""LINE Messaging API v2 integration (zero extra dependencies, HMAC-SHA256 verified).

Features:
- Signature verification (`X-Line-Signature`)
- Reply API (free unlimited) & Push API (for Proactive Monitoring automatic alerts)
- Rich color-coded LINE Flex Message evacuation & weather card
- Native LINE Quick Reply buttons (including 1-tap `location` picker!)
"""

import base64
import hashlib
import hmac
from typing import Any

import requests

from .common import get_env

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

RISK_THEME: dict[str, tuple[str, str]] = {
    "CRITICAL": ("#DC2626", "🚨 極高風險｜立即避難"),
    "HIGH": ("#EA580C", "⚠️ 高風險｜建議撤離"),
    "MEDIUM": ("#D97706", "🟡 中度警戒｜提高警覺"),
    "LOW": ("#059669", "🟢 目前安全｜無生效警戒"),
}


def is_line_configured() -> bool:
    return bool(get_env("LINE_CHANNEL_SECRET") and get_env("LINE_CHANNEL_ACCESS_TOKEN"))


def verify_line_signature(body: bytes, signature: str) -> bool:
    """Verify LINE webhook X-Line-Signature header using HMAC-SHA256."""
    secret = get_env("LINE_CHANNEL_SECRET")
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature.strip())


def _line_headers() -> dict[str, str]:
    token = get_env("LINE_CHANNEL_ACCESS_TOKEN")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def reply_line_messages(reply_token: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Send up to 5 LINE messages using a webhook replyToken (free unlimited)."""
    token = get_env("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return {"status": "skipped", "reason": "未設定 LINE_CHANNEL_ACCESS_TOKEN"}
    try:
        resp = requests.post(
            LINE_REPLY_URL,
            headers=_line_headers(),
            json={"replyToken": reply_token, "messages": messages[:5]},
            timeout=12,
        )
        resp.raise_for_status()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def push_line_messages(line_user_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Proactively push up to 5 LINE messages to a subscribed user."""
    token = get_env("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return {"status": "skipped", "reason": "未設定 LINE_CHANNEL_ACCESS_TOKEN"}
    if not line_user_id:
        return {"status": "skipped", "reason": "缺少 line_user_id"}
    try:
        resp = requests.post(
            LINE_PUSH_URL,
            headers=_line_headers(),
            json={"to": line_user_id, "messages": messages[:5]},
            timeout=12,
        )
        resp.raise_for_status()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def default_quick_reply(is_simulation: bool = False) -> dict[str, Any]:
    """Build native LINE Quick Reply bar with 1-tap GPS location picker."""
    items: list[dict[str, Any]] = [
        {
            "type": "action",
            "action": {"type": "location", "label": "📍 傳送我的位置"},
        },
        {
            "type": "action",
            "action": {"type": "message", "label": "📡 訂閱主動推播", "text": "訂閱"},
        },
    ]
    if is_simulation:
        items.append(
            {
                "type": "action",
                "action": {"type": "message", "label": "🟢 切回即時模式", "text": "切回即時"},
            }
        )
    else:
        items.append(
            {
                "type": "action",
                "action": {"type": "message", "label": "🧪 凱米颱風演練", "text": "演練凱米"},
            }
        )
    items.append(
        {
            "type": "action",
            "action": {"type": "message", "label": "❓ 使用說明", "text": "說明"},
        }
    )
    return {"items": items}


def build_welcome_messages() -> list[dict[str, Any]]:
    text = (
        "🛡️ 歡迎使用【台灣防災多 Agent 撤離助理】！\n\n"
        "你可以這樣用：\n"
        "1️⃣ 點下方「📍 傳送我的位置」分享 GPS，立刻幫你查官方警戒、最近避難所、步行路線與即時天氣\n"
        "2️⃣ 直接打地名提問（例如：新北市汐止安全嗎？）\n"
        "3️⃣ 傳「訂閱」開啟主動監測推播（警戒升級會自動推播通知你）\n"
        "4️⃣ 傳「演練凱米」可進入 2024 凱米颱風歷史演練模式試玩\n\n"
        "⚠️ 緊急生命危險請立即撥打 119。"
    )
    return [{"type": "text", "text": text, "quickReply": default_quick_reply(False)}]


def build_evacuation_flex_message(
    risk_level: str,
    place: str,
    reasons: list[str],
    strategy: str,
    shelter: dict[str, Any] | None = None,
    navigation_url: str = "",
    outdoor_weather: dict[str, Any] | None = None,
    blocked_roads: list[str] | None = None,
    alert_id: str = "",
    is_simulation: bool = False,
    simulated_time: str = "",
    is_proactive_push: bool = False,
) -> dict[str, Any]:
    """Build a clean, color-coded LINE Flex Message card for disaster alerts & daily tips."""
    bg_color, level_label = RISK_THEME.get(risk_level, ("#334155", f"風險等級：{risk_level}"))
    header_tag = "📡 主動監測自動推播" if is_proactive_push else "🛡️ 台灣防災多 Agent 報告"
    if is_simulation:
        header_tag = f"🧪 歷史演練 ({simulated_time[:16]})" if simulated_time else "🧪 歷史災難演練模式"

    body_contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"📍 {place or '查詢位置'}",
            "weight": "bold",
            "size": "md",
            "color": "#0F172A",
            "wrap": True,
        },
        {
            "type": "text",
            "text": strategy or "請留意官方最新防災警戒。",
            "size": "sm",
            "color": "#334155",
            "wrap": True,
            "margin": "sm",
        },
    ]

    if alert_id:
        body_contents.append(
            {
                "type": "text",
                "text": f"🔔 已自動發出告警通知（編號：{alert_id}，免人工審核）",
                "size": "xs",
                "color": "#B91C1C",
                "weight": "bold",
                "wrap": True,
                "margin": "sm",
            }
        )

    if reasons:
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append(
            {
                "type": "text",
                "text": "📊 官方監測依據",
                "weight": "bold",
                "size": "xs",
                "color": "#64748B",
                "margin": "md",
            }
        )
        for r in reasons[:3]:
            body_contents.append(
                {
                    "type": "text",
                    "text": f"• {r[:120]}",
                    "size": "xs",
                    "color": "#1E293B",
                    "wrap": True,
                    "margin": "xs",
                }
            )

    # 低／中風險附上日常戶外天氣與穿著提示
    if risk_level in ("LOW", "MEDIUM") and outdoor_weather and outdoor_weather.get("status") == "ok":
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append(
            {
                "type": "text",
                "text": f"🌤️ {outdoor_weather.get('headline', '即時天氣')}",
                "weight": "bold",
                "size": "xs",
                "color": "#1D4ED8",
                "wrap": True,
                "margin": "md",
            }
        )
        for tip in (outdoor_weather.get("outdoor_tips") or [])[:2]:
            body_contents.append(
                {
                    "type": "text",
                    "text": tip[:120],
                    "size": "xs",
                    "color": "#334155",
                    "wrap": True,
                    "margin": "xs",
                }
            )

    if blocked_roads:
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append(
            {
                "type": "text",
                "text": f"🚧 避開封閉路段：{blocked_roads[0][:110]}",
                "size": "xs",
                "color": "#C2410C",
                "wrap": True,
                "margin": "md",
            }
        )

    if shelter:
        body_contents.append({"type": "separator", "margin": "md"})
        dist = shelter.get("距離與方位") or ""
        addr = shelter.get("地址") or shelter.get("縣市及鄉鎮市區") or ""
        cap = shelter.get("預計收容人數") or 0
        body_contents.append(
            {
                "type": "text",
                "text": f"🏫 最近避難所：{shelter.get('名稱', '')}",
                "weight": "bold",
                "size": "sm",
                "color": "#065F46",
                "wrap": True,
                "margin": "md",
            }
        )
        body_contents.append(
            {
                "type": "text",
                "text": f"{dist}｜{addr}（容納 {cap} 人）".strip("｜"),
                "size": "xs",
                "color": "#334155",
                "wrap": True,
                "margin": "xs",
            }
        )

    body_contents.append(
        {
            "type": "text",
            "text": "⚠️ 撤離請以地方政府與村里長指令為準，緊急生命危險請撥 119。",
            "size": "xxs",
            "color": "#64748B",
            "wrap": True,
            "margin": "md",
        }
    )

    footer_buttons: list[dict[str, Any]] = []
    if navigation_url and navigation_url.startswith("http"):
        footer_buttons.append(
            {
                "type": "button",
                "style": "primary",
                "color": bg_color,
                "height": "sm",
                "action": {
                    "type": "uri",
                    "label": "🚶 Google Maps 導航去避難所",
                    "uri": navigation_url,
                },
            }
        )

    public_url = get_env("PUBLIC_BASE_URL").rstrip("/")
    if public_url.startswith("http"):
        footer_buttons.append(
            {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {
                    "type": "uri",
                    "label": "🗺️ 開啟互動地圖與即時 CCTV",
                    "uri": public_url,
                },
            }
        )

    bubble: dict[str, Any] = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": bg_color,
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "text",
                    "text": header_tag,
                    "color": "#FFFFFFCC",
                    "size": "xs",
                    "weight": "bold",
                },
                {
                    "type": "text",
                    "text": level_label,
                    "color": "#FFFFFF",
                    "size": "lg",
                    "weight": "bold",
                    "margin": "xs",
                },
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "contents": body_contents,
        },
    }
    if footer_buttons:
        bubble["footer"] = {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "12px",
            "contents": footer_buttons,
        }

    alt_text = f"{level_label}｜{place}｜{strategy[:60]}"
    return {
        "type": "flex",
        "altText": alt_text[:390],
        "contents": bubble,
        "quickReply": default_quick_reply(is_simulation),
    }
