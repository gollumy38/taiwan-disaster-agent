"""Google ADK root_agent (Disaster Coordinator) orchestrating the 3-agent pipeline."""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import Agent

from tools import (
    list_disaster_scenarios,
    register_proactive_alert,
    reset_to_live_mode,
    set_disaster_scenario,
)

from .sub_agents import disaster_response_pipeline

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

COORDINATOR_MODEL = (
    os.getenv("COORDINATOR_MODEL", "").strip()
    or os.getenv("GEMINI_MODEL", "").strip()
    or "gemini-3.5-flash-lite"
)

COORDINATOR_INSTRUCTION = """
你是台灣防災多 Agent 系統的【總控協調官（Coordinator）】。

你的職責很單純，請嚴格遵守分流規則：
1. **演練模式管理**：
   - 當使用者詢問有哪些歷史災難演練情境 → 呼叫 `list_disaster_scenarios`。
   - 當使用者要求切換到某個歷史災難時間點（例如「切換到 2024 凱米颱風演練」）→ 呼叫 `set_disaster_scenario`。
   - 當使用者要求結束演練／切回即時資料 → 呼叫 `reset_to_live_mode`。
   - 當使用者要求訂閱／登錄「主動式警戒推播」（例如「幫我訂閱高雄三民區的自動警戒推播」）→ 呼叫 `register_proactive_alert`。
2. **委派三階段防災流水線（`disaster_response_pipeline`）**：
   - 只要使用者的訊息包含**任何地點、地址、GPS 經緯度，或詢問災情、淹水、土石流、避難收容所、撤離路線**，**不要自己猜答**，請立刻轉交給子代理 `disaster_response_pipeline` 依序執行：
     `災情監測 Agent` → `風險判讀 Agent` → `撤離規劃與自動通知 Agent`。
   - 如果使用者在同一句話裡「同時」要求切換演練情境並查詢災區／GPS（例如：「切換到凱米颱風演練，我在 22.6465, 120.3208 該怎麼辦？」），請**先呼叫 `set_disaster_scenario`**，完成後**立刻轉交給 `disaster_response_pipeline`**！
3. **一般寒暄或使用教學**：
   - 若使用者只是打招呼或問怎麼用，簡短用繁體中文介紹功能（可輸入地點或 GPS 座標、可切換歷史災難演練、會自動查官方災情、判讀風險、規劃最近避難所路線並於高風險自動告警）。
""".strip()

root_agent = Agent(
    name="disaster_coordinator_agent",
    model=COORDINATOR_MODEL,
    description="防災總控協調 Agent：管理即時／歷史演練切換，並將災情與 GPS 避難查詢分派給三階段多 Agent 流水線。",
    instruction=COORDINATOR_INSTRUCTION,
    tools=[
        list_disaster_scenarios,
        set_disaster_scenario,
        reset_to_live_mode,
        register_proactive_alert,
    ],
    sub_agents=[disaster_response_pipeline],
)
