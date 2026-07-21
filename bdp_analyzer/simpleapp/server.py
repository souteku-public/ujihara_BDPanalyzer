"""シンプル計測アプリ.

1 台の端末 (Kymeta / Intellian / Starlink) の WebGUI・API を一定間隔 (既定 5 秒)
でポーリングし、

  - リアルタイムグラフ表示 (SINR / アンテナ向き / 周波数など)
  - UI 上のボタンで開始/停止できる CSV 記録
  - (依存が入っていれば) 衛星ハンドオーバーの理論予測の表示

だけを行う軽量ダッシュボード。送信側/受信側や通信品質測定の概念は持たない。

起動:
    python -m bdp_analyzer simple --config config.yaml
    → http://localhost:8080/
"""
from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request, send_file

from ..collectors import build_collector

log = logging.getLogger("bdp.simple")

# CSV / グラフに載せる列 (RFSample のうち raw 以外)
CSV_FIELDS = [
    "sinr_db", "rssi_dbm", "azimuth_deg", "elevation_deg", "tilt_deg",
    "tx_freq_mhz", "rx_freq_mhz", "satellite_id", "beam_id",
    "down_bps", "up_bps", "latency_ms", "drop_rate",
    "obstruction_pct", "state",
]

# コレクタ種別 → ハンドオーバー予測で使うコンステレーション名
_KIND_TO_CON = {"kymeta": "oneweb", "intellian": "oneweb",
                "starlink": "starlink"}


