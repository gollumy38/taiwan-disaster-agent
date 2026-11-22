# LINE Bot 免費串接教學（5 分鐘讓手機 LINE 直接查警戒＋接收主動推播）

本專案已在 [web_app.py](web_app.py) 內建 **`POST /api/line/webhook`**，只要你把網頁部署到免費雲端（Hugging Face Spaces 或 Render，見 [DEPLOY.md](DEPLOY.md)），就能共用同一個伺服器免費跑起 LINE Bot！

### 費用說明（\$0 免費方案）
- **回覆訊息（Reply API）**：使用者傳位置或打字時回覆避難卡片 → **完全免費無上限**。
- **主動推播（Push API）**：背景 Proactive Agent 偵測到警戒升級主動推播到手機 → LINE 輕用量方案**每月免費 200 則**。

---

## 步驟 1：免費建立 LINE Messaging API Channel

1. 前往 [LINE Developers Console](https://developers.line.biz/console/)，用你的 LINE 帳號登入。
2. 建立一個 **Provider**（例如填 `TaiwanDisasterAgent`）。
3. 點 **Create a Messaging API channel**（會自動幫你建立對應的免費 LINE 官方帳號）：
   - 填寫頻道名稱（例如：`台灣防災避難助理`）、描述、分類後送出。

## 步驟 2：取得兩把金鑰

1. 在 **Basic settings** 分頁下方找到 **Channel secret** → 複製起來。
2. 切到 **Messaging API** 分頁最下方，點 **Issue** 產生 **Channel access token (long-lived)** → 複製起來。
3. 在同一個 **Messaging API** 分頁中：
   - **Auto-reply messages（自動回應訊息）** → 點 Edit 進去把它 **關閉**（避免 LINE 預設罐頭訊息干擾我們的 Agent）。
   - **Use webhook** → **開啟**。

## 步驟 3：填入環境變數並設定 Webhook

1. 在你的雲端平台（Hugging Face Spaces 的 Secrets 或 Render 的 Environment Variables；本機測試則填在 `.env`）加入：
   - `LINE_CHANNEL_SECRET` = 剛複製的 Channel secret
   - `LINE_CHANNEL_ACCESS_TOKEN` = 剛複製的 Channel access token
   - `PUBLIC_BASE_URL` = 你的公開 HTTPS 網址（例如 `https://你的帳號-taiwan-disaster-agent.hf.space`），設定後 LINE 卡片底部會多出一顆 **「🗺️ 開啟互動地圖與即時 CCTV」** 按鈕！
2. 回到 LINE Developers Console → **Messaging API** → **Webhook URL**，貼上：
   ```text
   https://你的公開網址/api/line/webhook
   ```
3. 點 **Verify** 出現 `Success` 即大功告成！

---

## 步驟 4：手機掃 QR Code 加好友立刻試玩！

在 LINE 聊天室裡試試這 4 個指令：
1. 點底部快捷鍵 **「📍 傳送我的位置」** → 6 秒內收到彩色風險避難 Flex 卡片（含即時天氣戶外提示＋最近官方避難所＋一鍵 Google Maps 步行導航）。
2. 點 **「🧪 凱米颱風演練」** → 接著輸入 `高雄三民區` 或 `屏東三地門鄉`，體驗 `CRITICAL` 紅色警戒卡片與自動告警編號。
3. 點 **「📡 訂閱主動推播」**（或輸入 `訂閱 高雄市三民區`）→ 系統背景每 5 分鐘主動巡檢官方警戒，升級達標時自動 Push 避難卡片到你的 LINE！
4. 輸入 **`取消訂閱`** 隨時退出主動推播；點 **「🟢 切回即時模式」** 回到即時官方資料。
