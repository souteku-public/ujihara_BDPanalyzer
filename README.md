# ujihara_BDPanalyzer

**BDP Analyzer** — LEO 衛星通信 / インターネット通信 品質モニタリングソフトウェア

Starlink Mini・OneWeb(Kymeta / Intellian)の**電波(RF)側メトリクス**と、
**インターネット通信品質**(スループット・ジッタ・RTT・ロス)を同時に測定・記録し、
両者を**対比表示**するダッシュボードを提供します。さらに TLE(軌道要素)を用いた
**衛星ハンドオーバー予測**機能を備えます。

> BDP = **B**eam / **D**ata-**P**ath analyzer(電波ビームとデータ経路を同時に分析)

---

## 何ができるか(依頼項目との対応)

| 依頼 | 対応機能 |
|---|---|
| SINR・アンテナの向き・使用周波数を見たい | RF コレクタ（`sinr_db` / `azimuth_deg`・`elevation_deg` / `tx_freq_mhz`・`rx_freq_mhz`） |
| Kymeta の WebGUI から IP 指定で情報記録 | **Kymeta コレクタ**（HTTPS WebGUI をスクレイプ, `config` で IP と項目マッピング） |
| Starlink mini の電波監視ソリューション | **Starlink コレクタ**（Dish の gRPC からボアサイト方位/仰角・SNR系・障害物・遅延を取得） |
| OneWeb Intellian の電波監視ソリューション | **Intellian コレクタ**（LUI スクレイプ / SNMP。取れない項目は TLE 予測で補完） |
| スループット・ジッタ・RTT の測定 | **netqual**（送信/受信 2 台構成の自己完結測定） |
| SINR との対比 | ダッシュボードの「SINR vs スループット / RTT・ジッタ」対比グラフ + `/api/correlation` |
| 両衛星の状況表示 & ハンドオーバー予測 | **handover**（CelesTrak TLE + SGP4 で可視衛星とハンドオーバー時刻を予測） |
| 送信側・受信側で別ソフト | `python -m bdp_analyzer sender` / `receiver` |

---

## 構成

```
bdp_analyzer/
├── collectors/         RF コレクタ（機器ごと・プラグイン式）
│   ├── starlink.py     Starlink Mini: Dishy gRPC (192.168.100.1:9200)
│   ├── kymeta.py       Kymeta u8/Osprey: HTTPS WebGUI スクレイプ
│   └── intellian.py    OneWeb Intellian: LUI スクレイプ / SNMP
├── netqual/            インターネット通信品質（送受 2 台）
│   ├── server.py       受信側エージェント（TCP スループット + UDP エコー）
│   └── client.py       送信側エージェント（測定主体）
├── handover/           ハンドオーバー予測
│   ├── tle.py          CelesTrak TLE 取得・キャッシュ
│   └── predict.py      SGP4 でサービス衛星と切替時刻を予測
├── webapp/             Flask ダッシュボード（Chart.js）
├── storage.py          SQLite 時系列 + SINR/ネット品質の突き合わせ
├── orchestrator.py     定期実行オーケストレータ
└── __main__.py         CLI (monitor / receiver / sender / predict)
```

各機能は**独立**しており、外部依存(skyfield, grpcurl 等)が無くても本体は起動します
（該当機能のみ無効化してログ出力）。

---

## セットアップ

```bash
pip install -r requirements.txt
# Starlink コレクタを使う場合のみ、OS に grpcurl を導入
#   例) apt install grpcurl / brew install grpcurl
cp config.example.yaml config.yaml   # 環境に合わせて編集
```

`config.yaml` で自局位置・使う機器・測定対向先を設定します（詳細はファイル内コメント）。

---

## 使い方（2 台構成）

インターネット越しに測定するため、**受信側**と**送信側**で役割を分けます。

### ① 受信側 PC（対向サーバ）

```bash
python -m bdp_analyzer receiver --control-port 5301 --udp-port 5302
```
TCP 5301 / UDP 5302 を受信側に到達できるよう NAT/FW を開けてください。

### ② 送信側 / メイン PC（測定 + ダッシュボード）

`config.yaml` の `netqual.role: sender`・`peer_host: <受信側アドレス>` を設定し:

```bash
python -m bdp_analyzer monitor --config config.yaml
# → http://localhost:8080/ でダッシュボード
```

RF コレクタ・ネット品質測定・ハンドオーバー予測が定期実行され、SQLite に蓄積、
ブラウザで対比表示されます。

### 単発ツール

```bash
python -m bdp_analyzer sender <受信側host> --loop      # 測定値を端末に表示
python -m bdp_analyzer predict --config config.yaml    # ハンドオーバー予測を確認
```

---

## ダッシュボード

- **現在の RF 状態**: SINR / 方位角 / 仰角 / 周波数 / 障害物 / 端末遅延
- **SINR vs スループット**、**SINR vs RTT・ジッタ**: 電波品質と通信品質の対比(二軸)
- **パケットロス**の推移
- **接続中と推定される衛星 / 次のハンドオーバー**: Starlink・OneWeb 別
- **ハンドオーバー予測タイムライン**: 過去記録 + 近未来予測

---

## 機器ごとの接続方法・注意点

実機のフィールドマッピングや取得可否は **[docs/devices.md](docs/devices.md)** を参照。
アーキテクチャ詳細は **[docs/architecture.md](docs/architecture.md)**。

---

## テスト

```bash
python -m pytest tests/ -q
```
HW / ネットワーク不要のループバックまで含めて検証します。
