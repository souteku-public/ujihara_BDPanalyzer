"""BDP Analyzer — LEO 衛星通信 / インターネット通信 品質モニタリング.

Beam / Data-Path (BDP) Analyzer:
    - RF コレクタ  : Starlink Mini (gRPC) / Kymeta (WebGUI) / OneWeb Intellian
    - ネット品質    : スループット・ジッタ・RTT・パケットロス (送受 2 台構成対応)
    - ハンドオーバー予測 : CelesTrak TLE + SGP4 による衛星可視予測
    - ダッシュボード : SINR vs スループット/ジッタ/RTT の対比表示、衛星マップ

パッケージは HW が無くても import / 起動できるよう、外部依存は遅延 import で扱う。
"""

__version__ = "0.1.0"
