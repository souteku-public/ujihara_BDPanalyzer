"""Kymeta (OneWeb 運用) コレクタ.

Kymeta u8 / Osprey 端末は端末 IP の **HTTPS WebGUI** を持ち、Status ページに
SINR・スキャン角(→仰角)などのトラッキング指標を表示する。RF 情報を見られる
WebGUI はこれのみ、というのがユーザ要件。ここでは IP を指定して WebGUI から
値を取得(スクレイプ)する。

WebGUI の内部 API/HTML は機種・ファームで異なるため、``field_map`` で
「どのキーをどの RFSample フィールドに対応づけるか」を設定で与える方式にした。

取得モード:
  1. ``endpoints.status_json`` が設定されていれば、その JSON を取得し
     field_map の "a.b.c" パスで値を拾う (推奨・堅牢)。
  2. JSON が無い場合、HTML を取得し field_map の値を「正規表現」として扱い、
     最初のキャプチャグループを数値/文字列として拾う。

実機の HTML を確認して field_map を調整する手順は docs/devices.md を参照。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

import requests

from ..model import RFSample
from .base import Collector, dig, to_float

log = logging.getLogger("bdp.collector.kymeta")

# RFSample の数値フィールド (float 化するもの)
_NUMERIC = {
    "sinr_db", "rssi_dbm", "azimuth_deg", "elevation_deg", "tilt_deg",
    "tx_freq_mhz", "rx_freq_mhz", "down_bps", "up_bps", "latency_ms",
    "drop_rate", "obstruction_pct",
}


class KymetaCollector(Collector):
    kind = "kymeta"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.base_url = cfg.get("base_url", "").rstrip("/")
        self.verify_tls = bool(cfg.get("verify_tls", False))
        self.timeout_s = float(cfg.get("timeout_s", 8))
        self.endpoints = cfg.get("endpoints", {}) or {}
        self.field_map: Dict[str, str] = cfg.get("field_map", {}) or {}

        self.session = requests.Session()
        user, pw = cfg.get("username"), cfg.get("password")
        if user is not None:
            self.session.auth = (user, pw or "")
        if not self.verify_tls:
            requests.packages.urllib3.disable_warnings()  # type: ignore

    # ---- 取得手段 --------------------------------------------------------
    def _get_json(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            r = self.session.get(self.base_url + path, verify=self.verify_tls,
                                  timeout=self.timeout_s)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 — 監視を止めない
            log.warning("[%s] JSON 取得失敗 %s: %s", self.name, path, e)
            return None

    def _get_html(self, path: str) -> Optional[str]:
        try:
            r = self.session.get(self.base_url + path, verify=self.verify_tls,
                                  timeout=self.timeout_s)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] HTML 取得失敗 %s: %s", self.name, path, e)
            return None

    # ---- マッピング ------------------------------------------------------
    def _from_json(self, data: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for field, path in self.field_map.items():
            val = dig(data, path)
            if val is None:
                continue
            out[field] = to_float(val) if field in _NUMERIC else val
        return out

    def _from_html(self, html: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for field, pattern in self.field_map.items():
            try:
                m = re.search(pattern, html)
            except re.error:
                log.warning("[%s] field_map[%s] が正規表現として不正", self.name, field)
                continue
            if not m:
                continue
            val = m.group(1) if m.groups() else m.group(0)
            out[field] = to_float(val) if field in _NUMERIC else val
        return out

    def poll(self, now: float) -> Optional[RFSample]:
        values: Dict[str, Any] = {}
        raw: Dict[str, Any] = {}

        json_ep = self.endpoints.get("status_json")
        if json_ep:
            data = self._get_json(json_ep)
            if data is not None:
                raw = data if isinstance(data, dict) else {"data": data}
                values = self._from_json(raw)

        if not values:  # JSON が無い / 空 → HTML スクレイプ
            html_ep = self.endpoints.get("status_html", "/")
            html = self._get_html(html_ep)
            if html:
                values = self._from_html(html)

        if not values:
            return None

        return RFSample(ts=now, source=self.name, kind=self.kind, raw=raw, **values)
