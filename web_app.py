"""專屬防災網頁後端（FastAPI + Google ADK 三階段多 Agent 流水線 + GPS 互動地圖）。

本機啟動方式：
    cd c:\\Users\\user\\Desktop\\agent
    .\\.venv\\Scripts\\python.exe web_app.py
    瀏覽器打開：http://127.0.0.1:8080

雲端部署（Hugging Face Spaces / Render / Cloud Run）：
    自動讀取 HOST (0.0.0.0) 與 PORT，並內建 IP 速率限制與選填通關密碼保護。
"""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, Field
import uvicorn

from disaster_agent.sub_agents import (
    _extract_gps_and_place,
    disaster_response_pipeline,
)
from tools import (
    add_subscriber,
    answer_disaster_question,
    build_evacuation_flex_message,
    build_welcome_messages,
    default_quick_reply,
    find_nearby_cctvs,
    find_nearest_shelters,
    get_evacuation_route,
    get_iot_flood_sensors,
    get_outdoor_weather_tips,
    get_wra_camera_frames,
    is_line_configured,
    list_disaster_scenarios,
    list_subscribers,
    load_shelters,
    remove_subscriber,
    remove_subscriber_by_line_user,
    reply_line_messages,
    resolve_gps_location,
    run_patrol_cycle,
    verify_line_signature,
)
from tools.notifications import ALERTS_LOG_PATH
from tools.scenarios import LIVE_OVERRIDE, STATE_KEY

PROJECT_ROOT = Path(__file__).resolve().parent
WEB_INDEX_PATH = PROJECT_ROOT / "web" / "index.html"
load_dotenv(PROJECT_ROOT / ".env")

APP_NAME = "taiwan_disaster_web"

# 防公開網址被惡意刷爆 Gemini 免費額度（單一 IP 每分鐘最多 4 次、每 10 分鐘最多 15 次多 Agent 分析）
RATE_LIMIT_1M = int(os.getenv("ANALYZE_RATE_LIMIT_1M", "4"))
RATE_LIMIT_10M = int(os.getenv("ANALYZE_RATE_LIMIT_10M", "15"))
_analyze_hits: dict[str, list[float]] = defaultdict(list)


class _SimpleToolContext:
    """Lightweight state carrier so standalone REST tools share scenario state."""

    def __init__(self, scenario_id: str) -> None:
        self.state: dict[str, Any] = {
            STATE_KEY: scenario_id.strip() if scenario_id.strip() else LIVE_OVERRIDE
        }


class NearbyRequest(BaseModel):
    latitude: float
    longitude: float
    scenario_id: str = ""
    disaster_type: str = ""
    limit: int = Field(default=5, ge=1, le=10)


class AnalyzeRequest(BaseModel):
    latitude: float
    longitude: float
    message: str = ""
    scenario_id: str = ""
    access_password: str = ""


class ChatRequest(BaseModel):
    message: str
    latitude: float = 0.0
    longitude: float = 0.0
    place: str = ""
    scenario_id: str = ""
    context_summary: str = ""
    history: list[dict[str, str]] = Field(default_factory=list)
    access_password: str = ""


class SubscriberRequest(BaseModel):
    name: str
    latitude: float = 0.0
    longitude: float = 0.0
    place: str = ""
    min_level: str = "CRITICAL"
    webhook_url: str = ""


class PatrolNowRequest(BaseModel):
    scenario_id: str = ""
    force_notify: bool = False


class PatrolConfigRequest(BaseModel):
    enabled: bool = True
    poll_seconds: int = Field(default=300, ge=30, le=3600)
    scenario_id: str = ""


