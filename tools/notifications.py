"""Automated evacuation & disaster alert notification tool (no human approval required).

Dispatches alerts immediately when risk level reaches HIGH or CRITICAL:
1. Always appends an audit record to `logs/alerts_log.jsonl`.
2. In live mode, if `ALERT_WEBHOOK_URL` is configured in `.env`, automatically POSTs
   the alert payload to the webhook (compatible with Discord / Slack / custom endpoints).
3. In historical simulation mode, marks `is_simulation: true` and logs locally without
   firing external production webhooks (preventing accidental real alerts during drills).
"""

import json
from typing import Any, Optional
import uuid

import requests

from .common import PROJECT_ROOT, get_env, now_taiwan_iso
from .scenarios import get_active_scenario

try:
    from google.adk.tools import ToolContext
except ImportError:  # pragma: no cover
    ToolContext = Any  # type: ignore[misc, assignment]

LOGS_DIR = PROJECT_ROOT / "logs"
ALERTS_LOG_PATH = LOGS_DIR / "alerts_log.jsonl"


def send_evacuation_alert(
    severity: str,
    location: str,
    summary: str,
    recommended_shelter: str = "",
    shelter_address: str = "",
    navigation_url: str = "",
    action_steps: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """自動發送防災警戒與撤離推播通知（無需人工審核，高風險或極高風險時自動執行）。

    Args:
        severity: 風險等級，例如「HIGH」（高風險）或「CRITICAL」（極高風險／立即避難）。
        location: 受影響地點或 GPS 座標描述，例如「高雄市三民區 (22.6465, 120.3208)」。
        summary: 簡明災情摘要（1~2 句）。
        recommended_shelter: 選填，推薦的最近安全避難收容處所名稱。
        shelter_address: 選填，推薦避難所地址。
        navigation_url: 選填，一鍵 Google Maps 導航網址。
        action_steps: 選填，立即行動指引摘要。

    Returns:
        包含告警編號、發送時間、是否為演練模式與各管道發送結果的字典。
    """
    scenario = get_active_scenario(tool_context)
    is_simulation = scenario is not None
    alert_id = f"ALT-{uuid.uuid4().hex[:8].upper()}"
    dispatched_at = now_taiwan_iso()

    prefix = "🧪 [演練模擬自動告警]" if is_simulation else "🚨 [自動防災撤離告警]"
    lines = [
        f"{prefix} 等級：{severity}｜地點：{location}",
        f"摘要：{summary}",
    ]
    if recommended_shelter:
        lines.append(f"推薦避難所：{recommended_shelter}（{shelter_address}）")
    if navigation_url:
        lines.append(f"導航連結：{navigation_url}")
    if action_steps:
        lines.append(f"立即行動：{action_steps}")
    lines.append("緊急生命危險請立即撥打 119。")
    message_text = "\n".join(lines)

    record = {
        "alert_id": alert_id,
        "dispatched_at": dispatched_at,
        "is_simulation": is_simulation,
        "scenario_id": scenario["scenario_id"] if scenario else None,
        "simulated_time": scenario.get("simulated_time") if scenario else None,
        "severity": severity,
        "location": location,
        "summary": summary,
        "recommended_shelter": recommended_shelter,
        "shelter_address": shelter_address,
        "navigation_url": navigation_url,
        "action_steps": action_steps,
        "message": message_text,
    }

    channels_sent: list[str] = []
    webhook_error = ""

    # 1. 自動寫入本機告警紀錄檔（logs/alerts_log.jsonl）
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with ALERTS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        channels_sent.append("local_alert_log (logs/alerts_log.jsonl)")
    except Exception as e:
        webhook_error = f"寫入本機紀錄失敗：{e}"

    # 2. 若為即時模式且 .env 有設定 ALERT_WEBHOOK_URL，自動送出外部推播
    webhook_url = get_env("ALERT_WEBHOOK_URL")
    if webhook_url and not is_simulation:
        try:
            payload = {
                "content": message_text,  # Discord 相容
                "text": message_text,     # Slack / 通用 Webhook 相容
                "alert": record,
            }
            resp = requests.post(webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            channels_sent.append("external_webhook (ALERT_WEBHOOK_URL)")
        except Exception as e:
            webhook_error = f"外部 Webhook 推播失敗：{e}"
    elif webhook_url and is_simulation:
        channels_sent.append("external_webhook (演練模式已安全跳過外部推播)")

    return {
        "status": "dispatched_simulation" if is_simulation else "dispatched",
        "auto_sent_without_human_approval": True,
        "alert_id": alert_id,
        "dispatched_at": dispatched_at,
        "is_simulation": is_simulation,
        "channels": channels_sent,
        "webhook_configured": bool(webhook_url),
        "warning": webhook_error or None,
        "message_preview": message_text,
    }
