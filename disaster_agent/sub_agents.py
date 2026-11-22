"""Specialized 3-stage disaster response pipeline with high-reliability fallback.

Pipeline stages (executed deterministically via Google ADK SequentialAgent):
1. hazard_monitor_agent     -> Deterministic BaseAgent: GPS/place extraction + 7 official APIs (0 LLM calls)
2. risk_assessment_agent    -> Deterministic BaseAgent: Taiwan official risk grading (0 LLM calls)
3. evacuation_planner_agent -> Deterministic shelter/route/auto-alert + 1-call LLM synthesis
                               with automatic multi-model fallback AND deterministic zero-downtime fallback!
"""

import asyncio
from collections.abc import AsyncGenerator
import os
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from tools import (
    collect_location_hazards,
    find_shelters_and_route,
    grade_hazards,
    send_evacuation_alert,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 免費方案多模型備援順序（各模型有各自獨立的每日配額池）
DEFAULT_FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
]

TAIWAN_PLACE_PATTERN = re.compile(
    r"(?:臺北市|台北市|新北市|桃園市|臺中市|台中市|臺南市|台南市|高雄市|基隆市|新竹市|嘉義市|"
    r"新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|屏東縣|宜蘭縣|花蓮縣|臺東縣|台東縣|澎湖縣|金門縣|連江縣|"
    r"台北|臺北|新北|桃園|台中|臺中|台南|臺南|高雄|基隆|屏東|宜蘭|花蓮|台東|臺東)"
    r"(?:[\u4e00-\u9fff]{1,4}(?:區|鄉|鎮|市))?"
)

GPS_PATTERN = re.compile(r"(2[1-6]\.\d{2,7})\s*[,，\s]\s*(1[12][0-9]\.\d{2,7})")
GPS_SWAPPED_PATTERN = re.compile(r"(1[12][0-9]\.\d{2,7})\s*[,，\s]\s*(2[1-6]\.\d{2,7})")


def _get_fallback_models() -> list[str]:
    models: list[str] = []
    for env_key in ("PLANNER_MODEL", "GEMINI_MODEL"):
        val = os.getenv(env_key, "").strip()
        if val and val not in models:
            models.append(val)
    for m in DEFAULT_FALLBACK_MODELS:
        if m not in models:
            models.append(m)
    return models


def _extract_user_text(ctx: InvocationContext) -> str:
    if not ctx.user_content or not ctx.user_content.parts:
        return ""
    return " ".join(p.text for p in ctx.user_content.parts if getattr(p, "text", None))


def _extract_gps_and_place(text: str) -> tuple[float, float, str]:
    lat, lon = 0.0, 0.0
    m = GPS_PATTERN.search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
    else:
        ms = GPS_SWAPPED_PATTERN.search(text)
        if ms:
            lat, lon = float(ms.group(2)), float(ms.group(1))

    pm = TAIWAN_PLACE_PATTERN.search(text)
    place = pm.group(0) if pm else ""
    return lat, lon, place


