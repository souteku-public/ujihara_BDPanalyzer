"""Starlink (Mini 含む) コレクタ.

Starlink には RF 専用の WebGUI が無いが、Dish 本体 (Dishy) は
``192.168.100.1:9200`` で **認証不要の gRPC** を公開しており、そこから

  - ボアサイト方位角 / 仰角 (アンテナの向き)
  - SNR がノイズフロア以上か (is_snr_above_noise_floor)、旧 SNR 値
  - ダウン/アップ スループット、PoP ping 遅延、ドロップ率
  - 障害物 (obstruction) 率
  - GPS / 状態

が取得できる。これが「Starlink の電波部を監視するソリューション」となる。

実装は追加 Python 依存を増やさないため、``grpcurl`` バイナリを
サーバリフレクション付きで呼び出し、JSON を解析する。

  grpcurl -plaintext -d '{"get_status":{}}' 192.168.100.1:9200 \
          SpaceX.API.Device.Device/Handle

注意:
  - 周波数はこの API では公開されていない (Starlink は非開示)。
  - 新しいファームでは ``snr`` 数値は 0 で返り、is_snr_above_noise_floor /
    is_snr_persistently_low の真偽のみが有効な機体がある。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any, Dict, Optional

from ..model import RFSample
from .base import Collector, dig, to_float

log = logging.getLogger("bdp.collector.starlink")


class StarlinkCollector(Collector):
    kind = "starlink"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.address = cfg.get("address", "192.168.100.1:9200")
        self.grpcurl = cfg.get("grpcurl", "grpcurl")
        self.timeout_s = float(cfg.get("timeout_s", 8))
        self._warned = False

    def _run_grpcurl(self) -> Optional[Dict[str, Any]]:
        if shutil.which(self.grpcurl) is None:
            if not self._warned:
                log.error("[%s] grpcurl が見つかりません。OS にインストールしてください "
                          "(https://github.com/fullstorydev/grpcurl)", self.name)
                self._warned = True
            return None
        cmd = [
            self.grpcurl, "-plaintext", "-max-time", str(int(self.timeout_s)),
            "-d", '{"get_status":{}}',
            self.address, "SpaceX.API.Device.Device/Handle",
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=self.timeout_s + 2)
        except subprocess.TimeoutExpired:
            log.warning("[%s] grpcurl timeout", self.name)
            return None
        if out.returncode != 0:
            log.warning("[%s] grpcurl error: %s", self.name, out.stderr.strip()[:200])
            return None
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError:
            log.warning("[%s] grpcurl の出力を JSON として解析できません", self.name)
            return None

    def poll(self, now: float) -> Optional[RFSample]:
        resp = self._run_grpcurl()
        if resp is None:
            return None
        st = resp.get("dishGetStatus") or resp.get("dish_get_status") or {}
        if not st:
            log.warning("[%s] dishGetStatus が空です", self.name)
            return None

        align = st.get("alignmentStats", {})
        obstr = st.get("obstructionStats", {})

        # SNR: 数値があればそれを、無ければノイズフロア判定のみ
        snr_val = to_float(st.get("snr"))
        above = st.get("isSnrAboveNoiseFloor")
        frac = to_float(obstr.get("fractionObstructed"))

        return RFSample(
            ts=now,
            source=self.name,
            kind=self.kind,
            sinr_db=snr_val if snr_val else None,
            snr_above_noise=bool(above) if above is not None else None,
            azimuth_deg=to_float(align.get("boresightAzimuthDeg")
                                 or st.get("boresightAzimuthDeg")),
            elevation_deg=to_float(align.get("boresightElevationDeg")
                                   or st.get("boresightElevationDeg")),
            tilt_deg=to_float(align.get("tiltAngleDeg")),
            down_bps=to_float(st.get("downlinkThroughputBps")),
            up_bps=to_float(st.get("uplinkThroughputBps")),
            latency_ms=to_float(st.get("popPingLatencyMs")),
            drop_rate=to_float(st.get("popPingDropRate")),
            obstruction_pct=(frac * 100.0) if frac is not None else None,
            state=st.get("state") or dig(st, "deviceState.state"),
            raw=st,
        )