class SimpleMonitor:
    """端末 1 台のポーリング + リングバッファ + CSV 記録."""

    def __init__(self, collector_cfg: Dict[str, Any], *,
                 interval_s: float = 5.0,
                 records_dir: str = "records",
                 handover_cfg: Optional[Dict[str, Any]] = None,
                 ground_station: Any = None,
                 tle_cache_dir: str = "data/tle"):
        self.collector = build_collector(collector_cfg)
        self.name = collector_cfg.get("name", self.collector.kind)
        self.kind = collector_cfg.get("kind", "")
        self.base_url = collector_cfg.get("base_url") or collector_cfg.get(
            "address", "")
        self.interval_s = float(interval_s)
        self.records_dir = records_dir

        # 直近 4 時間ぶん (5 秒間隔で 2880 点) を保持
        n = max(int(4 * 3600 / max(self.interval_s, 1)), 600)
        self.buf: deque = deque(maxlen=n)
        self.last_ts: Optional[float] = None
        self.last_error: Optional[str] = None

        # 記録状態
        self._rec_lock = threading.Lock()
        self._rec_fh = None
        self._rec_writer = None
        self.rec_path: Optional[str] = None
        self.rec_rows = 0
        self.rec_started: Optional[float] = None
        self.last_rec_path: Optional[str] = None   # 直近の (停止済み含む) CSV

        # ハンドオーバー予測 (任意・依存が無ければ自動で無効)
        self.constellation = _KIND_TO_CON.get(self.kind)
        self.handover_cfg = handover_cfg or {}
        self.ground_station = ground_station
        self.tle_cache_dir = tle_cache_dir
        self.ho_next: Optional[Dict[str, Any]] = None
        self.ho_serving: Optional[Dict[str, Any]] = None
        self.ho_error: Optional[str] = None

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # ---- ライフサイクル ---------------------------------------------------
    def start(self) -> None:
        t = threading.Thread(target=self._poll_loop, daemon=True,
                             name="simple-poll")
        t.start()
        self._threads.append(t)
        if self.handover_cfg.get("enabled") and self.constellation \
                and self.ground_station is not None:
            th = threading.Thread(target=self._handover_loop, daemon=True,
                                  name="simple-ho")
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        self.record_stop()
        for t in self._threads:
            t.join(timeout=3)

    # ---- ポーリング --------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                sample = self.collector.poll(t0)
            except Exception as e:  # noqa: BLE001 — 監視を止めない
                sample = None
                self.last_error = str(e)
            if sample is not None:
                row = {k: v for k, v in sample.as_row().items()
                       if k in CSV_FIELDS}
                row["ts"] = sample.ts
                self.buf.append(row)
                self.last_ts = sample.ts
                self.last_error = None
                self._write_csv(row)
            elif self.last_error is None:
                self.last_error = ("値を抽出できませんでした。endpoints/field_map が"
                                   "実機と不一致の可能性 (probe コマンドで探索可)")
            # 取得時間ぶんを差し引いて周期を維持
            self._stop.wait(max(0.5, self.interval_s - (time.time() - t0)))

    # ---- CSV 記録 ----------------------------------------------------------
    def record_start(self) -> Dict[str, Any]:
        with self._rec_lock:
            if self._rec_fh is not None:
                return self.record_status()
            os.makedirs(self.records_dir, exist_ok=True)
            fname = f"{self.name}_{datetime.now():%Y%m%d_%H%M%S}.csv"
            self.rec_path = os.path.join(self.records_dir, fname)
            # Excel 互換のため BOM 付き UTF-8
            self._rec_fh = open(self.rec_path, "w", newline="",
                                encoding="utf-8-sig")
            self._rec_writer = csv.writer(self._rec_fh)
            self._rec_writer.writerow(["ts", "time_iso"] + CSV_FIELDS)
            self.rec_rows = 0
            self.rec_started = time.time()
            self.last_rec_path = self.rec_path
            log.info("CSV 記録開始: %s", self.rec_path)
            return self.record_status()

    def record_stop(self) -> Dict[str, Any]:
        with self._rec_lock:
            if self._rec_fh is not None:
                self._rec_fh.close()
                log.info("CSV 記録停止: %s (%d 行)", self.rec_path, self.rec_rows)
            self._rec_fh = None
            self._rec_writer = None
            self.rec_path = None
            self.rec_started = None
            return self.record_status()

    def _write_csv(self, row: Dict[str, Any]) -> None:
        with self._rec_lock:
            if self._rec_writer is None:
                return
            iso = datetime.fromtimestamp(row["ts"]).astimezone().isoformat(
                timespec="seconds")
            self._rec_writer.writerow(
                [row["ts"], iso] + ["" if row.get(c) is None else row.get(c)
                                    for c in CSV_FIELDS])
            self._rec_fh.flush()
            self.rec_rows += 1

    def record_status(self) -> Dict[str, Any]:
        return {"active": self._rec_fh is not None,
                "path": self.rec_path,
                "last_path": self.last_rec_path,
                "rows": self.rec_rows,
                "started_ts": self.rec_started}

    # ---- ハンドオーバー予測 (任意) ------------------------------------------
    def _handover_loop(self) -> None:
        try:
            from ..handover.predict import HandoverPredictor
            cfg = dict(self.handover_cfg)
            cfg["constellations"] = [self.constellation]
            predictor = HandoverPredictor(cfg, self.ground_station,
                                          self.tle_cache_dir)
        except Exception as e:  # noqa: BLE001 — 依存なし等は機能ごと無効化
            self.ho_error = f"ハンドオーバー予測は無効 ({e})"
            log.info("%s", self.ho_error)
            return
        interval = float(self.handover_cfg.get("interval_s", 30))
        while not self._stop.is_set():
            try:
                serving, events = predictor.predict(time.time())
                sv = (serving or {}).get(self.constellation)
                if sv:
                    sv = {k: v for k, v in sv.items() if k != "visible"}
                self.ho_serving = sv
                nxt = next((e for e in events
                            if e.constellation == self.constellation), None)
                self.ho_next = nxt.as_row() if nxt else None
                self.ho_error = None
            except Exception as e:  # noqa: BLE001
                self.ho_error = str(e)
            self._stop.wait(interval)

    # ---- API 用スナップショット ----------------------------------------------
    def series(self, minutes: float) -> List[Dict[str, Any]]:
        cut = time.time() - minutes * 60
        return [r for r in list(self.buf) if r["ts"] >= cut]

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "base_url": self.base_url,
            "interval_s": self.interval_s,
            "last_ts": self.last_ts, "last_error": self.last_error,
            "recording": self.record_status(),
            "constellation": self.constellation,
            "handover": {"serving": self.ho_serving, "next": self.ho_next,
                         "error": self.ho_error},
        }


# --------------------------------------------------------------------------
def create_simple_app(mon: SimpleMonitor) -> Flask:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__,
                template_folder=os.path.join(here, "webapp", "templates"),
                static_folder=os.path.join(here, "webapp", "static"))

    @app.route("/")
    def index():
        return render_template("simple.html", name=mon.name, kind=mon.kind,
                               base_url=mon.base_url,
                               interval_s=mon.interval_s)

    @app.route("/api/simple/status")
    def api_status():
        return jsonify(mon.status())

    @app.route("/api/simple/series")
    def api_series():
        minutes = float(request.args.get("minutes", 30))
        return jsonify(mon.series(minutes))

    @app.route("/api/simple/record", methods=["POST"])
    def api_record():
        body = request.get_json(force=True, silent=True) or {}
        action = body.get("action")
        if action == "start":
            return jsonify(mon.record_start())
        if action == "stop":
            return jsonify(mon.record_stop())
        return jsonify({"error": "action は start か stop"}), 400

    @app.route("/record.csv")
    def download_csv():
        path = mon.last_rec_path
        if not path or not os.path.exists(path):
            return "まだ記録がありません", 404
        return send_file(os.path.abspath(path), as_attachment=True,
                         download_name=os.path.basename(path))

    return app


def run(mon: SimpleMonitor, host: str, port: int) -> None:
    app = create_simple_app(mon)
    app.run(host=host, port=port, threaded=True)
