# アーキテクチャ

## 全体像

```
                ┌─────────────────────── メイン PC (sender / monitor) ───────────────────────┐
                │                                                                            │
  端末群 ──RF──▶│  collectors ─┐                                                             │
  Starlink gRPC │  (Starlink /  │                                                            │
  Kymeta  HTTPS │   Kymeta /    ├─▶ Storage(SQLite) ─▶ webapp(Flask+Chart.js) ─▶ ブラウザ    │
  Intellian     │   Intellian)  │        ▲                                                   │
                │  handover ────┘        │                                                   │
                │  (TLE+SGP4) ───────────┘                                                   │
                │  netqual.client ───────────────┐ 測定                                       │
                └────────────────────────────────┼──────────────────────────────────────────┘
                                                  │ TCP(スループット)/UDP(RTT・ジッタ・ロス)
                ┌─────────────────────────────────▼── 対向 PC (receiver) ────────────────────┐
                │  netqual.server (TCP 制御 + UDP エコー)                                      │
                └────────────────────────────────────────────────────────────────────────────┘
```

- **orchestrator** は各計測を「ジョブ」（`rf:<name>` / `net:<label>` / `handover` /
  `netqual-server`）として管理し、Web UI から実行時に ON/OFF・測定先追加ができる。
  UI での変更は `data/ui_state.json` に保存され再起動後も引き継ぐ（config.yaml は不変更）。
  測定先は複数持て、`NetSample.session` にラベルが入りグラフはラベル別に描画される。
- **webapp** は Storage を読むだけの薄い層。API(JSON) + 1 枚の SPA ダッシュボード。
- 各機能は疎結合。外部依存が無くても本体は起動し、該当機能のみ無効化。

## データモデル（`model.py`）
- `RFSample` … 電波側 1 点（SINR・向き・周波数・端末スループット/遅延/障害物）。
- `NetSample` … ネット品質 1 点（スループット・RTT・ジッタ・ロス、方向別）。
- `HandoverEvent` … ハンドオーバー予測 1 件（from/to 衛星・理由・仰角・先読み秒）。

## SINR とネット品質の対比
`Storage.correlated()` が、各 `NetSample` に対し時間窓（既定 ±30 s）内で最も近い
`RFSample.sinr_db` を突き合わせて返します。ダッシュボードはこれを二軸グラフで
重ね描きし、`/api/correlation` でも取得できます。

## ネット品質測定プロトコル（`netqual/protocol.py`）
外部ツール非依存の自己完結測定。1 コネクション 1 計測でストリーム同期ズレを回避。

**送受兼用(role=both)**: orchestrator は受信サーバ(`NetqualServer`)を常駐させつつ、
`peer_host` へ能動測定スレッドを回す。両 PC で同一ソフトを動かし互いを peer に
指定すると対称な双方向測定になる。上り/下りは 1 回の測定で両方採取するため、
役割を分けても兼用でも取得内容は同じ(機能低下なし)。

- **スループット**: TCP。`TP_DOWN`(受信側→送信側バルク) / `TP_UP`(送信側→受信側)。
  アップは受信側が実受信バイト数を返し、実効値を採用。
- **RTT / ジッタ / ロス**: UDP エコー。送信時刻をプローブに載せ往復させるため
  **2 台の時刻同期は不要**。ジッタは RTT 列の RFC3550 相当平滑化偏差。
  片道遅延(`owd_ms`)は NTP/PTP 同期時に拡張可能なフィールドを用意済み。

## ハンドオーバー予測（`handover/`）
1. CelesTrak から Starlink/OneWeb の TLE を取得・キャッシュ（`tle.py`）。
2. skyfield/SGP4 で自局から見た全可視衛星の仰角を時間配列で計算（`predict.py`）。
3. **サービス衛星 = 仰角最大の可視衛星**という近似で、時間を先送りしながら
   サービス衛星が入れ替わる時刻をハンドオーバーとして検出。現サービス衛星が
   仰角マスクを割る時刻は `reason="elevation_mask"` として先読み。

> 実際の割当は事業者非公開スケジューラのため、あくまで幾何ベースの推定です。
> 「そろそろ切替が起きそう」という運用先読み・SINR 変動との突き合わせに有効。

## 拡張ポイント
- 新しい端末 → `collectors/` に `Collector` サブクラスを追加し `__init__.py` に登録。
- 保存先変更 → `Storage` を差し替え（InfluxDB/Prometheus 等）。
- 片道遅延・上り下り個別のジッタ → `netqual` プロトコルにフィールド追加。
