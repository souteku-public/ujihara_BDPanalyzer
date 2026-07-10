"""共通データモデル.

すべてのサンプルは UTC epoch 秒 (float) の ``ts`` を持つ。値が取得できない項目は
``None`` を許容する (機種差・ファーム差でフィールドが存在しないため)。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any


@dataclass
class RFSample:
    """アンテナ / 端末から取得した電波(RF)側メトリクス 1 点."""

    ts: float
    source: str                      # コレクタ名 (例: "starlink-mini-1")
    kind: str                        # "starlink" | "kymeta" | "intellian"

    # 信号品質
    sinr_db: Optional[float] = None          # SINR / SNR [dB]
    snr_above_noise: Optional[bool] = None   # Starlink: is_snr_above_noise_floor
    rssi_dbm: Optional[float] = None

    # アンテナの向き
    azimuth_deg: Optional[float] = None      # 方位角 (真北基準, 0-360)
    elevation_deg: Optional[float] = None    # 仰角 (地平線基準, 0-90)
    tilt_deg: Optional[float] = None

    # 周波数
    tx_freq_mhz: Optional[float] = None
    rx_freq_mhz: Optional[float] = None
    beam_id: Optional[str] = None
    satellite_id: Optional[str] = None       # 接続中衛星 (判れば)

    # 端末が自前で持つ通信量・遅延 (Starlink dish 等)
    down_bps: Optional[float] = None
    up_bps: Optional[float] = None
    latency_ms: Optional[float] = None
    drop_rate: Optional[float] = None        # 0.0-1.0

    # 障害物 / 状態
    obstruction_pct: Optional[float] = None  # 0.0-100.0
    state: Optional[str] = None              # "CONNECTED" 等

    raw: Dict[str, Any] = field(default_factory=dict)  # 生データ (デバッグ用)

    def as_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


@dataclass
class NetSample:
    """インターネット通信品質メトリクス 1 点."""

    ts: float
    session: str                     # 測定セッション名 (対向 host など)
    direction: str                   # "downlink" | "uplink"

    throughput_bps: Optional[float] = None
    rtt_ms: Optional[float] = None           # 平均 RTT
    rtt_min_ms: Optional[float] = None
    rtt_max_ms: Optional[float] = None
    jitter_ms: Optional[float] = None        # RFC3550 相当
    loss_pct: Optional[float] = None         # 0.0-100.0
    out_of_order_pct: Optional[float] = None # 順序逆転パケット率 0.0-100.0
    owd_ms: Optional[float] = None           # 片道遅延 (時刻同期時のみ有効)
    streams: Optional[int] = None            # スループット測定の並列ストリーム数

    def as_row(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HandoverEvent:
    """ハンドオーバー(接続衛星切替)予測イベント."""

    ts: float                        # 予測発生時刻 (epoch 秒)
    constellation: str               # "starlink" | "oneweb"
    from_sat: Optional[str]
    to_sat: Optional[str]
    reason: str                      # "elevation_mask" | "better_candidate" | "los"
    from_elevation_deg: Optional[float] = None
    to_elevation_deg: Optional[float] = None
    lead_time_s: Optional[float] = None      # 予測時点から発生までの秒数

    def as_row(self) -> Dict[str, Any]:
        return asdict(self)
