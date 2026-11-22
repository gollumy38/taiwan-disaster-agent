"""從政府資料開放平台（內政部消防署 #73242）同步全台官方避難收容處所點位 CSV。

執行方式：
    .venv\\Scripts\\python.exe scripts\\sync_shelters.py
"""

import csv
from pathlib import Path
import sys
import requests

sys.stdout.reconfigure(encoding="utf-8")

DATASET_API = "https://data.gov.tw/api/v2/rest/dataset/73242"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "shelters_nfa_official.csv"


def main() -> None:
    print("查詢政府資料開放平台 #73242（避難收容處所點位檔案）最新載點...")
    meta = requests.get(DATASET_API, timeout=20, headers={"User-Agent": "taiwan-disaster-agent/0.3"}).json()
    csv_url = ""
    for dist in meta.get("result", {}).get("distribution", []):
        if (dist.get("resourceFormat") or "").upper() == "CSV" and dist.get("resourceDownloadUrl"):
            csv_url = dist["resourceDownloadUrl"]
            break

    if not csv_url:
        raise RuntimeError("無法從 data.gov.tw #73242 找到 CSV 下載網址")

    print(f"開始下載：{csv_url}")
    resp = requests.get(csv_url, timeout=60, headers={"User-Agent": "taiwan-disaster-agent/0.3"})
    resp.raise_for_status()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(resp.content)

    # 驗證筆數與欄位
    for enc in ("utf-8-sig", "utf-8", "cp950"):
        try:
            text = resp.content.decode(enc)
            rows = list(csv.DictReader(text.splitlines()))
            print(f"✅ 同步完成！共 {len(rows)} 筆全台官方避難收容處所，已儲存至：{OUTPUT_PATH}")
            if rows:
                print("欄位清單：", list(rows[0].keys()))
                print("第 1 筆範例：", {k: rows[0][k] for k in list(rows[0].keys())[:8]})
            return
        except UnicodeDecodeError:
            continue

    print(f"已下載檔案至 {OUTPUT_PATH}（{len(resp.content)} bytes），但無法自動解析編碼。")


if __name__ == "__main__":
    main()
