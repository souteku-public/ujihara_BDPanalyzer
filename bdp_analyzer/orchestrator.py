"""監視オーケストレータ.

設定に基づき、以下を各々のスレッドで定期実行し結果を Storage に蓄積する:
  - RF コレクタ (Starlink / Kymeta / Intellian)
  - ネット品質測定 (role=sender のとき)
  - ハンドオーバー予測

role=receiver の場合は測定サーバのみを起動する(orchestrator は使わず
`python -m bdp_analyzer.netqual.server` 相当)。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import List, Optional

from .config import Config
from .storage import Storage
from .collectors import build_collector
from .handover.predict import HandoverPredictor
from .netqual import client as netclient

log = logging.getLogger("bdp.orchestrator")


class _Periodic(threading.Thread):
    def __init__(self, name: str, interval_s: float, fn):
        super().__init__(name=name, daemon=True)
        self.interval_s = max(1.0, interval_s)
        self.fn = fn
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                self.fn()
            except Exception as e:  # noqa: BLE001 — 1 周期の失敗で止めない
                log.exception("%s 実行エラー: %s", self.name, e)
            elapsed = time.monotonic() - start
            self._stop.wait(max(0.0, self.interval_s - elapsed))

    def stop(self) -> None:
        self._stop.set()


class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.storage = Storage(config.db_path)
        self.threads: List[_Periodic] = []
        # 予測結果の最新スナップショット(ダッシュボードが参照)
        self.latest_serving: dict = {}

    # ---- 個別ジョブ ------------------------------------------------------
    def _rf_job(self, collector):
        s = collector.poll(time.time())
        if s is not None:
            self.storage.add_rf(s)
            log.debug("RF[%s] sinr=%s az=%s el=%s", s.source, s.sinr_db,
                      s.azimuth_deg, s.elevation_deg)

    def _netqual_job(self):
        nq = self.config.netqual
        samples = netclient.measure_once(
            nq.get("peer_host"),
            int(nq.get("control_port", 5301)),
            int(nq.get("udp_port", 5302)),
            throughput_seconds=float(nq.get("throughput_seconds", 5)),
            udp_probe_count=int(nq.get("udp_probe_count", 200)),
            udp_probe_interval_ms=float(nq.get("udp_probe_interval_ms", 20)),
            session=nq.get("peer_host"),
        )
        for s in samples:
            self.storage.add_net(s)
        log.info("netqual: down=%.2fMbps up=%.2fMbps rtt=%sms loss=%s%%",
                 (samples[0].throughput_bps or 0) / 1e6,
                 (samples[1].throughput_bps or 0) / 1e6,
                 samples[0].rtt_ms, samples[0].loss_pct)

    def _handover_job(self, predictor: HandoverPredictor):
        serving, events = predictor.predict(time.time())
        self.latest_serving = serving
        for e in events:
            self.storage.add_handover(e)
        if events:
            nxt = events[0]
            log.info("次のハンドオーバー予測: %s %s→%s あと%.0fs (%s)",
                     nxt.constellation, nxt.from_sat, nxt.to_sat,
                     nxt.lead_time_s or 0, nxt.reason)

    # ---- 起動 ------------------------------------------------------------
    def start(self) -> None:
        # RF コレクタ
        for cfg in self.config.enabled_collectors:
            try:
                col = build_collector(cfg)
            except Exception as e:  # noqa: BLE001
                log.error("コレクタ構築失敗 %s: %s", cfg.get("name"), e)
                continue
            self.threads.append(_Periodic(
                f"rf:{col.name}", col.interval_s, lambda c=col: self._rf_job(c)))
            log.info("コレクタ有効化: %s (%s)", col.name, col.kind)

        # ネット品質 (sender のみ測定を主導)
        nq = self.config.netqual
        if nq.get("enabled") and nq.get("role") == "sender":
            if not nq.get("peer_host"):
                log.error("netqual.role=sender だが peer_host が未設定です")
            else:
                self.threads.append(_Periodic(
                    "netqual", float(nq.get("interval_s", 30)), self._netqual_job))
                log.info("ネット品質測定(sender)有効化 → %s", nq.get("peer_host"))

        # ハンドオーバー予測
        ho = self.config.handover
        if ho.get("enabled"):
            cache_dir = os.path.join(os.path.dirname(self.config.db_path) or ".", "tle")
            predictor = HandoverPredictor(ho, self.config.ground_station, cache_dir)
            self.threads.append(_Periodic(
                "handover", float(ho.get("interval_s", 30)),
                lambda p=predictor: self._handover_job(p)))
            log.info("ハンドオーバー予測有効化")

        for t in self.threads:
            t.start()

    def stop(self) -> None:
        for t in self.threads:
            t.stop()
