"""Historical disaster scenario loader & session state controller.

Supports two ways of activating a historical disaster replay:
1. In-conversation switching via Google ADK ToolContext.state["scenario_id"]
   (isolated per chat session in `adk web` / `adk run`).
2. Environment variable `DISASTER_SCENARIO=<scenario_id>` in `.env` or shell.
"""

import json
from pathlib import Path
from typing import Any, Optional

from .common import PROJECT_ROOT, get_env

try:
    from google.adk.tools import ToolContext
except ImportError:  # pragma: no cover
    ToolContext = Any  # type: ignore[misc, assignment]

SCENARIOS_DIR = PROJECT_ROOT / "scenarios"
STATE_KEY = "scenario_id"
LIVE_OVERRIDE = "__LIVE__"


def _load_all_scenarios() -> dict[str, dict[str, Any]]:
    scenarios: dict[str, dict[str, Any]] = {}
    if not SCENARIOS_DIR.exists():
        return scenarios
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sid = data.get("scenario_id") or path.stem
            data["scenario_id"] = sid
            scenarios[sid] = data
        except Exception:
            continue
    return scenarios


def resolve_active_scenario_id(tool_context: Optional[ToolContext] = None) -> str:
    """Return active scenario_id, or empty string when in live official API mode."""
    if tool_context is not None and hasattr(tool_context, "state"):
        state_val = tool_context.state.get(STATE_KEY)
        if state_val == LIVE_OVERRIDE:
            return ""
        if isinstance(state_val, str) and state_val.strip():
            return state_val.strip()

    env_val = get_env("DISASTER_SCENARIO")
    if env_val and env_val != LIVE_OVERRIDE:
        return env_val
    return ""


def get_active_scenario(tool_context: Optional[ToolContext] = None) -> dict[str, Any] | None:
    """Load the currently active scenario dict, or None if in live mode."""
    sid = resolve_active_scenario_id(tool_context)
    if not sid:
        return None
    scenarios = _load_all_scenarios()
    return scenarios.get(sid)


def _match_scenario(query: str, scenarios: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    q = query.strip().lower()
    if not q:
        return None
    if query.strip() in scenarios:
        return scenarios[query.strip()]
    for sid, sc in scenarios.items():
        if q == sid.lower():
            return sc
        haystack = " ".join(
            [
                sid,
                sc.get("title", ""),
                sc.get("simulated_time", ""),
                " ".join(sc.get("keywords", [])),
            ]
        ).lower()
        if q in haystack:
            return sc
    return None


def list_disaster_scenarios(tool_context: Optional[ToolContext] = None) -> dict:
    """列出目前可用的歷史災難演練情境，以及系統現在處於即時模式或演練模式。

    Returns:
        目前模式與所有可切換的歷史災難情境清單。
    """
    scenarios = _load_all_scenarios()
    active_id = resolve_active_scenario_id(tool_context)
    items = [
        {
            "scenario_id": sid,
            "標題": sc.get("title", sid),
            "模擬時間": sc.get("simulated_time", ""),
            "說明": sc.get("description", ""),
            "推薦測試地點": sc.get("recommended_test_places", []),
        }
        for sid, sc in scenarios.items()
    ]
    return {
        "status": "ok",
        "current_mode": "simulation" if active_id else "live",
        "active_scenario_id": active_id or None,
        "available_scenarios": items,
    }


def set_disaster_scenario(
    scenario_query: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """切換至指定的歷史災難時間點演練模式。切換後，所有災害查詢工具都會改回傳該災難時間點的資料。

    Args:
        scenario_query: 情境代碼或關鍵字，例如「2024_gaemi_typhoon」、「凱米颱風」或「凱米」。

    Returns:
        切換結果、模擬災難時間與推薦測試地點。
    """
    scenarios = _load_all_scenarios()
    matched = _match_scenario(scenario_query, scenarios)
    if not matched:
        return {
            "status": "error",
            "error": f"找不到符合「{scenario_query}」的歷史災難情境。",
            "available_scenario_ids": list(scenarios.keys()),
        }

    sid = matched["scenario_id"]
    if tool_context is not None and hasattr(tool_context, "state"):
        tool_context.state[STATE_KEY] = sid

    return {
        "status": "ok",
        "mode": "simulation",
        "scenario_id": sid,
        "title": matched.get("title", sid),
        "simulated_time": matched.get("simulated_time", ""),
        "description": matched.get("description", ""),
        "recommended_test_places": matched.get("recommended_test_places", []),
        "message": (
            f"已切換至歷史災難演練模式：{matched.get('title', sid)}"
            f"（模擬時間：{matched.get('simulated_time', '')}）。"
            "現在你可以假裝身處災區提問，隨時說「切回即時模式」即可恢復查詢真實即時資料。"
        ),
    }


def reset_to_live_mode(tool_context: Optional[ToolContext] = None) -> dict:
    """結束歷史災難演練模式，切回查詢台灣官方即時災害公開 API。

    Returns:
        切換回即時模式的確認訊息。
    """
    if tool_context is not None and hasattr(tool_context, "state"):
        tool_context.state[STATE_KEY] = LIVE_OVERRIDE

    return {
        "status": "ok",
        "mode": "live",
        "active_scenario_id": None,
        "message": "已切回「即時官方資料模式」，接下來所有查詢都會即時連線台灣官方災害 API。",
    }