class _SessionToolContextAdapter:
    """Adapter exposing session.state so tools read/write ADK session state seamlessly."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state


# ==============================================================================
# Stage 1: 災情監測 Agent（確定性資料蒐集，0 LLM 呼叫、超快且零幻覺）
# ==============================================================================
class HazardMonitorAgent(BaseAgent):
    """Stage 1 agent: resolves GPS/place and queries all 7 official disaster sources."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        user_text = _extract_user_text(ctx)
        lat, lon, place = _extract_gps_and_place(user_text)
        adapter = _SessionToolContextAdapter(dict(ctx.session.state))

        if not (lat and lon) and not place:
            msg = "請提供欲查詢的台灣縣市鄉鎮（例如「高雄三民區」、「新北汐止」）或 GPS 經緯度座標。"
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                branch=ctx.branch,
                content=types.Content(role="model", parts=[types.Part(text=msg)]),
                actions=EventActions(state_delta={"monitoring_report": msg, "last_hazard_bundle": {}}),
            )
            return

        bundle = await asyncio.to_thread(
            collect_location_hazards,
            place,
            lat,
            lon,
            adapter,  # type: ignore[arg-type]
        )

        mode = bundle.get("mode", "live")
        gps = bundle.get("gps_location") or {}
        q_place = bundle.get("query_place") or place

        lines = ["### 【第 1 站：災情監測報告】"]
        if mode == "simulation":
            lines.append(
                f"- **查詢模式**：🧪 歷史演練模式（{bundle.get('scenario_title', '')}｜模擬時間：`{bundle.get('simulated_time', '')}`）"
            )
        else:
            lines.append(f"- **查詢模式**：🟢 即時官方資料模式（查詢時間：`{bundle.get('queried_at', '')}`）")

        if gps and gps.get("latitude") and gps.get("longitude"):
            road_part = f" {gps.get('road')}" if gps.get("road") else ""
            lines.append(
                f"- **定位位置**：{q_place}{road_part}（GPS: `{gps['latitude']}, {gps['longitude']}`｜{gps.get('source', '')}）"
            )
        else:
            lines.append(f"- **查詢地區**：{q_place}")

        def _brief(key: str, label: str) -> None:
            res = bundle.get(key) or {}
            status = res.get("status", "")
            cnt = res.get("count", 0)
            if status == "ok" and cnt > 0:
                data = res.get("data")
                sample = ""
                if isinstance(data, list) and data:
                    first = data[0]
                    if isinstance(first, dict):
                        sample = "；".join(
                            f"{k}={v}" for k, v in list(first.items())[:5] if v not in ("", None)
                        )
                lines.append(f"- **{label}**：查獲 **{cnt} 筆**（{sample}）")
            elif status == "ok":
                lines.append(f"- **{label}**：無生效警戒／無異常")
            else:
                lines.append(f"- **{label}**：{res.get('message') or res.get('error') or '無法取得'}")

        _brief("flood_warnings", "水利署淹水警戒")
        _brief("iot_flood_sensors", "IoT 路面淹水感測器")
        _brief("river_water_level", "水利署河川水位")
        _brief("reservoir_warnings", "水利署水庫放流")
        _brief("landslide_alerts", "農村水保署大崩塌／土石流")
        _brief("ncdr_flood", "NCDR 淹水 CAP")
        _brief("ncdr_debris_flow", "NCDR 土石流 CAP")
        _brief("ncdr_road", "NCDR 道路封閉 CAP")
        _brief("rainfall", "氣象署自動雨量站")

        ow = bundle.get("outdoor_weather") or {}
        if ow.get("status") == "ok" and ow.get("headline"):
            lines.append(f"- **即時天氣概況**：{ow['headline']}")

        report = "\n".join(lines)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(role="model", parts=[types.Part(text=report)]),
            actions=EventActions(
                state_delta={
                    "monitoring_report": report,
                    "last_hazard_bundle": bundle,
                }
            ),
        )


hazard_monitor_agent = HazardMonitorAgent(
    name="hazard_monitor_agent",
    description="負責解析使用者 GPS 座標或地名，並一次查齊 7 大台灣官方災害監測資料（0 LLM 呼叫）。",
)


