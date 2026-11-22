"""獨立測試腳本：驗證災害公開 API、歷史演練快照、GPS 定位、避難所查詢與自動通知。

執行方式：
    # 測試 2024 凱米颱風 + GPS（高雄三民區）
    .venv\\Scripts\\python.exe test_tools.py --scenario 2024_gaemi_typhoon --lat 22.6465 --lon 120.3208

    # 測試即時公開 API
    .venv\\Scripts\\python.exe test_tools.py 新北市
"""

import argparse
import json
import os
import sys

from tools import (
    find_nearest_shelters,
    get_evacuation_route,
    get_flood_warnings,
    get_ncdr_alerts,
    get_rainfall,
    get_river_water_level,
    resolve_gps_location,
    send_evacuation_alert,
)

sys.stdout.reconfigure(encoding="utf-8")


def show_result(title: str, result: dict) -> None:
    print(f"\n=== {title} ===")
    summary = {k: v for k, v in result.items() if k != "data"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    data = result.get("data")
    if isinstance(data, list) and data:
        print("範例第 1 筆：", json.dumps(data[0], ensure_ascii=False)[:400])
    elif isinstance(data, str) and data:
        print("回傳片段：", data[:400])


def main() -> None:
    parser = argparse.ArgumentParser(description="測試防災多 Agent 工具組")
    parser.add_argument("place", nargs="?", default="新北市", help="欲查詢的地區名稱")
    parser.add_argument("--scenario", default="", help="指定歷史災難情境 ID（如 2024_gaemi_typhoon）")
    parser.add_argument("--lat", type=float, default=0.0, help="測試 GPS 緯度（如 22.6465）")
    parser.add_argument("--lon", type=float, default=0.0, help="測試 GPS 經度（如 120.3208）")
    args = parser.parse_args()

    if args.scenario:
        os.environ["DISASTER_SCENARIO"] = args.scenario
        print(f"模式：歷史災難演練（{args.scenario}）")
    else:
        print("模式：即時官方公開 API")

    place = args.place
    if args.lat and args.lon:
        loc = resolve_gps_location(args.lat, args.lon)
        show_result("0. GPS 反向定位", loc)
        if loc.get("status") == "ok" and loc.get("place"):
            place = loc["place"]

    print(f"查詢行政區：{place}")
    show_result("1. 水利署淹水警戒", get_flood_warnings(place))
    show_result("2. 水利署河川水位", get_river_water_level(place))
    show_result("3a. NCDR 淹水警戒", get_ncdr_alerts("淹水", place))
    show_result("3b. NCDR 道路警戒", get_ncdr_alerts("道路", place))
    show_result("4. 氣象署雨量站", get_rainfall(place))

    shelters = find_nearest_shelters(
        latitude=args.lat,
        longitude=args.lon,
        place=place,
        disaster_type="水災",
        limit=3,
    )
    show_result("5. 最近避難收容處所", shelters)

    if args.lat and args.lon and shelters.get("data"):
        top = shelters["data"][0]
        route = get_evacuation_route(
            origin_lat=args.lat,
            origin_lon=args.lon,
            dest_lat=top["緯度"],
            dest_lon=top["經度"],
            dest_name=top["名稱"],
            travel_mode="walking",
            blocked_roads="民族陸橋下,翠華路地下道",
        )
        show_result("6. 前往最近避難所路線規劃", route)
        alert = send_evacuation_alert(
            severity="CRITICAL",
            location=f"{place} ({args.lat}, {args.lon})",
            summary="演練測試：一級淹水警戒與河川警戒。",
            recommended_shelter=top["名稱"],
            shelter_address=top["地址"],
            navigation_url=route.get("google_maps_walking_url", ""),
            action_steps="立即往高處避難並避開封閉地下道。",
        )
        show_result("7. 自動告警發送結果", alert)


if __name__ == "__main__":
    main()
