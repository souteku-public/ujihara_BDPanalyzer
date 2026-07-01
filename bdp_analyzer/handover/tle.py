"""CelesTrak から TLE(軌道要素)を取得・キャッシュする.

Starlink / OneWeb の全機体 TLE を GROUP 単位で取得。取得失敗時は
キャッシュファイルにフォールバックする(ダーク環境対策)。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Tuple

import requests

log = logging.getLogger("bdp.handover.tle")

# (name, line1, line2)
TLE = Tuple[str, str, str]


def _cache_path(cache_dir: str, constellation: str) -> str:
    return os.path.join(cache_dir, f"tle_{constellation}.txt")


def _parse(text: str) -> List[TLE]:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: List[TLE] = []
    i = 0
    while i + 2 < len(lines) + 1 and i + 2 <= len(lines):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2] if i + 2 < len(lines) else ""
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append((name, l1, l2))
            i += 3
        else:
            i += 1
    return out


def fetch_tle(constellation: str, url: str, cache_dir: str,
              cache_hours: float = 6.0) -> List[TLE]:
    """TLE を取得。キャッシュが新しければそれを使う."""
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, constellation)

    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h < cache_hours:
            with open(path, "r", encoding="utf-8") as fh:
                return _parse(fh.read())

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        text = r.text
        if "1 " in text and "2 " in text:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            log.info("TLE 更新: %s (%d 行)", constellation, len(text.splitlines()))
            return _parse(text)
        log.warning("TLE 応答が不正: %s", constellation)
    except Exception as e:  # noqa: BLE001
        log.warning("TLE 取得失敗 %s: %s (キャッシュを試行)", constellation, e)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return _parse(fh.read())
    return []