# ==============================================================================
# Stage 2: 風險判讀 Agent（確定性規則引擎 BaseAgent，0 幻覺且不耗 LLM 配額）
# ==============================================================================
class RiskAssessmentAgent(BaseAgent):
    """Stage 2 agent: grades official hazard data deterministically."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        bundle = ctx.session.state.get("last_hazard_bundle") or {}
        user_text = _extract_user_text(ctx)
        result = grade_hazards(bundle, user_text)

        mode = bundle.get("mode", "live")
        if mode == "simulation":
            mode_line = f"歷史演練模式（{bundle.get('scenario_title', '')}｜模擬時間：{bundle.get('simulated_time', '')}）"
        else:
            mode_line = f"即時官方資料模式（查詢時間：{bundle.get('queried_at', '')}）"

        gps = bundle.get("gps_location") or {}
        place = bundle.get("query_place", "未提供")
        gps_str = (
            f"{place}（道路：{gps.get('road') or '無'}｜GPS：{gps['latitude']}, {gps['longitude']}）"
            if gps.get("latitude") and gps.get("longitude")
            else place
        )

        blocked_text = (
            "\n".join(f"  - {r}" for r in result["blocked_roads"])
            if result["blocked_roads"]
            else "  - 無"
        )
        reasons_text = "\n".join(f"  - {r}" for r in result["reasons"])
        auto_alert_text = (
            "是（將自動發送撤離告警，無需人工審核）"
            if result["should_auto_alert"]
            else "否（目前未達自動告警門檻）"
        )

        report = (
            "### 【第 2 站：風險判讀結果（規則引擎）】\n"
            f"1. **查詢模式**：{mode_line}\n"
            f"2. **使用者位置與 GPS**：{gps_str}\n"
            f"3. **風險等級**：`{result['risk_level']}`\n"
            f"4. **主要災害類型**：{'、'.join(result['disaster_types'])}\n"
            f"5. **避難策略建議**：{result['strategy']}\n"
            f"6. **必須避開的封閉或高風險路段**：\n{blocked_text}\n"
            f"7. **是否觸發自動告警通知**：{auto_alert_text}\n"
            f"8. **判讀依據**：\n{reasons_text}"
        )

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(role="model", parts=[types.Part(text=report)]),
            actions=EventActions(
                state_delta={
                    "risk_assessment": report,
                    "risk_assessment_data": result,
                }
            ),
        )


risk_assessment_agent = RiskAssessmentAgent(
    name="risk_assessment_agent",
    description="以台灣官方防災基準執行確定性風險分級（LOW/MEDIUM/HIGH/CRITICAL）、整理封閉路段並決定是否觸發自動撤離告警。",
)


# ==============================================================================
# Stage 3: 撤離規劃與自動通知 Agent（1 次 LLM 潤飾 + 多模型切換 + 零中斷規則備援）
# ==============================================================================
def _build_deterministic_evacuation_briefing(
    bundle: dict[str, Any],
    risk: dict[str, Any],
    shelters_bundle: dict[str, Any],
    alert_res: dict[str, Any] | None,
    llm_fallback_note: str = "",
) -> str:
    lines: list[str] = []
    if bundle.get("mode") == "simulation":
        lines.append(
            f"🧪 【歷史災難演練模式｜模擬時間：{bundle.get('simulated_time', '')}｜{bundle.get('scenario_title', '')}】\n"
        )

    level = risk.get("risk_level", "LOW")
    level_zh = {
        "CRITICAL": "極高風險（立即避難／撤離）",
        "HIGH": "高風險（建議預防性撤離）",
        "MEDIUM": "中度風險（提高警覺）",
        "LOW": "低風險（目前無生效警戒）",
    }.get(level, level)

    place = bundle.get("query_place") or "查詢地點"
    gps = bundle.get("gps_location") or {}
    road = f" {gps.get('road')}" if gps.get("road") else ""

    lines.append("### 🚨 風險等級與災情結論")
    lines.append(f"- **當前位置**：{place}{road}")
    lines.append(f"- **風險等級**：`{level}` — **{level_zh}**")
    lines.append(f"- **主要威脅**：{'、'.join(risk.get('disaster_types') or ['無'])}")
    lines.append(f"- **避難策略**：{risk.get('strategy', '')}")
    for r in risk.get("reasons") or []:
        lines.append(f"  - {r}")

    if alert_res:
        lines.append("\n### 🔔 自動告警通知狀態（免人工審核）")
        lines.append(f"- **發送狀態**：✅ 已由系統自動發送（`{alert_res.get('status')}`）")
        lines.append(f"- **告警編號**：`{alert_res.get('alert_id')}`（時間：{alert_res.get('dispatched_at')}）")
        lines.append(f"- **發送管道**：{'、'.join(alert_res.get('channels') or [])}")

    shelters = (shelters_bundle.get("shelters") or {}).get("data") or []
    route = shelters_bundle.get("primary_route") or {}

    if shelters:
        lines.append("\n### 🏫 推薦最近避難收容處所")
        for idx, s in enumerate(shelters[:3], start=1):
            tag = "首選" if idx == 1 else f"備選 {idx - 1}"
            dist = s.get("距離與方位") or ""
            phone = f"｜☎️ `{s['管理人電話']}`" if s.get("管理人電話") else ""
            lines.append(
                f"{idx}. **{tag}：{s['名稱']}**（{dist}）\n"
                f"   - 地址：{s.get('地址') or s.get('縣市及鄉鎮市區')}｜容納 {s.get('預計收容人數', 0)} 人｜適用：{s.get('適用災害類別', '')}{phone}"
            )

    if route:
        lines.append(f"\n### 🗺️ 怎麼去最近避難所（{route.get('destination', '')}）")
        lines.append(
            f"- **預估距離與步行時間**：{route.get('route_distance_text', '')}（保守步行約 **{route.get('estimated_minutes', '?')} 分鐘**）"
        )
        walk_url = route.get("google_maps_walking_url")
        drive_url = route.get("google_maps_driving_url")
        if walk_url:
            lines.append(f"- 🚶 [開啟 Google Maps 步行導航]({walk_url})")
        if drive_url:
            lines.append(f"- 🚗 [開啟 Google Maps 開車導航]({drive_url})")
        steps = route.get("steps") or []
        if steps:
            lines.append("- **逐步街道指引**：")
            for i, step in enumerate(steps[:8], start=1):
                lines.append(f"  {i}. {step}")
        for w in route.get("blocked_road_warnings") or []:
            lines.append(f"- {w}")

    blocked = risk.get("blocked_roads") or []
    if blocked:
        lines.append("\n### 🚧 必須避開的封閉或高風險路段")
        for b in blocked:
            lines.append(f"- {b}")

    lines.append("\n### 🔴 立刻要做的事 vs ❌ 絕對不要做的事")
    if level in ("CRITICAL", "HIGH"):
        lines.append("- **🔴 立刻要做**：攜帶緊急避難包與手機行動電源；若室外積水過深或達小腿以上，優先**垂直避難往二樓以上移動**；撤離前安全關閉總電源與瓦斯。")
        lines.append("- **❌ 絕對不要**：切勿進入地下室牽車、切勿強行涉水或開車通過積水地下道與封閉山區道路、遠離溪流河岸與邊坡。")
    else:
        ow = bundle.get("outdoor_weather") or {}
        if ow.get("status") == "ok":
            lines.append("\n### 🌤️ 日常戶外活動與穿著提示")
            if ow.get("headline"):
                lines.append(f"- **目前天氣**：{ow['headline']}")
            for tip in ow.get("outdoor_tips") or []:
                lines.append(f"- {tip}")
        lines.append("- **🔴 日常防災準備**：目前無需緊急撤離，可順手記住上方最近避難收容處所位置，留意天候變化即可。")

    lines.append("\n### 📊 資料來源與查詢時間")
    lines.append(
        f"- 經濟部水利署（淹水／河川／水庫）、農業部農村水保署、NCDR 民生示警、內政部消防署全台避難收容處所資料庫、OSRM 路網規劃（時間：`{bundle.get('queried_at', '')}`）"
    )
    if llm_fallback_note:
        lines.append(f"\n> ℹ️ *{llm_fallback_note}*")
    lines.append("\n---")
    lines.append("⚠️ **緊急提醒**：撤離請以地方政府與村里長發布的正式命令為準；遇到立即生命危險請撥打 **119**（或 110）。")
    return "\n".join(lines)


def _try_llm_polish(
    user_text: str,
    monitoring_report: str,
    risk_assessment: str,
    deterministic_briefing: str,
) -> tuple[str | None, str]:
    """Use 1 single Gemini call (with multi-model quota fallback) to personalize the briefing."""
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return None, "未設定 GOOGLE_API_KEY，已使用高可靠規則引擎產出完整指引。"

    client = genai.Client(api_key=api_key)
    prompt = (
        "你是台灣的防災撤離專家。下方已經由官方資料監測、風險規則引擎、全台避難所系統與自動告警系統產出完整事實與指引。\n"
        "請針對使用者的提問，保留**所有關鍵數據、演練模式標示（若有）、自動告警編號、日常戶外與穿著提示（若風險為低或中，務必親切呈現天氣／雨具／保暖／防曬等實用建議）、避難所名稱地址電話、逐步街道指引、Google Maps 導航連結（保持 Markdown 超連結格式）、封閉路段與 119 提醒**，"
        "整理成最清楚、有同理心、可直接照做的繁體中文回覆，絕對不要捏造任何額外數據。\n\n"
        f"【使用者提問】\n{user_text}\n\n"
        f"【監測與風險摘要】\n{risk_assessment}\n\n"
        f"【已驗證避難規劃與自動告警事實】\n{deterministic_briefing}"
    )

    last_err = ""
    for model_name in _get_fallback_models():
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2),
            )
            text = (resp.text or "").strip()
            if text:
                return text, model_name
        except Exception as e:
            last_err = f"{model_name}: {str(e)[:120]}"
            continue
    return None, f"AI 語言潤飾暫達免費配額或高負載（已自動切換至高可靠規則引擎模式，官方數據、避難所路線與自動告警均完整生效）。"


class EvacuationPlannerAgent(BaseAgent):
    """Stage 3 agent: deterministic shelter/route/auto-alert + 1-call resilient LLM synthesis."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        bundle = ctx.session.state.get("last_hazard_bundle") or {}
        risk = ctx.session.state.get("risk_assessment_data") or {}
        monitoring_report = ctx.session.state.get("monitoring_report") or ""
        risk_assessment = ctx.session.state.get("risk_assessment") or ""
        user_text = _extract_user_text(ctx)

        if not bundle:
            fallback = "請先提供台灣縣市鄉鎮或 GPS 經緯度，我就能立即為您查詢災情與最近避難收容處所。"
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                branch=ctx.branch,
                content=types.Content(role="model", parts=[types.Part(text=fallback)]),
                actions=EventActions(state_delta={"evacuation_plan": fallback}),
            )
            return

        gps = bundle.get("gps_location") or {}
        lat = float(gps.get("latitude") or 0.0)
        lon = float(gps.get("longitude") or 0.0)
        place = str(bundle.get("query_place") or "")

        primary_dtype = ""
        for dt in risk.get("disaster_types") or []:
            if dt and dt != "無明顯災害":
                primary_dtype = dt
                break

        blocked_str = "、".join(risk.get("blocked_roads") or [])
        shelters_bundle = await asyncio.to_thread(
            find_shelters_and_route,
            lat,
            lon,
            place,
            primary_dtype,
            blocked_str,
            3,
        )

        # 高風險或極高風險：100% 自動發送告警（免人工確認，絕不因 LLM 配額失敗而漏發）
        alert_res = None
        if risk.get("should_auto_alert"):
            top_shelters = (shelters_bundle.get("shelters") or {}).get("data") or []
            top = top_shelters[0] if top_shelters else {}
            route = shelters_bundle.get("primary_route") or {}
            nav_url = (
                route.get("google_maps_walking_url")
                or top.get("google_maps_導航連結")
                or top.get("google_maps_地圖連結")
                or ""
            )
            loc_label = f"{place} ({lat:.4f}, {lon:.4f})" if (lat and lon) else place
            adapter = _SessionToolContextAdapter(dict(ctx.session.state))
            alert_res = await asyncio.to_thread(
                send_evacuation_alert,
                risk.get("risk_level", "HIGH"),
                loc_label,
                "；".join((risk.get("reasons") or [])[:3]),
                top.get("名稱", ""),
                top.get("地址", ""),
                nav_url,
                risk.get("strategy", ""),
                adapter,  # type: ignore[arg-type]
            )

        base_briefing = _build_deterministic_evacuation_briefing(
            bundle=bundle,
            risk=risk,
            shelters_bundle=shelters_bundle,
            alert_res=alert_res,
        )

        # 僅用 1 次 LLM 呼叫潤飾，若達 429/503 自動無縫降級為高可靠規則引擎回覆
        polished, note_or_model = await asyncio.to_thread(
            _try_llm_polish,
            user_text,
            monitoring_report,
            risk_assessment,
            base_briefing,
        )
        if polished:
            final_text = polished
        else:
            final_text = _build_deterministic_evacuation_briefing(
                bundle=bundle,
                risk=risk,
                shelters_bundle=shelters_bundle,
                alert_res=alert_res,
                llm_fallback_note=note_or_model,
            )

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(role="model", parts=[types.Part(text=final_text)]),
            actions=EventActions(state_delta={"evacuation_plan": final_text}),
        )


evacuation_planner_agent = EvacuationPlannerAgent(
    name="evacuation_planner_agent",
    description="查詢最近合適避難收容處所、規劃避開封閉路段的 GPS 撤離路線與導航連結、於高風險自動發送告警，並具備多模型配額切換與零中斷規則備援。",
)

# ==============================================================================
# 3-Stage Deterministic Multi-Agent Pipeline
# ==============================================================================
disaster_response_pipeline = SequentialAgent(
    name="disaster_response_pipeline",
    description=(
        "依序執行【1. 災情監測 → 2. 風險判讀 → 3. 撤離規劃與自動通知】三階段多 Agent 協作流水線。"
        "只要使用者提供地點或 GPS 查詢災情、風險、避難收容所或撤離路線，都應委派此流水線處理。"
    ),
    sub_agents=[
        hazard_monitor_agent,
        risk_assessment_agent,
        evacuation_planner_agent,
    ],
)
