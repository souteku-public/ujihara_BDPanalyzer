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
| 送受 2 台での測定 | **送受兼用の 1 ソフト**（`role: both`）で双方向測定。役割分離（`sender`/`receiver`）も可 |

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

> 💡 **コマンド操作に慣れていない方は、この下の
> 「[はじめての方向け: ダウンロードから起動まで](#はじめての方向け-ダウンロードから起動まで)」
> を上から順に進めてください。**

慣れている方向けの最短手順:

```bash
pip install -r requirements.txt
# Starlink コレクタを使う場合のみ、OS に grpcurl を導入
#   例) apt install grpcurl / brew install grpcurl
cp config.example.yaml config.yaml   # 環境に合わせて編集
```

`config.yaml` で自局位置・使う機器・測定対向先を設定します（詳細はファイル内コメント）。

---

## はじめての方向け: ダウンロードから起動まで

コマンド操作が初めてでも、以下を**上から順に**実施すれば動かせます。
測定に使う **2 台の PC それぞれ**で同じ手順を行ってください。

### STEP 1. Python を入れる（既に入っていれば飛ばす）

本ソフトはプログラミング言語「Python(パイソン)」の上で動きます。

- **Windows の場合**
  1. <https://www.python.org/downloads/> を開き、黄色い「Download Python 3.x.x」ボタンを押す。
  2. ダウンロードしたファイルをダブルクリック。
  3. 最初の画面で **必ず「Add python.exe to PATH」にチェック**を入れてから「Install Now」。
     （これを忘れると後のコマンドが動きません。忘れたら入れ直せば OK）
- **Mac の場合**
  同じページから macOS 用インストーラをダウンロードし、ダブルクリックして進めるだけです。

**確認方法**: 後述の「黒い画面」で `python --version` と打って Enter。
`Python 3.10` 以上の番号が出れば成功です（`3.10`〜`3.12` あたりを推奨）。

### STEP 2. 「黒い画面」（コマンド入力画面）を開く

- **Windows**: スタートメニューで「**cmd**」と検索 →「コマンドプロンプト」を開く。
- **Mac**: Launchpad で「**ターミナル**」と検索して開く。

以降の `コードの枠` に書かれた文字は、この画面に**1 行ずつコピー&貼り付けして
Enter** してください。

### STEP 3. 本ソフトをダウンロードする

**方法 A: ZIP でダウンロード（いちばん簡単・おすすめ）**
1. ブラウザで本リポジトリの GitHub ページを開く。
2. 緑色の「**< > Code**」ボタン →「**Download ZIP**」を押す。
3. ダウンロードした ZIP を右クリック →「すべて展開」（Mac はダブルクリック）。
4. 展開したフォルダ（`ujihara_BDPanalyzer` で始まる名前）を、
   デスクトップなど分かりやすい場所に置く。

**方法 B: git を使う（分かる方のみ）**
```bash
git clone <このリポジトリの URL>
```

### STEP 4. ダウンロードしたフォルダに移動する

黒い画面で、フォルダの場所まで「移動」します。デスクトップに置いた場合の例:

```bash
# Windows の例（ユーザー名は自分のものに読み替え）
cd C:\Users\<ユーザー名>\Desktop\ujihara_BDPanalyzer-main

# Mac の例
cd ~/Desktop/ujihara_BDPanalyzer-main
```

> 💡 迷ったら: `cd ` (cd+半角スペース) まで打った後、フォルダを黒い画面に
> **ドラッグ&ドロップ**すると場所が自動入力されます。そのまま Enter。

### STEP 5. 必要な部品（ライブラリ）を自動で入れる

```bash
pip install -r requirements.txt
```

インターネットから必要な部品が自動ダウンロードされます（数分）。
`Successfully installed ...` などと出れば OK です。

> ⚠️ `pip` が無いと言われたら `python -m pip install -r requirements.txt` を試す。

### STEP 6. 設定ファイルを作る

1. フォルダ内の `config.example.yaml` をコピーして、同じ場所に
   `config.yaml` という名前で貼り付ける（メモ帳等で開いて「名前を付けて保存」でも可）。
2. `config.yaml` をメモ帳などで開き、最低限つぎの 3 か所を書き換える:

| 場所 | 書き換える内容 |
|---|---|
| `ground_station:` の `latitude` / `longitude` | 測定場所のおおよその緯度・経度（Google マップで右クリックすると表示されます） |
| `netqual:` の `peer_host` | **相手の PC** のアドレス（IP アドレスなど。相手 PC でも同様に、こちらの PC のアドレスを書く） |
| `collectors:` | 使うアンテナ（Starlink / Kymeta / Intellian）の項目だけ `enabled: true` にして、IP アドレスを実機のものにする |

書き換えたら**上書き保存**してください。ほかの項目は最初はそのままで動きます。

### STEP 7. 起動する

```bash
python -m bdp_analyzer monitor --config config.yaml
```

起動メッセージが流れたら、**そのまま画面は閉じずに**、ブラウザ（Edge や Chrome）で

> **http://localhost:8080/**

を開いてください。ダッシュボード（グラフ画面）が表示されれば成功です 🎉
（測定は 30 秒周期などで動くため、グラフが埋まるまで数分お待ちください）

**終了するとき**: 黒い画面で `Ctrl + C`（Mac も control + C）を押します。

### うまくいかないとき（よくあるつまずき）

| 症状 | 対処 |
|---|---|
| `python` は認識されていません と出る | STEP 1 の「Add python.exe to PATH」を忘れています。Python を入れ直す（チェックを忘れずに） |
| `No such file or directory` / ファイルが見つからない | STEP 4 の「フォルダへの移動」ができていません。ドラッグ&ドロップの小技を使う |
| `pip` が無いと言われる | `python -m pip install -r requirements.txt` に読み替える |
| ブラウザで画面が出ない | 黒い画面を閉じていないか確認。アドレスは `http://localhost:8080/`（httpsではない） |
| 相手 PC への測定が失敗する | 相手側でも本ソフトが起動しているか、`peer_host` のアドレスが正しいか、ルータやセキュリティソフトで TCP 5301 / UDP 5302 が許可されているか確認 |
| Starlink の値が取れない | 別途 `grpcurl` の導入が必要です（[docs/devices.md](docs/devices.md) 参照）。ネットワーク管理者に依頼を推奨 |
| Windows のセキュリティ警告が出る | 「アクセスを許可する」を選択（測定用の通信を待ち受けるため） |

---

## 使い方

### 推奨: 送受兼用（1 ソフトを両 PC で実行）

`netqual.role: both`（既定）にすると、**1 プロセスが受信サーバを常駐しつつ、
相手へ上り・下り両方向を能動測定**します。両 PC で同じコマンドを動かし、互いを
`peer_host` に指定すれば対称な双方向測定になります。上り/下りは 1 回の測定で
両方取得します（role に関係なく）。

```bash
# PC-A の config.yaml:  netqual: { role: both, peer_host: "<PC-B の到達先>" }
# PC-B の config.yaml:  netqual: { role: both, peer_host: "<PC-A の到達先>" }
python -m bdp_analyzer monitor --config config.yaml   # 両 PC で実行
# → 各 PC の http://localhost:8080/ でダッシュボード
```

RF コレクタ・ネット品質測定・ハンドオーバー予測が定期実行され、SQLite に蓄積、
ブラウザで対比表示されます。TCP `control_port` / UDP `udp_port` を相手から到達
できるよう NAT/FW を開けてください。

### 役割を分けたい場合（従来どおりでも可）

機能は同じですが、応答専用・測定専用に分けることもできます。
```bash
python -m bdp_analyzer receiver              # 応答専用 (role=receiver 相当)
# もう一方で config の role: sender として monitor、または:
python -m bdp_analyzer sender <相手host> --loop   # 単発/ループ測定を端末表示
```

### 単発ツール

```bash
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
