"""監視オーケストレータ (ジョブ管理型).

設定に基づき各計測を「ジョブ」として登録し、Web UI から実行時に
ON/OFF・追加できるようにする:

  - rf:<name>    … RF コレクタ (Starlink / Kymeta / Intellian)
  - net:<label>  … 通信品質の測定先 (衛星経由 / 有線 LAN / Wi-Fi など複数可)
  - handover     … ハンドオーバー予測
  - netqual-server … 受信サーバ (対向からの測定に応答する側)

UI での ON/OFF・追加した測定先は ui_state.json に保存され、再起動後も
引き継がれる (config.yaml は書き換えない)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

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


def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z぀-ヿ一-鿿_-]+", "-", text).strip("-")


class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.storage = Storage(config.db_path)
        self._lock = threading.Lock()
        # job_id -> {id,label,kind,interval_s,enabled,thread,fn,removable,detail,meta}
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.netqual_server = None
        # 予測結果の最新スナップショット(ダッシュボードが参照)
        self.latest_serving: dict = {}
        self._state_path = os.path.join(
            os.path.dirname(config.db_path) or ".", "ui_state.json")

    # ---- UI 状態の永続化 ---------------------------------------------------
    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        state = {
            "enabled": {jid: j["enabled"] for jid, j in self.jobs.items()},
            "targets": [j["meta"] for j in self.jobs.values()
                        if j["kind"] == "net" and j.get("removable")],
        }
        try:
            with open(self._state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
        except OSError as e:
            log.warning("UI 状態の保存失敗: %s", e)

    # ---- 個別ジョブの中身 ---------------------------------------------------
    def _rf_job(self, collector):
        s = collector.poll(time.time())
        if s is not None:
            self.storage.add_rf(s)

    def _net_defaults(self) -> Dict[str, Any]:
        nq = self.config.netqual
        return {
            "control_port": int(nq.get("control_port", 5301)),
            "udp_port": int(nq.get("udp_port", 5302)),
            "interval_s": float(nq.get("interval_s", 30)),
            "throughput_seconds": float(nq.get("throughput_seconds", 5)),
            "udp_probe_count": int(nq.get("udp_probe_count", 200)),
            "udp_probe_interval_ms": float(nq.get("udp_probe_interval_ms", 20)),
        }

    def _net_job(self, target: Dict[str, Any]):
        d = self._net_defaults()
        samples = netclient.measure_once(
            target["host"],
            int(target.get("control_port", d["control_port"])),
            int(target.get("udp_port", d["udp_port"])),
            throughput_seconds=float(target.get("throughput_seconds",
                                                d["throughput_seconds"])),
            udp_probe_count=int(target.get("udp_probe_count", d["udp_probe_count"])),
            udp_probe_interval_ms=float(target.get("udp_probe_interval_ms",
                                                   d["udp_probe_interval_ms"])),
            session=target.get("label") or target["host"],
        )
        for s in samples:
            self.storage.add_net(s)
        log.info("netqual[%s]: down=%.2fMbps up=%.2fMbps rtt=%sms loss=%s%%",
                 target.get("label"),
                 (samples[0].throughput_bps or 0) / 1e6,
                 (samples[1].throughput_bps or 0) / 1e6,
                 samples[0].rtt_ms, samples[0].loss_pct)

    def _handover_job(self, predictor: HandoverPredictor):
        serving, events = predictor.predict(time.time())
        self.latest_serving = serving
        for e in events:
            self.storage.add_handover(e)

    # ---- ジョブ登録 / 制御 ---------------------------------------------------
    def _register(self, job_id: str, *, label: str, kind: str, interval_s: float,
                  fn, enabled: bool, removable: bool = False, detail: str = "",
                  meta: Optional[Dict[str, Any]] = None) -> None:
        self.jobs[job_id] = {
            "id": job_id, "label": label, "kind": kind, "interval_s": interval_s,
            "enabled": False, "thread": None, "fn": fn, "removable": removable,
            "detail": detail, "meta": meta or {},
        }
        if enabled:
            self._start_job(job_id)

    def _start_job(self, job_id: str) -> None:
        j = self.jobs[job_id]
        if j["kind"] == "server":
            if self.netqual_server is None:
                from .netqual.server import NetqualServer
                d = self._net_defaults()
                self.netqual_server = NetqualServer(d["control_port"], d["udp_port"])
                self.netqual_server.start()
        elif j["thread"] is None:
            j["thread"] = _Periodic(job_id, j["interval_s"], j["fn"])
            j["thread"].start()
        j["enabled"] = True

    def _stop_job(self, job_id: str) -> None:
        j = self.jobs[job_id]
        if j["kind"] == "server":
            if self.netqual_server is not None:
                self.netqual_server.stop()
                self.netqual_server = None
        elif j["thread"] is not None:
            j["thread"].stop()
            j["thread"] = None
        j["enabled"] = False

    # ---- Web UI 向け公開 API -------------------------------------------------
    def jobs_status(self) -> List[Dict[str, Any]]:
        order = {"rf": 0, "net": 1, "handover": 2, "server": 3}
        with self._lock:
            rows = [{k: j[k] for k in
                     ("id", "label", "kind", "interval_s", "enabled",
                      "removable", "detail")}
                    for j in self.jobs.values()]
        rows.sort(key=lambda r: (order.get(r["kind"], 9), r["label"]))
        return rows

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._lock:
            if job_id not in self.jobs:
                return False
            if enabled:
                self._start_job(job_id)
            else:
                self._stop_job(job_id)
            self._save_state()
            log.info("ジョブ %s を %s", job_id, "開始" if enabled else "停止")
            return True

    def add_net_target(self, label: str, host: str, *, enabled: bool = True,
                       **overrides) -> Optional[str]:
        """UI から測定先を追加 (Ether/Wi-Fi チェック等)。job_id を返す."""
        label = (label or host).strip()
        host = host.strip()
        if not host:
            return None
        job_id = f"net:{_slug(label)}"
        with self._lock:
            if job_id in self.jobs:
                return None  # 同名は不可
            meta = {"label": label, "host": host, **{
                k: v for k, v in overrides.items() if v is not None}}
            d = self._net_defaults()
            self._register(
                job_id, label=label, kind="net",
                interval_s=float(meta.get("interval_s", d["interval_s"])),
                fn=lambda t=meta: self._net_job(t),
                enabled=enabled, removable=True, detail=f"→ {host}", meta=meta)
            self._save_state()
        return job_id

    def remove_net_target(self, job_id: str) -> bool:
        with self._lock:
            j = self.jobs.get(job_id)
            if not j or not j.get("removable"):
                return False
            self._stop_job(job_id)
            del self.jobs[job_id]
            self._save_state()
            return True

    # ---- 起動 ---------------------------------------------------------------
    def start(self) -> None:
        state = self._load_state()
        overrides: Dict[str, bool] = state.get("enabled", {})

        def initial(job_id: str, default: bool) -> bool:
            return bool(overrides.get(job_id, default))

        # RF コレクタ: 設定にある全機器を登録 (enabled は初期状態、UI で切替可)
        for cfg in self.config.collectors:
            try:
                col = build_collector(cfg)
            except Exception as e:  # noqa: BLE001
                log.error("コレクタ構築失敗 %s: %s", cfg.get("name"), e)
                continue
            job_id = f"rf:{col.name}"
            self._register(job_id, label=col.name, kind="rf",
                           interval_s=col.interval_s,
                           fn=lambda c=col: self._rf_job(c),
                           enabled=initial(job_id, bool(cfg.get("enabled"))),
                           detail=cfg.get("kind", ""))

        # ネット品質: 受信サーバ + 測定先(複数)
        nq = self.config.netqual
        if nq.get("enabled", True):
            role = nq.get("role", "both")
            serve_default = bool(nq.get("serve", role in ("both", "receiver")))
            self._register("netqual-server", label="受信サーバ(応答側)",
                           kind="server", interval_s=0, fn=None,
                           enabled=initial("netqual-server", serve_default),
                           detail=f"TCP:{self._net_defaults()['control_port']} / "
                                  f"UDP:{self._net_defaults()['udp_port']}")

            measure_default = role in ("both", "sender")
            targets: List[Dict[str, Any]] = list(nq.get("targets") or [])
            if not targets and nq.get("peer_host"):  # 旧形式との互換
                targets = [{"label": nq["peer_host"], "host": nq["peer_host"]}]
            for t in targets:
                label = t.get("label") or t.get("host", "")
                job_id = f"net:{_slug(label)}"
                d = self._net_defaults()
                self._register(job_id, label=label, kind="net",
                               interval_s=float(t.get("interval_s", d["interval_s"])),
                               fn=lambda tt=t: self._net_job(tt),
                               enabled=initial(job_id, measure_default
                                               and t.get("enabled", True)),
                               detail=f"→ {t.get('host')}", meta=dict(t))
            # UI から追加された測定先を復元 (保存されていた ON/OFF 状態も引き継ぐ)
            for t in state.get("targets", []):
                job_id = f"net:{_slug(t.get('label', ''))}"
                if job_id in self.jobs:
                    continue
                self.add_net_target(t.get("label", ""), t.get("host", ""),
                                    enabled=initial(job_id, True),
                                    **{k: v for k, v in t.items()
                                       if k not in ("label", "host")})

        # ハンドオーバー予測
        ho = self.config.handover
        if ho:
            cache_dir = os.path.join(os.path.dirname(self.config.db_path) or ".", "tle")
            predictor = HandoverPredictor(ho, self.config.ground_station, cache_dir)
            self._register("handover", label="ハンドオーバー予測", kind="handover",
                           interval_s=float(ho.get("interval_s", 30)),
                           fn=lambda p=predictor: self._handover_job(p),
                           enabled=initial("handover", bool(ho.get("enabled", True))),
                           detail="TLE+SGP4")

        enabled_n = sum(1 for j in self.jobs.values() if j["enabled"])
        log.info("ジョブ登録: %d 件 (稼働 %d 件) — Web UI で切替できます",
                 len(self.jobs), enabled_n)

    def stop(self) -> None:
        for job_id in list(self.jobs):
            self._stop_job(job_id)
