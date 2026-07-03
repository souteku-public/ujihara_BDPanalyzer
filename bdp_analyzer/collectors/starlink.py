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

さらに ``get_history`` は端末内の **1 秒刻みリングバッファ** (遅延・上下スループット・
ドロップ率・SNR 等、直近 15 分程度) を返す。本コレクタはポーリングごとに
「前回以降の新しい秒データ」だけを差分回収し、ソース名 ``<name>:1s`` として
保存する。これによりポーリング間隔に関わらず 1 秒解像度の記録が取りこぼしなく
得られる (``history: false`` で無効化可能)。

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
        self.history_enabled = bool(cfg.get("history", True))
        self._last_current: Optional[int] = None   # get_history の読み取り位置
        self._warned = False

    def _run_grpcurl(self, payload: str = '{"get_status":{}}'
                     ) -> Optional[Dict[str, Any]]:
        if shutil.which(self.grpcurl) is None:
            if not self._warned:
                log.error("[%s] grpcurl が見つかりません。OS にインストールしてください "
                          "(https://github.com/fullstorydev/grpcurl)", self.name)
                self._warned = True
            return None
        cmd = [
            self.grpcurl, "-plaintext", "-max-time", str(int(self.timeout_s)),
            "-d", payload,
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

    # ---- 1 秒解像度の履歴 (get_history) -----------------------------------
    def _history_samples(self, hist: Dict[str, Any], now: float) -> list:
        """get_history 応答から前回以降の新規秒サンプルを RFSample 化する."""
        try:
            current = int(hist.get("current", 0))
        except (TypeError, ValueError):
            return []
        latency = hist.get("popPingLatencyMs") or []
        down = hist.get("downlinkThroughputBps") or []
        up = hist.get("uplinkThroughputBps") or []
        drop = hist.get("popPingDropRate") or []
        snr = hist.get("snr") or []
        ring = max(len(latency), len(down), len(up), len(drop))
        if current <= 0 or ring == 0:
            return []

        if self._last_current is None:
            # 初回は直近 interval_s 秒だけ (再起動時の重複を最小化)
            n_new = min(int(self.interval_s) + 2, ring, current)
        else:
            n_new = min(max(current - self._last_current, 0), ring, current)
        self._last_current = current
        if n_new <= 0:
            return []

        def at(arr, idx):
            return to_float(arr[idx]) if idx < len(arr) else None

        out = []
        for offset in range(n_new - 1, -1, -1):     # 古い順に
            idx = (current - 1 - offset) % ring
            snr_v = at(snr, idx)
            out.append(RFSample(
                ts=now - offset,
                source=f"{self.name}:1s",
                kind=self.kind,
                sinr_db=snr_v if snr_v else None,
                down_bps=at(down, idx),
                up_bps=at(up, idx),
                latency_ms=at(latency, idx),
                drop_rate=at(drop, idx),
            ))
        return out

    def poll_many(self, now: float) -> list:
        samples = []
        s = self.poll(now)
        if s is not None:
            samples.append(s)
        if self.history_enabled:
            resp = self._run_grpcurl('{"get_history":{}}')
            hist = (resp or {}).get("dishGetHistory") or \
                   (resp or {}).get("dish_get_history") or {}
            if hist:
                samples.extend(self._history_samples(hist, now))
        return samples

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
