"""Proactive Monitoring Agent: scheduled official hazard patrol & automatic resident push alerts.

How it works:
1. Residents register their location (GPS + place), alert threshold (default: CRITICAL =
   一級淹水 / 一級河川 / 土石流紅色警戒 / IoT 積水 >= 30cm; optional HIGH), and optional personal webhook.
2. On a timer (or on-demand), `run_patrol_cycle` scans every registered resident against
   live official Taiwan disaster APIs (or a chosen historical replay scenario) with ZERO
   LLM token cost and deterministic risk grading.
3. Whenever a resident's area escalates to or above their threshold (avoiding duplicate
   spam via level-escalation tracking + cooldown), the agent automatically computes their
   nearest safe shelter + Google Maps navigation URL and dispatches an alert immediately.
"""

from datetime import timedelta
import json
import threading
from typing import Any
import uuid

import requests

from .common import PROJECT_ROOT, now_taiwan, now_taiwan_iso, parse_iso_datetime
from .disaster_apis import collect_location_hazards
from .line_bot import build_evacuation_flex_message, push_line_messages
from .location import normalize_taiwan_coords, resolve_gps_location
from .notifications import send_evacuation_alert
from .risk_engine import LEVEL_ORDER, grade_hazards
from .scenarios import LIVE_OVERRIDE, STATE_KEY
from .shelters import find_nearest_shelters

SUBSCRIBERS_PATH = PROJECT_ROOT / "logs" / "subscribers.json"
RENOTIFY_COOLDOWN_MINUTES = 60

_sub_lock = threading.Lock()


class _PatrolToolContext:
    """Lightweight context carrying live/simulation scenario state for patrol runs."""

    def __init__(self, scenario_id: str) -> None:
        self.state: dict[str, Any] = {
            STATE_KEY: scenario_id.strip() if scenario_id.strip() else LIVE_OVERRIDE
        }