proactive_state: dict[str, Any] = {
    "enabled": os.getenv("PROACTIVE_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
    "poll_seconds": max(30, int(os.getenv("PROACTIVE_POLL_SECONDS", "300"))),
    "scenario_id": os.getenv("DISASTER_SCENARIO", "").strip(),
    "last_patrol": None,
    "patrol_count": 0,
}

runner = InMemoryRunner(agent=disaster_response_pipeline, app_name=APP_NAME)


async def _proactive_patrol_loop() -> None:
    """Background Proactive Agent: periodically scans official alerts for registered residents."""
    while True:
        await asyncio.sleep(proactive_state["poll_seconds"])
        if not proactive_state["enabled"]:
            continue
        if not list_subscribers():
            continue
        try:
            report = await asyncio.to_thread(
                run_patrol_cycle,
                proactive_state["scenario_id"],
                False,
            )
            proactive_state["last_patrol"] = report
            proactive_state["patrol_count"] += 1
        except Exception as e:
            proactive_state["last_patrol"] = {"status": "error", "error": str(e)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 預載全台 5,985 處避難收容處所至記憶體快取，確保第一次查詢就瞬間回應
    load_shelters()
    patrol_task = asyncio.create_task(_proactive_patrol_loop())
    try:
        yield
    finally:
        patrol_task.cancel()
        with suppress(asyncio.CancelledError):
            await patrol_task
        await runner.close()


app = FastAPI(title="台灣防災多 Agent GPS 撤離導航系統", lifespan=lifespan)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce_rate_limit(ip: str) -> None:
    now = time.monotonic()
    history = [ts for ts in _analyze_hits[ip] if now - ts < 600]
    recent_1m = [ts for ts in history if now - ts < 60]
    if len(recent_1m) >= RATE_LIMIT_1M:
        raise HTTPException(
            status_code=429,
            detail=f"操作過於頻繁：每分鐘最多執行 {RATE_LIMIT_1M} 次多 Agent 分析，請稍候再試。",
        )
    if len(history) >= RATE_LIMIT_10M:
        raise HTTPException(
            status_code=429,
            detail=f"已達暫時上限：每 10 分鐘最多執行 {RATE_LIMIT_10M} 次多 Agent 分析，請稍候再試。",
        )
    history.append(now)
    _analyze_hits[ip] = history


def _read_recent_alerts(limit: int = 15) -> list[dict[str, Any]]:
    if not ALERTS_LOG_PATH.exists():
        return []
    lines = ALERTS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    items: list[dict[str, Any]] = []
    for line in reversed(lines[-limit:]):
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items


def _build_map_payload(
    lat: float,
    lon: float,
    scenario_id: str,
    disaster_type: str = "",
    blocked_roads: str = "",
    limit: int = 5,
) -> dict[str, Any]:
    ctx = _SimpleToolContext(scenario_id)
    loc = resolve_gps_location(lat, lon)
    place = loc.get("place", "") if loc.get("status") == "ok" else ""

    shelters_res = find_nearest_shelters(
        latitude=lat,
        longitude=lon,
        place=place,
        disaster_type=disaster_type,
        limit=limit,
    )
    shelters = shelters_res.get("data") or []

    primary_route = None
    if shelters and shelters[0].get("緯度") and shelters[0].get("經度"):
        top = shelters[0]
        primary_route = get_evacuation_route(
            origin_lat=lat,
            origin_lon=lon,
            dest_lat=top["緯度"],
            dest_lon=top["經度"],
            dest_name=top["名稱"],
            travel_mode="walking",
            blocked_roads=blocked_roads,
        )

    iot_res = get_iot_flood_sensors(
        place=place,
        latitude=lat,
        longitude=lon,
        tool_context=ctx,  # type: ignore[arg-type]
    )
    flood_sensors = iot_res.get("data") if isinstance(iot_res.get("data"), list) else []

    outdoor_weather = get_outdoor_weather_tips(
        latitude=lat,
        longitude=lon,
        place=place,
        tool_context=ctx,  # type: ignore[arg-type]
    )

    cctv_res = find_nearby_cctvs(latitude=lat, longitude=lon, limit=8, max_distance_km=25.0)
    cctvs = cctv_res.get("data") or []

    return {
        "user": {
            "latitude": loc.get("latitude", lat),
            "longitude": loc.get("longitude", lon),
            "place": place,
            "road": loc.get("road", ""),
            "display_name": loc.get("display_name") or place or f"{lat:.5f}, {lon:.5f}",
            "source": loc.get("source", ""),
        },
        "shelters": shelters,
        "shelter_source": shelters_res.get("source", ""),
        "primary_route": primary_route,
        "flood_sensors": flood_sensors,
        "iot_status": iot_res.get("status", ""),
        "iot_message": iot_res.get("message", ""),
        "outdoor_weather": outdoor_weather,
        "cctvs": cctvs,
    }


@app.get("/health")
async def health():
    shelters, _ = load_shelters()
    return {"status": "ok", "shelters_loaded": len(shelters)}


@app.get("/")
async def serve_index():
    if not WEB_INDEX_PATH.exists():
        raise HTTPException(status_code=404, detail="找不到 web/index.html")
    return FileResponse(WEB_INDEX_PATH)


@app.get("/api/bootstrap")
async def api_bootstrap():
    shelters, sources = load_shelters()
    scenarios = list_disaster_scenarios()
    return {
        "shelter_count": len(shelters),
        "shelter_sources": sources,
        "scenarios": scenarios.get("available_scenarios", []),
        "recent_alerts": _read_recent_alerts(10),
        "password_required": bool(os.getenv("ACCESS_PASSWORD", "").strip()),
    }


@app.get("/api/alerts")
async def api_alerts():
    return {"alerts": _read_recent_alerts(20)}


@app.get("/api/cctv/wra/{source_id}/{station_id}")
async def api_wra_cctv_frames(source_id: int, station_id: str):
    return await asyncio.to_thread(get_wra_camera_frames, source_id, station_id)


@app.get("/api/proactive/status")
async def api_proactive_status():
    return {
        "enabled": proactive_state["enabled"],
        "poll_seconds": proactive_state["poll_seconds"],
        "scenario_id": proactive_state["scenario_id"] or None,
        "patrol_count": proactive_state["patrol_count"],
        "last_patrol": proactive_state["last_patrol"],
        "subscribers": list_subscribers(),
        "recent_alerts": _read_recent_alerts(10),
    }


@app.post("/api/proactive/subscribers")
async def api_add_subscriber(req: SubscriberRequest):
    try:
        sub = await asyncio.to_thread(
            add_subscriber,
            req.name,
            req.latitude,
            req.longitude,
            req.place,
            req.min_level,
            req.webhook_url,
        )
        return {"status": "ok", "subscriber": sub, "subscribers": list_subscribers()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/proactive/subscribers/{subscriber_id}")
async def api_remove_subscriber(subscriber_id: str):
    removed = remove_subscriber(subscriber_id)
    if not removed:
        raise HTTPException(status_code=404, detail="找不到該筆登錄資料")
    return {"status": "ok", "subscribers": list_subscribers()}


@app.post("/api/proactive/config")
async def api_proactive_config(req: PatrolConfigRequest):
    proactive_state["enabled"] = req.enabled
    proactive_state["poll_seconds"] = req.poll_seconds
    proactive_state["scenario_id"] = req.scenario_id.strip()
    return {
        "status": "ok",
        "enabled": proactive_state["enabled"],
        "poll_seconds": proactive_state["poll_seconds"],
        "scenario_id": proactive_state["scenario_id"] or None,
    }


@app.post("/api/proactive/patrol-now")
async def api_proactive_patrol_now(req: PatrolNowRequest):
    report = await asyncio.to_thread(
        run_patrol_cycle,
        req.scenario_id,
        req.force_notify,
    )
    proactive_state["last_patrol"] = report
    proactive_state["patrol_count"] += 1
    return {
        "status": "ok",
        "report": report,
        "subscribers": list_subscribers(),
        "recent_alerts": _read_recent_alerts(10),
    }


@app.post("/api/nearby")
async def api_nearby(req: NearbyRequest):
    return _build_map_payload(
        lat=req.latitude,
        lon=req.longitude,
        scenario_id=req.scenario_id,
        disaster_type=req.disaster_type,
        limit=req.limit,
    )


@app.post("/api/chat")
async def api_chat(req: ChatRequest, request: Request):
    """Multi-turn conversational disaster Q&A endpoint for the web UI."""
    required_pw = os.getenv("ACCESS_PASSWORD", "").strip()
    if required_pw and req.access_password.strip() != required_pw:
        raise HTTPException(status_code=401, detail="通關密碼錯誤，請輸入正確密碼後再試。")

    _enforce_rate_limit(_client_ip(request))

    q = req.message.strip()
    if not q:
        raise HTTPException(status_code=400, detail="請輸入問題。")

    # 若使用者在聊天窗直接問某個台灣新地點的即時災情，自動帶入三階段多 Agent 查詢
    lat_in, lon_in, place_in = _extract_gps_and_place(q)
    if place_in or (lat_in and lon_in):
        res = await _run_pipeline_for_line(q, req.scenario_id)
        if res.get("final_reply"):
            return {
                "status": "ok",
                "reply": res["final_reply"],
                "mode": "multi_agent_pipeline",
                "risk_level": (res.get("risk") or {}).get("risk_level"),
            }

    ctx_parts: list[str] = []
    if req.place:
        ctx_parts.append(f"目前地圖位置：{req.place}")
    if req.latitude and req.longitude:
        ctx_parts.append(f"GPS：{req.latitude:.5f}, {req.longitude:.5f}")
    if req.scenario_id:
        ctx_parts.append(f"目前處於歷史演練模式：{req.scenario_id}")
    if req.context_summary:
        ctx_parts.append(req.context_summary[:1500])

    ans = await asyncio.to_thread(
        answer_disaster_question,
        q,
        "\n".join(ctx_parts),
        req.history,
    )
    return {
        "status": "ok",
        "reply": ans["reply"],
        "mode": "disaster_chat",
        "model": ans.get("model", ""),
    }


@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest, request: Request):
    required_pw = os.getenv("ACCESS_PASSWORD", "").strip()
    if required_pw and req.access_password.strip() != required_pw:
        raise HTTPException(status_code=401, detail="通關密碼錯誤，請輸入正確密碼後再試。")

    _enforce_rate_limit(_client_ip(request))

    before_alerts_count = (
        len(ALERTS_LOG_PATH.read_text(encoding="utf-8").splitlines())
        if ALERTS_LOG_PATH.exists()
        else 0
    )

    user_id = "web_user"
    session_id = f"web-{uuid.uuid4().hex[:10]}"
    initial_state = {
        STATE_KEY: req.scenario_id.strip() if req.scenario_id.strip() else LIVE_OVERRIDE
    }
    await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
        state=initial_state,
    )

    extra = req.message.strip()
    prompt = (
        f"我目前的 GPS 位置是 {req.latitude:.6f}, {req.longitude:.6f}。"
        f"{extra if extra else '請問附近現在有沒有災害危險？最近的避難收容處所在哪裡、要怎麼去？'}"
    )
    user_msg = types.Content(role="user", parts=[types.Part(text=prompt)])

    stages: list[dict[str, str]] = []
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_msg,
        ):
            if event.content and event.content.parts:
                text_parts = [
                    p.text for p in event.content.parts if getattr(p, "text", None)
                ]
                if text_parts:
                    stages.append(
                        {
                            "author": event.author or "agent",
                            "text": "\n".join(text_parts).strip(),
                        }
                    )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"多 Agent 分析執行失敗：{e}")

    session = await runner.session_service.get_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )
    state = session.state if session else {}
    risk_data = state.get("risk_assessment_data") or {}
    risk_level = risk_data.get("risk_level", "UNKNOWN")
    disaster_types = risk_data.get("disaster_types") or []
    blocked_roads = risk_data.get("blocked_roads") or []

    primary_disaster = ""
    for dt in disaster_types:
        if dt and dt != "無明顯災害":
            primary_disaster = dt
            break

    map_data = _build_map_payload(
        lat=req.latitude,
        lon=req.longitude,
        scenario_id=req.scenario_id,
        disaster_type=primary_disaster,
        blocked_roads=",".join(blocked_roads),
        limit=5,
    )

    new_alerts: list[dict[str, Any]] = []
    if ALERTS_LOG_PATH.exists():
        all_lines = ALERTS_LOG_PATH.read_text(encoding="utf-8").splitlines()
        for line in all_lines[before_alerts_count:]:
            try:
                new_alerts.append(json.loads(line))
            except Exception:
                continue

    final_reply = ""
    for st in reversed(stages):
        if st["author"] == "evacuation_planner_agent":
            final_reply = st["text"]
            break
    if not final_reply and stages:
        final_reply = stages[-1]["text"]

    return {
        "status": "ok",
        "scenario_id": req.scenario_id or None,
        "risk_level": risk_level,
        "risk_data": risk_data,
        "stages": stages,
        "final_reply": final_reply,
        "auto_alerts": new_alerts,
        "recent_alerts": _read_recent_alerts(10),
        "map_data": map_data,
    }


