"""OneWeb Intellian (OW11 等) コレクタ.

Intellian の OneWeb 端末は公開 API を提供しておらず、監視手段は
LUI(ローカル Web UI)/モバイルアプリに限られる。そこで本コレクタは:

  1. SNMP が有効な機体/構成では SNMP GET で OID を読む (``snmp.enabled: true``)。
  2. それ以外は LUI の HTML/JSON を field_map でスクレイプ (Kymeta と同方式)。

いずれも実機依存のため、OID / field_map は設定で与える。取得できない項目
(周波数や SINR を LUI が出さない機体) は None のまま。その場合でも
ハンドオーバー予測(TLE ベース)側で「接続中と推定される衛星と仰角」を
補完できる。docs/devices.md 参照。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

import requests

from ..model import RFSample
from .base import Collector, dig, to_float

log = logging.getLogger("bdp.collector.intellian")

_NUMERIC = {
    "sinr_db", "rssi_dbm", "azimuth_deg", "elevation_deg", "tilt_deg",
    "tx_freq_mhz", "rx_freq_mhz", "down_bps", "up_bps", "latency_ms",
    "drop_rate", "obstruction_pct",
}


class IntellianCollector(Collector):
    kind = "intellian"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.verify_tls = bool(cfg.get("verify_tls", False))
        self.timeout_s = float(cfg.get("timeout_s", 8))
        self.endpoints = cfg.get("endpoints", {}) or {}
        self.field_map: Dict[str, str] = cfg.get("field_map", {}) or {}
        self.snmp_cfg = cfg.get("snmp", {}) or {}

        self.session = requests.Session()
        user, pw = cfg.get("username"), cfg.get("password")
        if user is not None:
            self.session.auth = (user, pw or "")
        if not self.verify_tls:
            requests.packages.urllib3.disable_warnings()  # type: ignore

    # ---- SNMP ------------------------------------------------------------
    def _poll_snmp(self) -> Dict[str, Any]:
        try:
            from pysnmp.hlapi import (  # type: ignore
                getCmd, SnmpEngine, CommunityData, UdpTransportTarget,
                ContextData, ObjectType, ObjectIdentity)
        except Exception:  # noqa: BLE001
            log.error("[%s] pysnmp が未インストールのため SNMP を利用できません", self.name)
            return {}

        host = self.snmp_cfg.get("host", self.base_url)
        community = self.snmp_cfg.get("community", "public")
        oids: Dict[str, str] = self.snmp_cfg.get("oids", {}) or {}
        out: Dict[str, Any] = {}
        for field, oid in oids.items():
            try:
                it = getCmd(SnmpEngine(), CommunityData(community),
                            UdpTransportTarget((host, 161), timeout=self.timeout_s),
                            ContextData(), ObjectType(ObjectIdentity(oid)))
                err_ind, err_stat, _idx, var_binds = next(it)
                if err_ind or err_stat:
                    continue
                val = str(var_binds[0][1])
                out[field] = to_float(val) if field in _NUMERIC else val
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] SNMP GET 失敗 %s: %s", self.name, oid, e)
        return out

    # ---- HTTP スクレイプ -------------------------------------------------
    def _poll_http(self) -> Dict[str, Any]:
        json_ep = self.endpoints.get("status_json")
        if json_ep:
            try:
                r = self.session.get(self.base_url + json_ep, verify=self.verify_tls,
                                     timeout=self.timeout_s)
                r.raise_for_status()
                data = r.json()
                return {f: (to_float(dig(data, p)) if f in _NUMERIC else dig(data, p))
                        for f, p in self.field_map.items() if dig(data, p) is not None}
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] JSON 取得失敗: %s", self.name, e)

        html_ep = self.endpoints.get("status_html", "/")
        try:
            r = self.session.get(self.base_url + html_ep, verify=self.verify_tls,
                                 timeout=self.timeout_s)
            r.raise_for_status()
            html = r.text
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] HTML 取得失敗: %s", self.name, e)
            return {}

        out: Dict[str, Any] = {}
        for field, pattern in self.field_map.items():
            try:
                m = re.search(pattern, html)
            except re.error:
                continue
            if m:
                val = m.group(1) if m.groups() else m.group(0)
                out[field] = to_float(val) if field in _NUMERIC else val
        return out

    def poll(self, now: float) -> Optional[RFSample]:
        values: Dict[str, Any] = {}
        if self.snmp_cfg.get("enabled"):
            values.update(self._poll_snmp())
        if not values and self.base_url:
            values.update(self._poll_http())
        if not values:
            return None
        return RFSample(ts=now, source=self.name, kind=self.kind, **values)
