# 同時相容 Hugging Face Spaces (預設 port 7860)、Render 與 Google Cloud Run (自動注入 PORT)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=7860

WORKDIR /app

# 先安裝相依套件以善用 Docker Build Cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼與公開避難收容處所資料（.dockerignore 會嚴格排除 .env 與私密檔案）
COPY web_app.py .
COPY web/ ./web/
COPY tools/ ./tools/
COPY disaster_agent/ ./disaster_agent/
COPY scenarios/ ./scenarios/
COPY data/ ./data/

# 建立可寫入的告警紀錄目錄（相容 Hugging Face Spaces 非 root 使用者）
RUN mkdir -p /app/logs && chmod 777 /app/logs

EXPOSE 7860

CMD ["python", "web_app.py"]