def _read_subscribers_unlocked() -> list[dict[str, Any]]:
    if not SUBSCRIBERS_PATH.exists():
        return []
    try:
        data = json.loads(SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_subscribers_unlocked(items: list[dict[str, Any]]) -> None:
    SUBSCRIBERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUBSCRIBERS_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_subscribers() -> list[dict[str, Any]]:
    """Return all registered residents for proactive disaster monitoring."""
    with _sub_lock:
        return list(_read_subscribers_unlocked())


def add_subscriber(
    name: str,
    latitude: float = 0.0,
    longitude: float = 0.0,
    place: str = "",
    min_level: str = "CRITICAL",
    webhook_url: str = "",
    line_user_id: str = "",
) -> dict[str, Any]:
    """Register a resident for proactive background monitoring & automatic push alerts."""
    clean_name = (name or "").strip() or "已登錄民眾"
    lvl = (min_level or "CRITICAL").strip().upper()
    if lvl not in ("HIGH", "CRITICAL"):
        lvl = "CRITICAL"

    has_gps = bool(latitude and longitude and abs(latitude) > 1 and abs(longitude) > 1)
    lat, lon = (0.0, 0.0)
    resolved_place = place.strip()
    road = ""
    if has_gps:
        lat, lon, _ = normalize_taiwan_coords(float(latitude), float(longitude))
        if not resolved_place:
            loc = resolve_gps_location(lat, lon)
            if loc.get("status") == "ok":
                resolved_place = loc.get("place", "")
                road = loc.get("road", "")

    if not resolved_place and not has_gps:
        raise ValueError("請提供有效的 GPS 經緯度或縣市鄉鎮名稱。")

    record = {
        "id": f"SUB-{uuid.uuid4().hex[:8].upper()}",
        "name": clean_name,
        "place": resolved_place or f"{lat:.4f}, {lon:.4f}",
        "road": road,
        "latitude": round(lat, 6) if has_gps else None,
        "longitude": round(lon, 6) if has_gps else None,
        "min_level": lvl,
        "webhook_url": webhook_url.strip(),
        "line_user_id": line_user_id.strip(),
        "created_at": now_taiwan_iso(),
        "last_checked_at": None,
        "last_risk_level": "LOW",
        "last_notified_level": None,
        "last_notified_at": None,
        "last_alert_id": None,
    }

    with _sub_lock:
        items = _read_subscribers_unlocked()
        # 若同一個 LINE 使用者重複訂閱，更新其最新位置避免重複推播
        if record["line_user_id"]:
            items = [it for it in items if it.get("line_user_id") != record["line_user_id"]]
        items.append(record)
        _write_subscribers_unlocked(items)
    return record


def remove_subscriber_by_line_user(line_user_id: str) -> bool:
    """Remove proactive monitoring subscription for a given LINE userId."""
    if not line_user_id:
        return False
    with _sub_lock:
        items = _read_subscribers_unlocked()
        kept = [it for it in items if it.get("line_user_id") != line_user_id]
        if len(kept) == len(items):
            return False
        _write_subscribers_unlocked(kept)
        return True


def remove_subscriber(subscriber_id: str) -> bool:
    """Remove a registered resident by ID."""
    with _sub_lock:
        items = _read_subscribers_unlocked()
        kept = [it for it in items if it.get("id") != subscriber_id]
        if len(kept) == len(items):
            return False
        _write_subscribers_unlocked(kept)
        return True


def _should_notify(
    sub: dict[str, Any],
    new_level: str,
    force_notify: bool,
) -> tuple[bool, str]:
    min_level = sub.get("min_level", "CRITICAL")
    if LEVEL_ORDER.get(new_level, 0) < LEVEL_ORDER.get(min_level, 3):
        return False, f"未達推播門檻（目前 {new_level}，門檻 {min_level}）"

    if force_notify:
        return True, f"達推播門檻 {new_level}（手動即時巡檢觸發）"

    prev_notified = sub.get("last_notified_level")
    if not prev_notified:
        return True, f"首次偵測達到 {new_level} 警戒，自動觸發推播"

    if LEVEL_ORDER.get(new_level, 0) > LEVEL_ORDER.get(prev_notified, 0):
        return True, f"警戒升級（{prev_notified} → {new_level}），自動觸發推播"

    last_at = parse_iso_datetime(sub.get("last_notified_at") or "")
    if last_at is None or (now_taiwan() - last_at) >= timedelta(minutes=RENOTIFY_COOLDOWN_MINUTES):
        return True, f"持續維持 {new_level} 警戒超過 {RENOTIFY_COOLDOWN_MINUTES} 分鐘，重新提醒"

    return False, f"已於 {sub.get('last_notified_at')} 推播過同等級 {new_level}（冷卻去重中）"


def run_patrol_cycle(
    scenario_id: str = "",
    force_notify: bool = False,
) -> dict[str, Any]:
    """Execute one full proactive patrol cycle across all registered subscribers."""
    started_at = now_taiwan_iso()
    ctx = _PatrolToolContext(scenario_id)
    is_simulation = bool(scenario_id.strip())

    with _sub_lock:
        subscribers = _read_subscribers_unlocked()

    if not subscribers:
        return {
            "status": "ok",
            "mode": "simulation" if is_simulation else "live",
            "scenario_id": scenario_id or None,
            "ran_at": started_at,
            "checked_count": 0,
            "triggered_count": 0,
            "alerts_sent": [],
            "results": [],
            "message": "目前尚未登錄任何主動監測民眾，請先登錄至少一個監測位置。",
        }

    results: list[dict[str, Any]] = []
    alerts_sent: list[dict[str, Any]] = []
    updated_map: dict[str, dict[str, Any]] = {}

    for sub in subscribers:
        lat = float(sub.get("latitude") or 0.0)
        lon = float(sub.get("longitude") or 0.0)
        place = str(sub.get("place") or "")

        bundle = collect_location_hazards(
            place=place,
            latitude=lat,
            longitude=lon,
            tool_context=ctx,  # type: ignore[arg-type]
        )
        risk = grade_hazards(bundle)
        new_level = risk["risk_level"]
        notify, notify_reason = _should_notify(sub, new_level, force_notify=force_notify)

        alert_res = None
        personal_webhook_status = None
        top_shelter = None

        if notify:
            primary_dtype = ""
            for dt in risk["disaster_types"]:
                if dt and dt != "無明顯災害":
                    primary_dtype = dt
                    break

            shelters_res = find_nearest_shelters(
                latitude=lat,
                longitude=lon,
                place=bundle.get("query_place") or place,
                disaster_type=primary_dtype,
                limit=1,
            )
            if shelters_res.get("data"):
                top_shelter = shelters_res["data"][0]

            nav_url = (
                top_shelter.get("google_maps_導航連結")
                or top_shelter.get("google_maps_地圖連結")
                or ""
            ) if top_shelter else ""

            loc_label = (
                f"{sub['name']}｜{bundle.get('query_place') or place}"
                + (f" ({lat:.4f}, {lon:.4f})" if lat and lon else "")
            )
            summary = "；".join(risk["reasons"][:3])
            alert_res = send_evacuation_alert(
                severity=new_level,
                location=loc_label,
                summary=f"[主動巡檢自動發現] {summary}",
                recommended_shelter=top_shelter["名稱"] if top_shelter else "",
                shelter_address=top_shelter["地址"] if top_shelter else "",
                navigation_url=nav_url,
                action_steps=risk["strategy"],
                tool_context=ctx,  # type: ignore[arg-type]
            )

            # 若該民眾有填寫個人 Webhook，且非演練模式，一併自動推播給他個人
            personal_wh = (sub.get("webhook_url") or "").strip()
            if personal_wh and not is_simulation:
                try:
                    msg = alert_res["message_preview"]
                    r = requests.post(
                        personal_wh,
                        json={"content": msg, "text": msg, "subscriber": sub["name"], "alert": alert_res},
                        timeout=10,
                    )
                    r.raise_for_status()
                    personal_webhook_status = "sent"
                except Exception as e:
                    personal_webhook_status = f"failed: {e}"
            elif personal_wh and is_simulation:
                personal_webhook_status = "skipped_in_simulation"

            # 若該民眾綁定了 LINE userId，自動 Push 避難卡片到他的 LINE（演練模式不誤推真實用戶）
            line_uid = (sub.get("line_user_id") or "").strip()
            line_push_status = None
            if line_uid and not is_simulation:
                flex = build_evacuation_flex_message(
                    risk_level=new_level,
                    place=bundle.get("query_place") or place,
                    reasons=risk["reasons"],
                    strategy=risk["strategy"],
                    shelter=top_shelter,
                    navigation_url=nav_url,
                    blocked_roads=risk.get("blocked_roads") or [],
                    alert_id=alert_res["alert_id"],
                    is_simulation=False,
                    is_proactive_push=True,
                )
                push_res = push_line_messages(line_uid, [flex])
                line_push_status = push_res.get("status")
            elif line_uid and is_simulation:
                line_push_status = "skipped_in_simulation"

            alerts_sent.append(
                {
                    "subscriber_id": sub["id"],
                    "subscriber_name": sub["name"],
                    "place": bundle.get("query_place") or place,
                    "risk_level": new_level,
                    "alert_id": alert_res["alert_id"],
                    "dispatched_at": alert_res["dispatched_at"],
                    "is_simulation": alert_res["is_simulation"],
                    "recommended_shelter": top_shelter["名稱"] if top_shelter else "",
                    "navigation_url": nav_url,
                    "personal_webhook_status": personal_webhook_status,
                    "line_push_status": line_push_status,
                    "message_preview": alert_res["message_preview"],
                }
            )

        sub_copy = dict(sub)
        sub_copy["last_checked_at"] = started_at
        sub_copy["last_risk_level"] = new_level
        if notify and alert_res:
            sub_copy["last_notified_level"] = new_level
            sub_copy["last_notified_at"] = alert_res["dispatched_at"]
            sub_copy["last_alert_id"] = alert_res["alert_id"]
        elif LEVEL_ORDER.get(new_level, 0) < LEVEL_ORDER.get(sub.get("min_level", "CRITICAL"), 3):
            # 解除警戒時重設上次推播等級，確保下次再升級時能立即通知
            sub_copy["last_notified_level"] = None
        updated_map[sub["id"]] = sub_copy

        results.append(
            {
                "subscriber_id": sub["id"],
                "name": sub["name"],
                "place": bundle.get("query_place") or place,
                "min_level": sub.get("min_level", "CRITICAL"),
                "risk_level": new_level,
                "disaster_types": risk["disaster_types"],
                "reasons": risk["reasons"][:3],
                "notified": notify,
                "notify_reason": notify_reason,
                "alert_id": alert_res["alert_id"] if alert_res else None,
                "recommended_shelter": top_shelter["名稱"] if top_shelter else None,
                "navigation_url": (
                    (top_shelter.get("google_maps_導航連結") or top_shelter.get("google_maps_地圖連結"))
                    if top_shelter
                    else None
                ),
            }
        )

    # 寫回更新後的巡檢與推播狀態（保留寫入期間新增的訂閱者）
    with _sub_lock:
        latest = _read_subscribers_unlocked()
        merged = [updated_map.get(item["id"], item) for item in latest]
        _write_subscribers_unlocked(merged)

    return {
        "status": "ok",
        "mode": "simulation" if is_simulation else "live",
        "scenario_id": scenario_id or None,
        "ran_at": started_at,
        "checked_count": len(results),
        "triggered_count": len(alerts_sent),
        "alerts_sent": alerts_sent,
        "results": results,
    }


# ---------- 供 Google ADK 對話介面呼叫的精簡工具 ----------

def register_proactive_alert(
    name: str,
    place: str = "",
    latitude: float = 0.0,
    longitude: float = 0.0,
    min_level: str = "CRITICAL",
) -> dict:
    """幫使用者登錄主動式防災監測推播：背景定時巡檢官方警戒，升級達標時自動推播撤離指引。

    Args:
        name: 民眾稱呼或地點標籤（例如「王小明（高雄三民家）」）。
        place: 縣市或鄉鎮市區名稱（例如「高雄市三民區」）。
        latitude: 選填，居家或當前 GPS 緯度。
        longitude: 選填，居家或當前 GPS 經度。
        min_level: 觸發門檻，「CRITICAL」（一級淹水／一級河川／土石流紅色警戒，預設）或「HIGH」（含二級／黃色警戒）。

    Returns:
        登錄成功的訂閱資料。
    """
    rec = add_subscriber(
        name=name,
        latitude=latitude,
        longitude=longitude,
        place=place,
        min_level=min_level,
    )
    return {
        "status": "ok",
        "message": f"已為「{rec['name']}」（{rec['place']}）啟用主動式監測推播（門檻：{rec['min_level']}）。",
        "subscriber": rec,
    }
