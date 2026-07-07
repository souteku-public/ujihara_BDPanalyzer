"""SQLite による時系列ストレージ.

スレッドセーフに使えるよう、各操作ごとに接続を開く簡易実装 (計測周期が秒単位で
競合が少ないため十分)。SINR とネット品質の対比は ``correlated()`` で時間窓を
突き合わせて返す。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Dict, List, Optional

from .model import RFSample, NetSample, HandoverEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rf_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, source TEXT, kind TEXT,
    sinr_db REAL, snr_above_noise INTEGER, rssi_dbm REAL,
    azimuth_deg REAL, elevation_deg REAL, tilt_deg REAL,
    tx_freq_mhz REAL, rx_freq_mhz REAL, beam_id TEXT, satellite_id TEXT,
    down_bps REAL, up_bps REAL, latency_ms REAL, drop_rate REAL,
    obstruction_pct REAL, state TEXT
);
CREATE INDEX IF NOT EXISTS idx_rf_ts ON rf_samples(ts);

CREATE TABLE IF NOT EXISTS net_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, session TEXT, direction TEXT,
    throughput_bps REAL, rtt_ms REAL, rtt_min_ms REAL, rtt_max_ms REAL,
    jitter_ms REAL, loss_pct REAL, owd_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_net_ts ON net_samples(ts);

CREATE TABLE IF NOT EXISTS load_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, session TEXT, direction TEXT,
    offered_bps REAL, achieved_bps REAL, loss_pct REAL, jitter_ms REAL, rtt_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_load_ts ON load_tests(ts);

CREATE TABLE IF NOT EXISTS weather_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    precip_mmh REAL, rain_mmh REAL, cloud_cover_pct REAL,
    temp_c REAL, humidity_pct REAL, weather_code INTEGER,
    wind_speed_ms REAL, rain_atten_ku45_db REAL
);
CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather_samples(ts);

CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_markers_ts ON markers(ts);

CREATE TABLE IF NOT EXISTS handover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, constellation TEXT, from_sat TEXT, to_sat TEXT,
    reason TEXT, from_elevation_deg REAL, to_elevation_deg REAL, lead_time_s REAL
);
CREATE INDEX IF NOT EXISTS idx_ho_ts ON handover_events(ts);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ---- 書き込み --------------------------------------------------------
    def _insert(self, table: str, row: Dict[str, Any]) -> None:
        # bool -> int 変換
        row = {k: (int(v) if isinstance(v, bool) else v) for k, v in row.items()}
        cols = ", ".join(row.keys())
        ph = ", ".join("?" for _ in row)
        with self._lock, self._connect() as con:
            con.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", tuple(row.values()))

    def add_rf(self, s: RFSample) -> None:
        self._insert("rf_samples", s.as_row())

    def add_net(self, s: NetSample) -> None:
        self._insert("net_samples", s.as_row())

    def add_handover(self, e: HandoverEvent) -> None:
        self._insert("handover_events", e.as_row())

    def add_load_result(self, row: Dict[str, Any]) -> None:
        keys = ("ts", "session", "direction", "offered_bps", "achieved_bps",
                "loss_pct", "jitter_ms", "rtt_ms")
        self._insert("load_tests", {k: row.get(k) for k in keys})

    def add_weather(self, row: Dict[str, Any]) -> None:
        keys = ("ts", "precip_mmh", "rain_mmh", "cloud_cover_pct", "temp_c",
                "humidity_pct", "weather_code", "wind_speed_ms",
                "rain_atten_ku45_db")
        self._insert("weather_samples", {k: row.get(k) for k in keys})

    def recent_weather(self, since_ts: float) -> List[Dict[str, Any]]:
        return self._query(
            "SELECT * FROM weather_samples WHERE ts>=? ORDER BY ts", (since_ts,))

    def add_marker(self, ts: float, text: str) -> None:
        """実験マーカー (例: 'アンテナ間距離 1.5m に変更') を記録."""
        self._insert("markers", {"ts": ts, "text": (text or "").strip()[:500]})

    def recent_markers(self, since_ts: float) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM markers WHERE ts>=? ORDER BY ts",
                           (since_ts,))

    # ---- 読み出し --------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def recent_rf(self, since_ts: float, source: Optional[str] = None) -> List[Dict[str, Any]]:
        if source:
            return self._query(
                "SELECT * FROM rf_samples WHERE ts>=? AND source=? ORDER BY ts",
                (since_ts, source))
        return self._query("SELECT * FROM rf_samples WHERE ts>=? ORDER BY ts", (since_ts,))

    def recent_net(self, since_ts: float) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM net_samples WHERE ts>=? ORDER BY ts", (since_ts,))

    def recent_load_tests(self, since_ts: float) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM load_tests WHERE ts>=? ORDER BY ts",
                           (since_ts,))

    def recent_handovers(self, since_ts: float) -> List[Dict[str, Any]]:
        return self._query(
            "SELECT * FROM handover_events WHERE ts>=? ORDER BY ts", (since_ts,))

    def sources(self) -> List[str]:
        return [r["source"] for r in self._query(
            "SELECT DISTINCT source FROM rf_samples WHERE source IS NOT NULL")]

    def correlated(self, since_ts: float, window_s: float = 30.0) -> List[Dict[str, Any]]:
        """各ネット品質サンプルに、時間窓内で最も近い RF サンプルの SINR を付与.

        SINR とスループット/ジッタ/RTT の対比グラフ用データを返す。
        """
        nets = self.recent_net(since_ts)
        rfs = [r for r in self.recent_rf(since_ts) if r.get("sinr_db") is not None]
        out: List[Dict[str, Any]] = []
        for n in nets:
            best = None
            best_dt = window_s
            for r in rfs:
                dt = abs(r["ts"] - n["ts"])
                if dt <= best_dt:
                    best, best_dt = r, dt
            merged = dict(n)
            merged["sinr_db"] = best["sinr_db"] if best else None
            merged["rf_source"] = best["source"] if best else None
            out.append(merged)
        return out
