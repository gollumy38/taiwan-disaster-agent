"""Shared HTTP, caching, retry, filtering, and formatting utilities."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from dotenv import load_dotenv
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TAIWAN_TZ = timezone(timedelta(hours=8))
DEFAULT_TIMEOUT = 20
DEFAULT_CACHE_TTL_SECONDS = 60
MAX_CHARS = 6000

_cache_lock = threading.Lock()
_response_cache: dict[str, tuple[float, bytes]] = {}


def now_taiwan() -> datetime:
    """Return current Taiwan local datetime (UTC+8)."""
    return datetime.now(TAIWAN_TZ)


def now_taiwan_iso() -> str:
    """Return current Taiwan local time (UTC+8) in ISO format."""
    return now_taiwan().isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into a timezone-aware datetime."""
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIWAN_TZ)
        return dt
    except ValueError:
        return None


def get_env(key: str, default: str = "") -> str:
    """Read an environment variable (reloading .env if needed)."""
    val = os.getenv(key)
    if val is None:
        load_dotenv(PROJECT_ROOT / ".env")
        val = os.getenv(key, default)
    return val.strip()


def fetch_bytes(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
    retries_on_429: int = 2,
) -> bytes:
    """HTTP GET with short-term TTL caching and gentle retry on HTTP 429."""
    now_mono = time.monotonic()
    if cache_ttl > 0:
        with _cache_lock:
            cached = _response_cache.get(url)
            if cached and (now_mono - cached[0]) < cache_ttl:
                return cached[1]

    last_exc: Exception | None = None
    for attempt in range(retries_on_429 + 1):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "taiwan-disaster-agent/0.2"},
            )
            if resp.status_code == 429 and attempt < retries_on_429:
                time.sleep(0.8 * (attempt + 1))
                continue
            resp.raise_for_status()
            content = resp.content
            if cache_ttl > 0:
                with _cache_lock:
                    _response_cache[url] = (time.monotonic(), content)
            return content
        except requests.HTTPError as e:
            last_exc = e
            if (
                e.response is not None
                and e.response.status_code == 429
                and attempt < retries_on_429
            ):
                time.sleep(0.8 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_exc = e
            raise

    assert last_exc is not None
    raise last_exc


def fetch_json(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
) -> Any:
    """Fetch a URL and parse its response body as JSON."""
    raw = fetch_bytes(url, timeout=timeout, cache_ttl=cache_ttl)
    return json.loads(raw.decode("utf-8"))


def name_variants(place: str) -> list[str]:
    """Return spelling variants handling 「台」/「臺」 and common administrative suffixes."""
    cleaned = place.strip()
    if not cleaned:
        return []

    base_forms = {
        cleaned,
        cleaned.replace("台", "臺"),
        cleaned.replace("臺", "台"),
    }
    variants = set(base_forms)
    # 若使用者輸入「高雄三民區」，也拆出「三民區」與「高雄」相關的精簡詞以利比對
    for form in list(base_forms):
        for prefix in (
            "臺北市", "台北市", "新北市", "桃園市", "臺中市", "台中市",
            "臺南市", "台南市", "高雄市", "基隆市", "新竹市", "嘉義市",
            "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣", "嘉義縣",
            "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "台東縣", "澎湖縣",
            "金門縣", "連江縣", "台北", "臺北", "新北", "桃園", "台中",
            "臺中", "台南", "臺南", "高雄", "基隆", "屏東", "宜蘭", "花蓮",
        ):
            if form.startswith(prefix) and len(form) > len(prefix):
                sub = form[len(prefix):].strip()
                if len(sub) >= 2:
                    variants.add(sub)
                    variants.add(sub.replace("台", "臺"))
                    variants.add(sub.replace("臺", "台"))
    return sorted(variants, key=len, reverse=True)


def find_list(obj: Any) -> list | None:
    """Recursively locate the first list in a nested JSON object."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            found = find_list(v)
            if found is not None:
                return found
    return None


def filter_by_place(items: list[Any], place: str) -> list[Any]:
    """Filter items whose serialized JSON matches place (supporting 台/臺 & 縣市+鄉鎮)."""
    cleaned = place.strip()
    if not cleaned:
        return items

    # 若輸入同時包含縣市與鄉鎮（如「高雄三民區」），優先找同時符合較精確的鄉鎮
    variants = name_variants(cleaned)
    exact_hits = [
        it
        for it in items
        if any(
            v in json.dumps(it, ensure_ascii=False)
            for v in (cleaned, cleaned.replace("台", "臺"), cleaned.replace("臺", "台"))
        )
    ]
    if exact_hits:
        return exact_hits

    return [
        it
        for it in items
        if any(n in json.dumps(it, ensure_ascii=False) for n in variants)
    ]


def truncate_data(data: Any, max_chars: int = MAX_CHARS) -> Any:
    """Ensure serialized data stays within max_chars to avoid overloading the LLM."""
    text = json.dumps(data, ensure_ascii=False)
    if len(text) <= max_chars:
        return data
    return text[:max_chars] + "…(資料過長已截斷)"
