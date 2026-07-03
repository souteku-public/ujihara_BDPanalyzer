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
        # ネットワーク情報 (UI 入力のメモ) と負荷試験の実行状態
        self.net_profile: Dict[str, Any] = {}
        self._global_ip: Optional[str] = None
        self.load_status: Dict[str, Any] = {"running": False, "results": []}

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
            "net_profile": self.net_profile,
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

    # ---- ネットワーク情報 (UI 入力 + 自動検出) --------------------------------
    _PROFILE_KEYS = ("line_type", "local_ip_note", "peer_global_ip", "note")

    def get_netinfo(self) -> Dict[str, Any]:
        import socket as _s
        ips = set()
        try:
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))          # 実送信はしない (経路上の自 IP を得る)
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
        try:
            for ip in _s.gethostbyname_ex(_s.gethostname())[2]:
                if not ip.startswith("127."):
                    ips.add(ip)
        except OSError:
            pass
        return {
            "hostname": _s.gethostname(),
            "local_ips": sorted(ips),
            "global_ip": self._global_ip,
            "profile": dict(self.net_profile),
        }

    def refresh_global_ip(self) -> Optional[str]:
        """外部サービスに問い合わせて自局のグローバル IP を取得 (ベストエフォート)."""
        import requests
        for url in ("https://api.ipify.org?format=json",
                    "https://ifconfig.me/ip"):
            try:
                r = requests.get(url, timeout=5)
                r.raise_for_status()
                ip = (r.json().get("ip") if "json" in url else r.text).strip()
                if ip:
                    self._global_ip = ip
                    return ip
            except Exception as e:  # noqa: BLE001
                log.debug("グローバル IP 取得失敗 %s: %s", url, e)
        return None

    def set_net_profile(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for k in self._PROFILE_KEYS:
                if k in data:
                    self.net_profile[k] = str(data[k])[:200]
            self._save_state()
        return dict(self.net_profile)

    # ---- 負荷耐性テスト (レートスイープ) --------------------------------------
    def start_load_test(self, target_id: str, direction: str,
                        rates_bps: List[float], step_seconds: float) -> Optional[str]:
        """UI からの負荷試験開始。エラー文字列を返す (None なら開始成功)."""
        if direction not in ("uplink", "downlink"):
            return "direction は uplink / downlink"
        if not rates_bps or len(rates_bps) > 20:
            return "レートは 1〜20 段で指定してください"
        with self._lock:
            if self.load_status.get("running"):
                return "負荷試験が実行中です"
            j = self.jobs.get(target_id)
            if not j or j["kind"] != "net":
                return "測定先が見つかりません"
            meta = dict(j["meta"])
            d = self._net_defaults()
            self.load_status = {
                "running": True, "target": target_id,
                "session": meta.get("label") or meta.get("host"),
                "direction": direction, "step": 0, "total": len(rates_bps),
                "results": [], "error": None, "started_ts": time.time(),
            }
        threading.Thread(target=self._load_test_thread, daemon=True,
                         args=(target_id, meta, d, direction,
                               rates_bps, step_seconds)).start()
        return None

    def get_load_status(self) -> Dict[str, Any]:
        with self._lock:
            st = dict(self.load_status)
            st["results"] = list(st.get("results", []))
        return st

    def _load_test_thread(self, target_id: str, meta: Dict[str, Any],
                          defaults: Dict[str, Any], direction: str,
                          rates_bps: List[float], step_seconds: float) -> None:
        from .netqual import loadtest

        # 定期測定と負荷が干渉しないよう、対象ジョブを試験中だけ止める
        # (UI 状態ファイルには保存しない一時停止)
        with self._lock:
            j = self.jobs.get(target_id)
            was_running = bool(j and j["enabled"])
            if was_running:
                self._stop_job(target_id)

        def on_step(r):
            self.storage.add_load_result(r)
            with self._lock:
                self.load_status["step"] += 1
                self.load_status["results"].append(r)

        try:
            loadtest.run_sweep(
                meta["host"],
                int(meta.get("control_port", defaults["control_port"])),
                int(meta.get("udp_port", defaults["udp_port"])),
                direction=direction, rates_bps=rates_bps,
                step_seconds=step_seconds,
                session=meta.get("label") or meta.get("host"),
                on_step=on_step)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.load_status["error"] = str(e)
            log.exception("負荷試験エラー: %s", e)
        finally:
            with self._lock:
                self.load_status["running"] = False
                if was_running and target_id in self.jobs:
                    self._start_job(target_id)

    # ---- 起動 ---------------------------------------------------------------
    def start(self) -> None:
        state = self._load_state()
        overrides: Dict[str, bool] = state.get("enabled", {})
        self.net_profile = dict(state.get("net_profile") or {})

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