# 記錄每個 LINE 使用者目前的演練模式與最近一次查詢位置（供「訂閱」快捷鍵使用）
_line_user_prefs: dict[str, dict[str, Any]] = {}


async def _run_pipeline_for_line(
    prompt: str,
    scenario_id: str,
) -> dict[str, Any]:
    """Run our 3-stage ADK disaster pipeline and return structured results for LINE Flex rendering."""
    before_alerts_count = (
        len(ALERTS_LOG_PATH.read_text(encoding="utf-8").splitlines())
        if ALERTS_LOG_PATH.exists()
        else 0
    )
    user_id = "line_user"
    session_id = f"line-{uuid.uuid4().hex[:10]}"
    initial_state = {
        STATE_KEY: scenario_id.strip() if scenario_id.strip() else LIVE_OVERRIDE
    }
    await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
        state=initial_state,
    )
    user_msg = types.Content(role="user", parts=[types.Part(text=prompt)])

    final_reply = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_msg,
    ):
        if event.author == "evacuation_planner_agent" and event.content and event.content.parts:
            parts = [p.text for p in event.content.parts if getattr(p, "text", None)]
            if parts:
                final_reply = "\n".join(parts).strip()

    session = await runner.session_service.get_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )
    state = session.state if session else {}
    bundle = state.get("last_hazard_bundle") or {}
    risk = state.get("risk_assessment_data") or {}

    alert_id = ""
    if ALERTS_LOG_PATH.exists():
        all_lines = ALERTS_LOG_PATH.read_text(encoding="utf-8").splitlines()
        if len(all_lines) > before_alerts_count:
            try:
                alert_id = json.loads(all_lines[-1]).get("alert_id", "")
            except Exception:
                pass

    return {
        "bundle": bundle,
        "risk": risk,
        "final_reply": final_reply,
        "alert_id": alert_id,
    }


