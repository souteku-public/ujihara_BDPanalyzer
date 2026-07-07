"""上空の気象 (雨雲) 情報の取得.

Open-Meteo (https://open-meteo.com) の現在天気 API を利用する。
無料・API キー不要・15 分毎更新。自局 (ground_station) の緯度経度で
降水強度・雲量・気温・湿度・風速・天気コードを定量取得し、
降雨減衰の理論 SINR への反映と、weather_samples テーブルへの記録に使う。

注意: Open-Meteo の current.precipitation は「直前 15 分間の降水量 [mm]」
のため、×4 して mm/h 換算で保存する (瞬時値の近似)。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("bdp.weather")

_API = ("https://api.open-meteo.com/v1/forecast"
        "?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,relative_humidity_2m,precipitation,rain,"
        "showers,cloud_cover,weather_code,wind_speed_10m"
        "&wind_speed_unit=ms")


def fetch_weather(lat: float, lon: float,
                  timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """現在の気象を取得して定量値の dict を返す (失敗時 None)."""
    url = _API.format(lat=lat, lon=lon)
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        cur = (r.json() or {}).get("current") or {}
    except Exception as e:  # noqa: BLE001 — 監視を止めない
        log.warning("気象取得失敗: %s", e)
        return None

    def f(key: str) -> Optional[float]:
        v = cur.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    precip_15min_mm = f("precipitation") or 0.0
    rain_15min_mm = f("rain") or 0.0
    return {
        "ts": time.time(),
        # 15 分値 [mm] → mm/h 換算 (×4)
        "precip_mmh": round(precip_15min_mm * 4.0, 2),
        "rain_mmh": round(rain_15min_mm * 4.0, 2),
        "cloud_cover_pct": f("cloud_cover"),
        "temp_c": f("temperature_2m"),
        "humidity_pct": f("relative_humidity_2m"),
        "weather_code": int(cur.get("weather_code") or 0),
        "wind_speed_ms": f("wind_speed_10m"),
    }
