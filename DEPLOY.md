# 免費雲端上線教學（免 Docker、\$0、免綁信用卡）

本專案已改用 **原生 Python 執行環境**，完全不需要 Docker 或付費 Pro 帳號，上線後同時提供 **GPS 互動地圖網頁＋LINE Bot Webhook＋背景主動巡檢**！

> **資安提醒**：`.gitignore` 已自動排除 `.env`、`logs/` 與 `docs/WATCH資料來源.xlsx`。**切勿手動上傳這些檔案**，金鑰一律填在平台後台的 Environment Variables。

---

## 方案 1：Render 免費方案（最推薦 — 免 Docker、免綁卡、永久 HTTPS 網址）

### 步驟 1：把專案推到 GitHub
1. 到 [https://github.com](https://github.com) 建一個新的 Repository（例如 `taiwan-disaster-agent`，Public 或 Private 皆可）。
2. 在終端機執行（`.gitignore` 會自動排除 `.env` 與 `docs/WATCH資料來源.xlsx`）：
   ```powershell
   cd c:\Users\user\Desktop\agent
   git init
   git add .
   git status
   ```
   *(確認 `git status` 畫面裡**沒有** `.env` 和 `WATCH資料來源.xlsx` 再繼續！)*
   ```powershell
   git commit -m "Taiwan Disaster Multi-Agent web app and LINE Bot"
   git branch -M main
   git remote add origin https://github.com/<你的帳號>/taiwan-disaster-agent.git
   git push -u origin main
   ```

### 步驟 2：連上 Render 一鍵免費部署
1. 前往 [https://render.com](https://render.com) 用 GitHub 免費登入（免綁信用卡）。
2. 點右上角 **New +** → **Web Service**（或 **Blueprint**），選擇剛剛的 `taiwan-disaster-agent` repo：
   - **Runtime**：選 **`Python 3`**（不用選 Docker！）
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`uvicorn web_app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**：選 **`Free`**（\$0/month）
3. 在下方 **Environment Variables** 加入：
   - `GOOGLE_API_KEY` = 你的 Gemini API 金鑰（必填）
   - *(選填)* `CWA_API_KEY`、`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`PUBLIC_BASE_URL`、`ACCESS_PASSWORD`
4. 點 **Deploy Web Service**，等約 2~3 分鐘就會拿到永久 HTTPS 網址：
   `https://taiwan-disaster-agent.onrender.com`
   - 傳給朋友打開就是互動地圖網頁！
   - 貼到 LINE Developers 的 Webhook URL 就是：`https://taiwan-disaster-agent.onrender.com/api/line/webhook`！

*(免費方案提醒：15 分鐘沒人開啟會自動休眠，下次打開等約 30~50 秒喚醒即可正常使用。)*

---

## 方案 2：免帳號 1 分鐘臨時 HTTPS（本機電腦直接傳手機測 GPS & LINE Bot）

如果現在就想立刻用手機實測、懶得先傳 GitHub：
1. 第一個終端機先跑起網站：
   ```powershell
   cd c:\Users\user\Desktop\agent
   .\.venv\Scripts\python.exe web_app.py
   ```
2. 第二個終端機用 Windows 內建 `winget` 裝免費工具 `cloudflared`（裝過可跳過）並開通臨時 HTTPS：
   ```powershell
   winget install Cloudflare.cloudflared
   cloudflared tunnel --url http://127.0.0.1:8080
   ```
3. 終端機會印出一個免費的 **`https://xxxx.trycloudflare.com`** 網址，直接傳給手機或貼進 LINE Webhook 就能用（關掉電腦後網址即失效）。
