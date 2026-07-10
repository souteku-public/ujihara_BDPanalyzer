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


# 実行間隔の許容範囲 (秒)。UI にもこの値がメモとして表示される。
INTERVAL_RANGES = {
    "rf":       {"min": 1,  "max": 3600,
                 "hint": "Starlink は 1 秒まで可 (get_history 併用で 1 秒解像度)。"
                         "Kymeta/Intellian は端末 Web への負荷を考え 5 秒以上を推奨"},
    "net":      {"min": 5,  "max": 3600,
                 "hint": "1 サイクルに『スループット測定時間×2 + プローブ約 4 秒』"
                         "かかるため、それより短くしても詰まるだけ"},
    "handover": {"min": 10, "max": 3600,
                 "hint": "TLE 伝搬計算の負荷と予測の鮮度のバランス。30 秒で十分なことが多い"},
    "weather":  {"min": 60, "max": 3600,
                 "hint": "Open-Meteo の更新は 15 分毎のため 300 秒 (5 分) で十分。"
                         "外部 API への負荷配慮から 60 秒未満には縮めない"},
}

# 測定パラメータの範囲・既定値 (UI の「測定設定」に表示されるメモの元データ)
SETTINGS_SPEC = {
    "throughput_seconds": {
        "default": 5, "min": 1, "max": 30, "unit": "秒",
        "label": "スループット測定時間 (1 方向あたり)",
        "hint": "長いほど正確だが回線を占有する時間も伸びる。短周期監視なら 1〜3 秒"},
    "udp_probe_count": {
        "default": 200, "min": 10, "max": 1000, "unit": "個",
        "label": "UDP プローブ数 (通常測定 1 回あたり)",
        "hint": "多いほどロス率の分解能が上がる (200 個 → 0.5% 刻み)"},
    "udp_probe_interval_ms": {
        "default": 20, "min": 5, "max": 100, "unit": "ms",
        "label": "UDP プローブ送出間隔 (通常測定)",
        "hint": "count×interval が測定所要時間になる (200×20ms = 4 秒)"},
    "rtt_probe_interval_ms": {
        "default": 200, "min": 50, "max": 1000, "unit": "ms",
        "label": "常時 RTT モニタ: プローブ間隔",
        "hint": "200ms で帯域負荷 約 50kbps。細かくするほどロス検出が鋭くなる。"
                "変更は該当モニタを OFF→ON した時に反映"},
    "rtt_agg_seconds": {
        "default": 1, "min": 1, "max": 60, "unit": "秒",
        "label": "常時 RTT モニタ: 集計窓 (記録解像度)",
        "hint": "1 秒 = 最高解像度。長期監視でデータ量を抑えたい場合は 5〜10 秒。"
                "変更は該当モニタを OFF→ON した時に反映"},
    "throughput_streams": {
        "default": 1, "min": 1, "max": 32, "unit": "本",
        "label": "スループット並列ストリーム数 (iperf -P 相当)",
        "hint": "衛星は高 BDP のため単一 TCP では回線を埋めきれないことが多い。"
                "4〜8 本にすると実効容量に近づく (回線の実力を測るなら推奨)"},
    "throughput_omit_seconds": {
        "default": 0, "min": 0, "max": 10, "unit": "秒",
        "label": "スロースタート除外時間 (iperf --omit 相当)",
        "hint": "TCP 立ち上がり分を平均から除外。高 RTT の衛星では 1〜2 秒推奨"},
}


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
        # 最新の気象 (理論 SINR の降雨減衰と UI 表示に使う)
        self.latest_weather: Dict[str, Any] = {}
        # ネットワーク情報 (UI 入力のメモ) と負荷試験の実行状態
        self.net_profile: Dict[str, Any] = {}
        self._global_ip: Optional[str] = None
        self.load_status: Dict[str, Any] = {"running": False, "results": []}
        # 測定パラメータの UI 上書き (SETTINGS_SPEC のキーのみ) と間隔上書き
        self.tuning: Dict[str, float] = {}
        self._interval_overrides: Dict[str, float] = {}

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
            "tuning": self.tuning,
            "intervals": {jid: j["interval_s"] for jid, j in self.jobs.items()
                          if j["kind"] in INTERVAL_RANGES},
        }
        try:
            with open(self._state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
        except OSError as e:
            log.warning("UI 状態の保存失敗: %s", e)

    # ---- 個別ジョブの中身 ---------------------------------------------------
    def _rf_job(self, collector):
        for s in collector.poll_many(time.time()):
            self.storage.add_rf(s)

    def _net_defaults(self) -> Dict[str, Any]:
        nq = self.config.netqual
        d = {
            "control_port": int(nq.get("control_port", 5301)),
            "udp_port": int(nq.get("udp_port", 5302)),
            "interval_s": float(nq.get("interval_s", 30)),
            "throughput_seconds": float(nq.get("throughput_seconds", 5)),
            "udp_probe_count": int(nq.get("udp_probe_count", 200)),
            "udp_probe_interval_ms": float(nq.get("udp_probe_interval_ms", 20)),
            "rtt_probe_interval_ms": 200.0,
            "rtt_agg_seconds": 1.0,
            "throughput_streams": int(nq.get("throughput_streams", 1)),
            "throughput_omit_seconds": float(nq.get("throughput_omit_seconds", 0)),
        }
        for k, v in self.tuning.items():     # UI での上書きを反映 (次回測定から有効)
            if k in d:
                d[k] = type(d[k])(v)
        return d

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
            bind_ip=target.get("bind_ip"),
            streams=int(target.get("streams", d["throughput_streams"])),
            omit_seconds=d["throughput_omit_seconds"],
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
        # 各コンステレーションの「次のハンドオーバー」をスカイプロット用に付与
        nxt: Dict[str, Any] = {}
        for e in events:
            nxt.setdefault(e.constellation, {
                "ts": e.ts, "to_sat": e.to_sat, "from_sat": e.from_sat,
                "reason": e.reason})
        for con, info in (serving or {}).items():
            if info is not None:
                info["next"] = nxt.get(con)
        self.latest_serving = serving
        for e in events:
            self.storage.add_handover(e)
        self._emit_sinr_model(serving)

    def _weather_job(self) -> None:
        """上空の気象を取得し、定量値として記録 (降雨減衰の参考値付き)."""
        from .weather import fetch_weather
        from .handover.linkbudget import rain_attenuation_db
        gs = self.config.ground_station
        w = fetch_weather(gs.latitude, gs.longitude)
        if w is None:
            return
        # 参考値: Ku 帯・仰角 45° 換算の降雨減衰 (定量比較用の代表値)
        w["rain_atten_ku45_db"] = round(
            rain_attenuation_db(w.get("precip_mmh") or 0.0, 45.0), 3)
        self.latest_weather = w
        self.storage.add_weather(w)
        log.info("気象: 降水 %.1f mm/h / 雲量 %s%% / 気温 %s℃ / 雨減衰(Ku,45°) %.2f dB",
                 w.get("precip_mmh") or 0, w.get("cloud_cover_pct"),
                 w.get("temp_c"), w["rain_atten_ku45_db"])

    def _emit_sinr_model(self, serving: Dict[str, Any]) -> None:
        """接続衛星の距離・仰角から理論 SINR を計算し、実測と同じ形式で記録.

        source="model:<constellation>" / kind="model" の RFSample として保存
        されるため、実測 SINR とそのまま重ね描き・CSV 比較ができる。
        """
        cfgm = self.config.raw.get("sinr_model") or {}
        # config に sinr_model セクションが無くても既定で有効 (既定パラメータで動作)
        if not cfgm.get("enabled", True):
            return
        from .model import RFSample
        from .handover.linkbudget import estimate_sinr_db, PARAM_KEYS
        now = time.time()
        # 気象モニタの最新降水強度を降雨減衰として反映 (use_weather: false で無効化)
        rain_mmh = 0.0
        if cfgm.get("use_weather", True):
            rain_mmh = float(self.latest_weather.get("precip_mmh") or 0.0)
        for con, info in (serving or {}).items():
            if not info or info.get("elevation_deg") is None \
                    or info.get("range_km") is None:
                continue
            params = {k: float(v) for k, v in (cfgm.get(con) or {}).items()
                      if k in PARAM_KEYS}
            est = estimate_sinr_db(range_km=float(info["range_km"]),
                                   elevation_deg=float(info["elevation_deg"]),
                                   rain_mmh=rain_mmh, **params)
            self.storage.add_rf(RFSample(
                ts=now, source=f"model:{con}", kind="model",
                sinr_db=round(est, 2),
                elevation_deg=info.get("elevation_deg"),
                azimuth_deg=info.get("azimuth_deg"),
                satellite_id=info.get("satellite")))

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
                self.netqual_server = NetqualServer(
                    d["control_port"], d["udp_port"],
                    allowed_sources=self.config.netqual.get("allowed_sources"))
                self.netqual_server.start()
        elif j["kind"] == "rttmon":
            if j["thread"] is None:
                from .netqual import rttmon
                d = self._net_defaults()
                meta = j["meta"]
                stop = threading.Event()
                j["stop_evt"] = stop
                j["thread"] = threading.Thread(
                    target=rttmon.run_monitor, daemon=True, name=job_id,
                    kwargs=dict(
                        host=meta["host"],
                        udp_port=int(meta.get("udp_port", d["udp_port"])),
                        stop=stop, on_sample=self.storage.add_net,
                        session=meta.get("label") or meta["host"],
                        probe_interval_ms=d["rtt_probe_interval_ms"],
                        agg_seconds=d["rtt_agg_seconds"],
                        bind_ip=meta.get("bind_ip")))
                j["thread"].start()
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
        elif j["kind"] == "rttmon":
            if j["thread"] is not None:
                j["stop_evt"].set()
                j["thread"] = None
        elif j["thread"] is not None:
            j["thread"].stop()
            j["thread"] = None
        j["enabled"] = False

    # ---- Web UI 向け公開 API -------------------------------------------------
    def set_job_interval(self, job_id: str, interval_s: float) -> Optional[str]:
        """実行間隔を変更 (即時反映・保存)。エラー文字列を返す (None=成功)."""
        with self._lock:
            j = self.jobs.get(job_id)
            if not j:
                return "ジョブが見つかりません"
            rng = INTERVAL_RANGES.get(j["kind"])
            if rng is None:
                return "このジョブの間隔は変更できません"
            try:
                iv = float(interval_s)
            except (TypeError, ValueError):
                return "数値で指定してください"
            iv = min(max(iv, rng["min"]), rng["max"])
            j["interval_s"] = iv
            if j["thread"] is not None:      # 稼働中なら新しい間隔で再起動
                j["thread"].stop()
                j["thread"] = _Periodic(job_id, iv, j["fn"])
                j["thread"].start()
            self._save_state()
            log.info("ジョブ %s の間隔を %.0f 秒に変更", job_id, iv)
            return None

    def get_settings(self) -> Dict[str, Any]:
        """測定パラメータの現在値と範囲/既定値メモ (UI の「測定設定」用)."""
        d = self._net_defaults()
        out = {}
        for key, spec in SETTINGS_SPEC.items():
            out[key] = {**spec, "value": d.get(key, spec["default"])}
        return out

    def set_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for key, spec in SETTINGS_SPEC.items():
                if key not in data:
                    continue
                try:
                    v = float(data[key])
                except (TypeError, ValueError):
                    continue
                self.tuning[key] = min(max(v, spec["min"]), spec["max"])
            self._save_state()
        return self.get_settings()

    def jobs_status(self) -> List[Dict[str, Any]]:
        order = {"rf": 0, "net": 1, "rttmon": 2, "handover": 3,
                 "weather": 4, "server": 5}
        with self._lock:
            rows = []
            for j in self.jobs.values():
                r = {k: j[k] for k in
                     ("id", "label", "kind", "interval_s", "enabled",
                      "removable", "detail")}
                rng = INTERVAL_RANGES.get(j["kind"])
                if rng:                       # UI の間隔入力欄 (範囲メモ付き)
                    r["interval_min"] = rng["min"]
                    r["interval_max"] = rng["max"]
                    r["interval_hint"] = rng["hint"]
                rows.append(r)
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

    def _register_rttmon(self, slug: str, meta: Dict[str, Any],
                         enabled: bool, removable: bool) -> None:
        """net ターゲットに対応する常時 RTT モニタジョブを登録する."""
        self._register(
            f"rttmon:{slug}", label=meta.get("label") or meta.get("host", ""),
            kind="rttmon", interval_s=0, fn=None,
            enabled=enabled, removable=removable,
            detail=f"→ {meta.get('host')} (1秒解像度)", meta=meta)

    def add_net_target(self, label: str, host: str, *, enabled: bool = True,
                       rtt_enabled: bool = False, **overrides) -> Optional[str]:
        """UI から測定先を追加 (Ether/Wi-Fi チェック等)。job_id を返す."""
        label = (label or host).strip()
        host = host.strip()
        if not host:
            return None
        slug = _slug(label)
        job_id = f"net:{slug}"
        with self._lock:
            if job_id in self.jobs:
                return None  # 同名は不可
            meta = {"label": label, "host": host, **{
                k: v for k, v in overrides.items() if v is not None}}
            d = self._net_defaults()
            detail = f"→ {host}" + (
                f" (src {meta['bind_ip']})" if meta.get("bind_ip") else "")
            self._register(
                job_id, label=label, kind="net",
                interval_s=float(meta.get("interval_s", d["interval_s"])),
                fn=lambda t=meta: self._net_job(t),
                enabled=enabled, removable=True, detail=detail, meta=meta)
            self._register_rttmon(slug, meta, rtt_enabled, removable=True)
            self._save_state()
        return job_id

    def remove_net_target(self, job_id: str) -> bool:
        with self._lock:
            j = self.jobs.get(job_id)
            if not j or not j.get("removable"):
                return False
            self._stop_job(job_id)
            del self.jobs[job_id]
            sibling = "rttmon:" + job_id[len("net:"):]   # 対応する RTT モニタも削除
            if sibling in self.jobs:
                self._stop_job(sibling)
                del self.jobs[sibling]
            self._save_state()
            return True

    # ---- ネットワーク情報 (UI 入力 + 自動検出) --------------------------------
    _PROFILE_KEYS = ("line_type", "local_ip_note", "peer_global_ip", "note")

    def get_netinfo(self) -> Dict[str, Any]:
        import socket as _s
        ips = set()
        adapters: List[Dict[str, str]] = []
        # psutil があればアダプタ名付きで全 NIC を列挙 (マルチ NIC 測定用)
        try:
            import psutil  # type: ignore
            for name, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == _s.AF_INET and not a.address.startswith("127."):
                        adapters.append({"name": name, "ip": a.address})
                        ips.add(a.address)
        except ImportError:
            pass
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
            "adapters": adapters,
            "global_ip": self._global_ip,
            "profile": dict(self.net_profile),
            "role": self.config.netqual.get("role", "both"),
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
                        rates_bps: List[float], step_seconds: float,
                        mode: str = "sweep") -> Optional[str]:
        """UI からの負荷/耐久試験開始。エラー文字列を返す (None なら開始成功).

        mode="sweep": レート段階スイープ / mode="soak": 一定レートの耐久試験
        (同一レートをチャンク刻みで流し続け、チャンクごとに記録)
        """
        if direction not in ("uplink", "downlink"):
            return "direction は uplink / downlink"
        if not rates_bps or len(rates_bps) > 400:
            return "ステップ数は 1〜400 で指定してください"
        if len(rates_bps) * step_seconds > 3700:
            return "合計時間は 1 時間以内にしてください"
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
                "mode": mode, "step_s": step_seconds,
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
                bind_ip=meta.get("bind_ip"),
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
        self.tuning = dict(state.get("tuning") or {})
        self._interval_overrides = dict(state.get("intervals") or {})

        def initial(job_id: str, default: bool) -> bool:
            return bool(overrides.get(job_id, default))

        def interval(job_id: str, default: float) -> float:
            return float(self._interval_overrides.get(job_id, default))

        # RF コレクタ: 設定にある全機器を登録 (enabled は初期状態、UI で切替可)
        for cfg in self.config.collectors:
            try:
                col = build_collector(cfg)
            except Exception as e:  # noqa: BLE001
                log.error("コレクタ構築失敗 %s: %s", cfg.get("name"), e)
                continue
            job_id = f"rf:{col.name}"
            self._register(job_id, label=col.name, kind="rf",
                           interval_s=interval(job_id, col.interval_s),
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
                slug = _slug(label)
                job_id = f"net:{slug}"
                d = self._net_defaults()
                detail = f"→ {t.get('host')}" + (
                    f" (src {t['bind_ip']})" if t.get("bind_ip") else "")
                self._register(job_id, label=label, kind="net",
                               interval_s=interval(job_id, float(
                                   t.get("interval_s", d["interval_s"]))),
                               fn=lambda tt=t: self._net_job(tt),
                               enabled=initial(job_id, measure_default
                                               and t.get("enabled", True)),
                               detail=detail, meta=dict(t))
                self._register_rttmon(
                    slug, dict(t),
                    enabled=initial(f"rttmon:{slug}",
                                    bool(t.get("rtt_monitor", False))),
                    removable=False)
            # UI から追加された測定先を復元 (保存されていた ON/OFF 状態も引き継ぐ)
            for t in state.get("targets", []):
                slug = _slug(t.get("label", ""))
                job_id = f"net:{slug}"
                if job_id in self.jobs:
                    continue
                self.add_net_target(t.get("label", ""), t.get("host", ""),
                                    enabled=initial(job_id, True),
                                    rtt_enabled=initial(f"rttmon:{slug}", False),
                                    **{k: v for k, v in t.items()
                                       if k not in ("label", "host")})
                if job_id in self.jobs and job_id in self._interval_overrides:
                    self.jobs[job_id]["interval_s"] = interval(
                        job_id, self.jobs[job_id]["interval_s"])

        # 気象 (雨雲) モニタ: 理論 SINR の降雨減衰と定量記録に使う
        wcfg = self.config.raw.get("weather") or {}
        if wcfg.get("enabled", True):
            self._register("weather", label="気象 (雨雲) モニタ", kind="weather",
                           interval_s=interval("weather",
                                               float(wcfg.get("interval_s", 300))),
                           fn=self._weather_job,
                           enabled=initial("weather", True),
                           detail=f"Open-Meteo @ ({self.config.ground_station.latitude:.2f}, "
                                  f"{self.config.ground_station.longitude:.2f})")

        # ハンドオーバー予測
        ho = self.config.handover
        if ho:
            cache_dir = os.path.join(os.path.dirname(self.config.db_path) or ".", "tle")
            predictor = HandoverPredictor(ho, self.config.ground_station, cache_dir)
            self._register("handover", label="ハンドオーバー予測", kind="handover",
                           interval_s=interval("handover",
                                               float(ho.get("interval_s", 30))),
                           fn=lambda p=predictor: self._handover_job(p),
                           enabled=initial("handover", bool(ho.get("enabled", True))),
                           detail="TLE+SGP4")

        enabled_n = sum(1 for j in self.jobs.values() if j["enabled"])
        log.info("ジョブ登録: %d 件 (稼働 %d 件) — Web UI で切替できます",
                 len(self.jobs), enabled_n)

    def stop(self) -> None:
        for job_id in list(self.jobs):
            self._stop_job(job_id)