def _summarize_line_context(
    place: str,
    risk: dict[str, Any],
    shelter: dict[str, Any] | None,
    outdoor_weather: dict[str, Any] | None,
    is_sim: bool,
) -> str:
    parts = [
        f"模式：{'2024 凱米颱風演練模式' if is_sim else '即時官方資料模式'}",
        f"位置：{place}",
        f"風險等級：{risk.get('risk_level', 'LOW')}（{'、'.join(risk.get('disaster_types') or [])}）",
        f"避難策略：{risk.get('strategy', '')}",
    ]
    if shelter:
        parts.append(
            f"最近避難所：{shelter.get('名稱')}｜{shelter.get('距離與方位', '')}｜地址：{shelter.get('地址', '')}｜電話：{shelter.get('管理人電話', '')}｜容納 {shelter.get('預計收容人數', 0)} 人"
        )
    if outdoor_weather and outdoor_weather.get("headline"):
        parts.append(f"當地天氣：{outdoor_weather['headline']}")
    return "\n".join(parts)


async def _handle_line_event(event: dict[str, Any]) -> None:
    """Process a single LINE webhook event asynchronously and reply via LINE Reply API."""
    reply_token = event.get("replyToken") or ""
    if not reply_token:
        return
    event_type = event.get("type", "")
    line_user_id = (event.get("source") or {}).get("userId", "")
    prefs = _line_user_prefs.setdefault(
        line_user_id or "anonymous",
        {
            "scenario_id": "",
            "last_lat": 0.0,
            "last_lon": 0.0,
            "last_place": "",
            "last_context": "",
            "history": [],
        },
    )

    try:
        if event_type == "follow":
            await asyncio.to_thread(reply_line_messages, reply_token, build_welcome_messages())
            return

        if event_type != "message":
            return

        msg = event.get("message") or {}
        msg_type = msg.get("type", "")
        scenario_id = prefs.get("scenario_id", "")
        is_sim = bool(scenario_id)

        if msg_type == "location":
            lat = float(msg.get("latitude") or 0.0)
            lon = float(msg.get("longitude") or 0.0)
            addr = str(msg.get("address") or "")
            prompt = (
                f"我目前的 GPS 位置是 {lat:.6f}, {lon:.6f}（{addr}）。"
                "請問附近現在有沒有災害危險？最近的避難收容處所在哪裡、要怎麼去？"
            )
            res = await _run_pipeline_for_line(prompt, scenario_id)
            bundle = res["bundle"]
            risk = res["risk"]
            place = bundle.get("query_place") or addr or f"{lat:.4f}, {lon:.4f}"
            prefs["last_lat"] = lat
            prefs["last_lon"] = lon
            prefs["last_place"] = place

            primary_dtype = next(
                (dt for dt in (risk.get("disaster_types") or []) if dt and dt != "無明顯災害"),
                "",
            )
            sh_res = await asyncio.to_thread(
                find_nearest_shelters, lat, lon, place, primary_dtype, 1
            )
            top_shelter = (sh_res.get("data") or [None])[0]
            nav_url = (
                top_shelter.get("google_maps_導航連結")
                or top_shelter.get("google_maps_地圖連結")
                or ""
            ) if top_shelter else ""

            prefs["last_context"] = _summarize_line_context(
                place, risk, top_shelter, bundle.get("outdoor_weather"), is_sim
            )
            flex = build_evacuation_flex_message(
                risk_level=risk.get("risk_level", "LOW"),
                place=place,
                reasons=risk.get("reasons") or [],
                strategy=risk.get("strategy") or "",
                shelter=top_shelter,
                navigation_url=nav_url,
                outdoor_weather=bundle.get("outdoor_weather"),
                blocked_roads=risk.get("blocked_roads") or [],
                alert_id=res["alert_id"],
                is_simulation=is_sim,
                simulated_time=str(bundle.get("simulated_time") or ""),
            )
            await asyncio.to_thread(reply_line_messages, reply_token, [flex])
            return

        if msg_type == "text":
            text = (msg.get("text") or "").strip()
            lower = text.lower()

            if text in ("說明", "幫助", "功能", "你好", "嗨") or lower in ("help", "hi", "hello"):
                await asyncio.to_thread(reply_line_messages, reply_token, build_welcome_messages())
                return

            if "切回即時" in text or "結束演練" in text or text == "即時":
                prefs["scenario_id"] = ""
                await asyncio.to_thread(
                    reply_line_messages,
                    reply_token,
                    [
                        {
                            "type": "text",
                            "text": "🟢 已切回「即時官方資料模式」，請點下方傳送位置或輸入地名查詢最新防災狀態！",
                            "quickReply": default_quick_reply(False),
                        }
                    ],
                )
                return

            if "演練" in text or "凱米" in text:
                prefs["scenario_id"] = "2024_gaemi_typhoon"
                await asyncio.to_thread(
                    reply_line_messages,
                    reply_token,
                    [
                        {
                            "type": "text",
                            "text": (
                                "🧪 已切換至【2024 凱米颱風演練模式（07/25 09:00）】！\n\n"
                                "你可以直接試打：\n"
                                "• 高雄三民區（一級淹水＋愛河警戒＋IoT 42cm＋地下道封閉）\n"
                                "• 屏東三地門鄉（土石流紅色警戒＋台24線封閉）\n"
                                "或點下方「📍 傳送我的位置」測試！"
                            ),
                            "quickReply": default_quick_reply(True),
                        }
                    ],
                )
                return

            if text.startswith("取消訂閱") or "退訂" in text:
                removed = remove_subscriber_by_line_user(line_user_id)
                reply_txt = (
                    "✅ 已取消您的主動防災推播訂閱。"
                    if removed
                    else "您目前尚未訂閱主動防災推播。"
                )
                await asyncio.to_thread(
                    reply_line_messages,
                    reply_token,
                    [{"type": "text", "text": reply_txt, "quickReply": default_quick_reply(is_sim)}],
                )
                return

            if text == "訂閱" or text.startswith("訂閱"):
                lat = float(prefs.get("last_lat") or 0.0)
                lon = float(prefs.get("last_lon") or 0.0)
                place = str(prefs.get("last_place") or "")
                extra_place = text.replace("訂閱", "").strip()
                if extra_place:
                    place = extra_place
                    lat, lon = 0.0, 0.0
                if not place and not (lat and lon):
                    await asyncio.to_thread(
                        reply_line_messages,
                        reply_token,
                        [
                            {
                                "type": "text",
                                "text": "請先點下方「📍 傳送我的位置」，或直接輸入「訂閱 高雄市三民區」，我就會幫你開啟自動警戒推播！",
                                "quickReply": default_quick_reply(is_sim),
                            }
                        ],
                    )
                    return

                sub = await asyncio.to_thread(
                    add_subscriber,
                    f"LINE用戶 ({place or '定位點'})",
                    lat,
                    lon,
                    place,
                    "CRITICAL",
                    "",
                    line_user_id,
                )
                await asyncio.to_thread(
                    reply_line_messages,
                    reply_token,
                    [
                        {
                            "type": "text",
                            "text": (
                                f"✅ 已開啟主動防災推播！\n"
                                f"📍 監測區域：{sub['place']}\n"
                                f"🚨 觸發條件：一級淹水／一級河川／土石流紅色警戒（CRITICAL）\n"
                                f"當該區域升級警戒時，我會自動傳送撤離卡片與最近避難所導航到你的 LINE！\n"
                                "（輸入「取消訂閱」可隨時關閉）"
                            ),
                            "quickReply": default_quick_reply(is_sim),
                        }
                    ],
                )
                return

            # 判斷是否包含台灣地名／GPS：有則跑三階段官方災情流水線，無則進入自由防災問答對話！
            lat_in, lon_in, place_in = _extract_gps_and_place(text)
            if not place_in and not (lat_in and lon_in):
                ans = await asyncio.to_thread(
                    answer_disaster_question,
                    text,
                    str(prefs.get("last_context") or ""),
                    prefs.get("history") or [],
                )
                reply_txt = ans["reply"][:1900]
                hist = prefs.setdefault("history", [])
                hist.append({"role": "user", "text": text})
                hist.append({"role": "assistant", "text": reply_txt})
                prefs["history"] = hist[-10:]
                await asyncio.to_thread(
                    reply_line_messages,
                    reply_token,
                    [{"type": "text", "text": reply_txt, "quickReply": default_quick_reply(is_sim)}],
                )
                return

            # 包含地點／GPS 的提問：交給三階段多 Agent 流水線並回傳 Flex 卡片
            res = await _run_pipeline_for_line(text, scenario_id)
            bundle = res["bundle"]
            risk = res["risk"]
            if not bundle:
                ans = await asyncio.to_thread(
                    answer_disaster_question,
                    text,
                    str(prefs.get("last_context") or ""),
                    prefs.get("history") or [],
                )
                await asyncio.to_thread(
                    reply_line_messages,
                    reply_token,
                    [{"type": "text", "text": ans["reply"][:1900], "quickReply": default_quick_reply(is_sim)}],
                )
                return

            place = bundle.get("query_place") or text
            gps = bundle.get("gps_location") or {}
            lat = float(gps.get("latitude") or 0.0)
            lon = float(gps.get("longitude") or 0.0)
            prefs["last_place"] = place
            if lat and lon:
                prefs["last_lat"] = lat
                prefs["last_lon"] = lon

            primary_dtype = next(
                (dt for dt in (risk.get("disaster_types") or []) if dt and dt != "無明顯災害"),
                "",
            )
            sh_res = await asyncio.to_thread(
                find_nearest_shelters, lat, lon, place, primary_dtype, 1
            )
            top_shelter = (sh_res.get("data") or [None])[0]
            nav_url = (
                top_shelter.get("google_maps_導航連結")
                or top_shelter.get("google_maps_地圖連結")
                or ""
            ) if top_shelter else ""

            prefs["last_context"] = _summarize_line_context(
                place, risk, top_shelter, bundle.get("outdoor_weather"), is_sim
            )
            hist = prefs.setdefault("history", [])
            hist.append({"role": "user", "text": text})
            hist.append({"role": "assistant", "text": f"已為您查詢 {place}（風險：{risk.get('risk_level', 'LOW')}）"})
            prefs["history"] = hist[-10:]

            flex = build_evacuation_flex_message(
                risk_level=risk.get("risk_level", "LOW"),
                place=place,
                reasons=risk.get("reasons") or [],
                strategy=risk.get("strategy") or "",
                shelter=top_shelter,
                navigation_url=nav_url,
                outdoor_weather=bundle.get("outdoor_weather"),
                blocked_roads=risk.get("blocked_roads") or [],
                alert_id=res["alert_id"],
                is_simulation=is_sim,
                simulated_time=str(bundle.get("simulated_time") or ""),
            )
            await asyncio.to_thread(reply_line_messages, reply_token, [flex])
    except Exception as e:
        await asyncio.to_thread(
            reply_line_messages,
            reply_token,
            [
                {
                    "type": "text",
                    "text": f"抱歉，查詢過程中發生暫時錯誤：{str(e)[:120]}\n請稍後再試；緊急危險請立即撥打 119。",
                    "quickReply": default_quick_reply(bool(prefs.get("scenario_id"))),
                }
            ],
        )


@app.post("/api/line/webhook")
async def api_line_webhook(request: Request):
    """LINE Messaging API Webhook endpoint (verifies X-Line-Signature & replies asynchronously)."""
    if not is_line_configured():
        raise HTTPException(
            status_code=503,
            detail="尚未設定 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN（請參考 LINE_SETUP.md）。",
        )
    raw_body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not verify_line_signature(raw_body, signature):
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    payload = json.loads(raw_body.decode("utf-8"))
    for ev in payload.get("events") or []:
        asyncio.create_task(_handle_line_event(ev))

    return {"status": "ok"}


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    print(f"🛡️  台灣防災多 Agent GPS 地圖系統啟動中：http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
