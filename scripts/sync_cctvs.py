"""同步全台官方 CCTV 站點清單（水利署、農村水保署崩塌地、高公局國道）至 data/cctv_catalog.json。

這樣部署到海外免費主機（如 Render 新加坡）時，即使台灣政府 API 封鎖海外 IP，
系統依然能秒查全台 4,498 支攝影機座標，並由使用者在台灣本地的瀏覽器直接載入即時畫面！

執行方式：
    .venv\\Scripts\\python.exe scripts\\sync_cctvs.py
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from tools.cctv import _load_freeway_stations, _load_landslide_stations, _load_wra_stations

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "cctv_catalog.json"


def main() -> None:
    print("開始同步台灣官方即時 CCTV 站點清單...")
    wra = _load_wra_stations()
    landslide = _load_landslide_stations()
    freeway = _load_freeway_stations()
    payload = {
        "wra": wra,
        "landslide": landslide,
        "freeway": freeway,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(
        f"✅ 同步完成！水利署={len(wra)} 支、崩塌地={len(landslide)} 支、國道={len(freeway)} 支"
        f"（共 {len(wra) + len(landslide) + len(freeway)} 支，檔案大小 {size_kb:.1f} KB）"
    )


if __name__ == "__main__":
    main()
