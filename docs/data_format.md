# データ保存形式

取得データはすべて **SQLite 1 ファイル**（既定: `data/bdp.sqlite`）に蓄積されます。
設計方針は「**どのネットワーク・どの機器で測っても同じ列構成**」です。
機器や回線の違いは列の中の値（`source` / `kind` / `session`）で区別し、
列そのものは変わりません。機種が出せない項目は空（NULL）になるだけです。

時刻はすべて **UTC epoch 秒**（小数、`ts` 列）。CSV エクスポート時は
Excel 等で読みやすいよう ISO 形式のローカル時刻列 `time_iso` が付きます。

## 取り出し方

| 方法 | 手順 |
|---|---|
| **CSV**（いちばん手軽） | ダッシュボード右上の「CSV: RF / 通信品質 / 負荷試験 / HO」リンク。表示中の期間分が落ちる。URL 直叩きも可: `/export/net.csv?minutes=1440`（=直近24時間） |
| **JSON API** | `/api/rf` `/api/net` `/api/loadtests` `/api/handovers`（`?minutes=` 指定）、SINR 突き合わせ済みは `/api/correlation` |
| **SQLite 直接** | `sqlite3 data/bdp.sqlite` や Python の `pandas.read_sql`、DB Browser for SQLite 等 |

Python (pandas) の例:
```python
import sqlite3, pandas as pd
con = sqlite3.connect("data/bdp.sqlite")
net = pd.read_sql("SELECT * FROM net_samples", con, parse_dates={"ts": "s"})
```

## テーブル定義

### net_samples — 通信品質（回線種別によらず共通）

| 列 | 型 | 単位/値 | 説明 |
|---|---|---|---|
| ts | REAL | epoch 秒 (UTC) | 測定時刻 |
| session | TEXT | — | 測定先ラベル（例: `Starlink経由` / `社内有線LAN` / `Wi-Fi`）。**回線の区別はこの列だけ** |
| direction | TEXT | `downlink` / `uplink` / `rtt` | 測定方向。`rtt` は**常時 RTT モニタ**（1 秒集計の往復測定、throughput は NULL） |
| throughput_bps | REAL | bps | TCP スループット |
| rtt_ms / rtt_min_ms / rtt_max_ms | REAL | ms | UDP エコーの往復遅延（平均/最小/最大） |
| jitter_ms | REAL | ms | RFC3550 相当の平滑化ジッタ |
| loss_pct | REAL | %(0-100) | UDP プローブのロス率 |
| owd_ms | REAL | ms | 片道遅延（時刻同期時のみ。現状は NULL） |

### rf_samples — 電波側メトリクス（機器種別によらず共通）

| 列 | 型 | 単位/値 | 説明 |
|---|---|---|---|
| ts | REAL | epoch 秒 (UTC) | 取得時刻 |
| source | TEXT | — | コレクタ名（config の `name`。例: `starlink-mini-1`）。**`:1s` 付き（例: `starlink-mini-1:1s`）は get_history 由来の 1 秒解像度サンプル**（遅延・上下スループット・ドロップ率のみ、向き等は NULL）。**`model:starlink` / `model:oneweb` はリンクバジェットによる理論 SINR**（実測との差分 Δ が干渉等の劣化推定量） |
| kind | TEXT | `starlink` / `kymeta` / `intellian` / `model` | 機器種別（`model` は理論値行） |
| sinr_db | REAL | dB | SINR/SNR（Starlink 新ファームは NULL） |
| snr_above_noise | INTEGER | 0/1 | SNR がノイズフロア以上か（Starlink） |
| rssi_dbm | REAL | dBm | 受信強度（機器が出す場合） |
| azimuth_deg / elevation_deg / tilt_deg | REAL | 度 | アンテナの向き（方位/仰角/チルト） |
| tx_freq_mhz / rx_freq_mhz | REAL | MHz | 使用周波数（Starlink は非公開→NULL） |
| beam_id / satellite_id | TEXT | — | ビーム/接続衛星（判る機器のみ） |
| down_bps / up_bps | REAL | bps | 端末が自己申告するスループット |
| latency_ms | REAL | ms | 端末の対 PoP 遅延（Starlink） |
| drop_rate | REAL | 0-1 | 端末の対 PoP ドロップ率 |
| obstruction_pct | REAL | %(0-100) | 障害物（遮蔽）率 |
| state | TEXT | — | 端末状態（例: `CONNECTED`） |

### load_tests — 負荷耐性テスト（レートスイープ、1 行 = 1 ステップ）

| 列 | 型 | 単位/値 | 説明 |
|---|---|---|---|
| ts | REAL | epoch 秒 (UTC) | ステップ完了時刻 |
| session | TEXT | — | 測定先ラベル |
| direction | TEXT | `uplink` / `downlink` | 負荷の方向 |
| offered_bps | REAL | bps | 送出（オファー）レート |
| achieved_bps | REAL | bps | 実効レート（実際に届いた分） |
| loss_pct | REAL | % | ロス率 |
| jitter_ms | REAL | ms | ジッタ |
| rtt_ms | REAL | ms | **負荷時 RTT**（並行プローブによる） |

### weather_samples — 上空の気象（Open-Meteo、定量記録）

| 列 | 型 | 単位 | 説明 |
|---|---|---|---|
| ts | REAL | epoch 秒 (UTC) | 取得時刻 |
| precip_mmh / rain_mmh | REAL | mm/h | 降水強度（15 分値×4 換算）。precip は雪等含む総降水 |
| cloud_cover_pct | REAL | % | 雲量 |
| temp_c / humidity_pct | REAL | ℃ / % | 気温・湿度 |
| weather_code | INTEGER | WMO コード | 0=快晴, 3=曇り, 61-65=雨, 95=雷雨 等 |
| wind_speed_ms | REAL | m/s | 風速 (10m) |
| rain_atten_ku45_db | REAL | dB | **参考値**: Ku 帯・仰角 45° 換算の降雨減衰。理論 SINR には各衛星の実仰角で個別に反映される |

### markers — 実験マーカー

| 列 | 型 | 説明 |
|---|---|---|
| ts | REAL | 記録時刻 (epoch 秒 UTC) |
| text | TEXT | 内容（例: 「アンテナ間距離 1.5m に変更」）。条件変更点をデータに刻む用途 |

### handover_events — ハンドオーバー予測/記録

| 列 | 型 | 単位/値 | 説明 |
|---|---|---|---|
| ts | REAL | epoch 秒 (UTC) | 予測される発生時刻（未来もあり得る） |
| constellation | TEXT | `starlink` / `oneweb` | 対象コンステレーション |
| from_sat / to_sat | TEXT | — | 切替元/先の衛星名（NULL = 圏外） |
| reason | TEXT | `better_candidate` / `elevation_mask` / `acquisition` / `los` | 切替理由 |
| from_elevation_deg / to_elevation_deg | REAL | 度 | 切替時点の仰角 |
| lead_time_s | REAL | 秒 | 予測時点から発生までの先読み時間 |

## 分析でよく使う突き合わせ

SINR とネット品質の対比は `ts` の近接（既定 ±30 秒）で結合します。
アプリ内では `/api/correlation` が同じロジックで結合済みデータを返します。

```sql
-- 例: 各ネット品質サンプルに最も近い RF サンプルの SINR を付ける
SELECT n.*, (SELECT r.sinr_db FROM rf_samples r
             WHERE ABS(r.ts - n.ts) < 30 ORDER BY ABS(r.ts - n.ts) LIMIT 1) AS sinr_db
FROM net_samples n;
```
