# 台灣防災多 Agent GPS 撤離導航系統（Google ADK 版）

使用 [Google ADK](https://google.github.io/adk-docs/)、Gemini 模型與 FastAPI + Leaflet 打造的台灣防災多 Agent 撤離系統：
- **專屬自動 GPS 互動地圖網頁**（自動讀取瀏覽器定位、點地圖任意處即查最近官方避難所、繪製真實街道步行路線與 IoT 淹水感測點）
- **三階段多 Agent 協作流水線**：`災情監測` → `確定性規則風險判讀` → `撤離規劃與自動告警（免人工確認）`
- **全台 5,973+ 處官方避難收容處所**（同步自政府資料開放平台 內政部消防署 `#73242`）
- **7 大台灣官方災害資料源（取自 WATCH 清單）＋ 歷史災難重播演練模式**

## 多 Agent 分工架構

```text
disaster_coordinator_agent（總控協調 Agent：即時／歷史演練切換）
  └── disaster_response_pipeline（Google ADK SequentialAgent 三階段接力）
        ├── 1. hazard_monitor_agent     （GPS 反查台灣地址 ＋ 一次查齊 7 大官方災害源）
        ├── 2. risk_assessment_agent    （確定性規則引擎 BaseAgent：0 幻覺、0.001 秒分級）
        └── 3. evacuation_planner_agent （最近避難所 ＋ 街道指引／Google Maps 導航 ＋ 自動告警）
```

## 整合的台灣官方災害與避難資料源

| 資料源 | 說明 | 需要金鑰 |
|---|---|---|
| 水利署 淹水警戒 API | 依即時雨量判定一／二級淹水警戒 | 不用 |
| 水利署 河川水位 API | 即時河川水位與一／二／三級警戒（警戒站優先排序） | 不用 |
| 水利署 水庫放流警戒 API | 水庫調節性放水與洩洪警戒 | 不用 |
| 農村水保署 大崩塌／土石流 API | 大規模崩塌與土石流紅／黃色警戒 | 不用 |
| 民生公共物聯網 IoT 淹水感測器 | 路面實測積水公分數與感測座標 | 不用 |
| NCDR 民生示警 CAP | 淹水、土石流、河川水位、道路封閉（自動濾除過期舊示警＋快取防限流） | 選填 |
| 氣象署 自動雨量站 API | 10 分鐘／1 小時／24 小時累積雨量（演練模式免金鑰） | 即時模式要（免費） |
| 內政部消防署 全台避難收容處所 | 5,973 筆全台官方點位（可用 `scripts/sync_shelters.py` 隨時更新） | 不用 |

## 快速開始

### 方式一：啟動「專屬自動 GPS 互動地圖網頁」（最推薦！）
在 CMD 或 PowerShell 執行：
```powershell
cd c:\Users\user\Desktop\agent
.\.venv\Scripts\python.exe web_app.py
```
打開瀏覽器前往 **`http://127.0.0.1:8080`**，系統會自動取得你的瀏覽器 GPS 並在地圖上標出最近避難所與步行路線；點 **「🚀 多 Agent 分析」** 即可執行完整三階段判讀與自動告警。

### 方式二：啟動 Google ADK 原生除錯介面
```powershell
cd c:\Users\user\Desktop\agent
.\.venv\Scripts\adk.exe web
```
打開 `http://127.0.0.1:8000`，左上角選擇 `disaster_agent`。

### 更新全台官方避難收容處所 CSV
```powershell
.\.venv\Scripts\python.exe scripts\sync_shelters.py
```

## 注意事項

- `.env`、`logs/` 與 `docs/WATCH資料來源.xlsx` 均已由 `.gitignore` 排除。
- 本系統僅供輔助參考：撤離請以地方政府、村里長的正式撤離命令為準，緊急狀況請撥 **119**。
