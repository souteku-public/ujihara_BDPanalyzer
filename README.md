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
| 計測の選択（全 LEO を同時接続しない運用） | ダッシュボードの**計測コントロール**で、機器・測定先・予測を個別に ON/OFF（再起動不要・状態は保存） |
| Ether / Wi-Fi の品質チェック | 測定先をラベル付きで複数登録（UI から追加可）。衛星なしの LAN 相手でも同じ品質測定が可能 |
| ネットワーク情報の入力・確認 | UI の**ネットワーク情報**パネル。自局 IP/ホスト名の自動検出、グローバル IP のワンクリック取得、回線種別（DHCP/固定グローバル/CGNAT）・対向 IP・メモを保存 |
| 回線の耐性チェック | **負荷耐性テスト（レートスイープ）**。送出レートを段階的に上げ、実効レート・ロス・**負荷時 RTT**・ジッタの変化から実効容量と飽和挙動を測定 |

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
2. **左上のブランチ選択ボタン（プルダウン）で、本ソフト一式が入っている
   ブランチを選ぶ**（開発中は `claude/leo-satellite-quality-monitor-tzbskn`。
   間違ったブランチのままだと README しか入っていない ZIP が落ちてきます）。
   直接リンク:
   <https://github.com/souteku-public/ujihara_BDPanalyzer/archive/refs/heads/claude/leo-satellite-quality-monitor-tzbskn.zip>
3. 緑色の「**< > Code**」ボタン →「**Download ZIP**」を押す。
4. ダウンロードした ZIP を右クリック →「すべて展開」（Mac はダブルクリック）。
5. 展開したフォルダ（`ujihara_BDPanalyzer` で始まる名前）を、
   デスクトップなど分かりやすい場所に置く。
   **フォルダの中に `requirements.txt` と `config.example.yaml` が見えていれば正解**です
   （`collectors` や `model.py` が直接見えている場合は 1 階層内側に入りすぎです。
   その外側のフォルダを使ってください）。

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
| `netqual:` の `targets:` | **相手の PC** のアドレスとラベル（相手 PC でも同様に、こちらの PC のアドレスを書く）。あとから画面上でも追加できるので、最初は空でも OK |
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

- **計測コントロール**: 何を計測するかを画面上のトグルで選択。
  - RF コレクタ（Starlink / Kymeta / Intellian）を機器ごとに ON/OFF
    （全 LEO を同時接続しない運用に対応。config で `enabled: false` でも登録され、UI から起動可）
  - 通信品質の**測定先を複数登録**（「Starlink経由」「社内LAN」「Wi-Fi」等のラベル付き）。
    画面からホスト IP を入力して追加・削除も可能 →
    **Ether / Wi-Fi だけの品質チェック**は、LAN 内の相手 PC で
    `python -m bdp_analyzer receiver` を起動し、その IP を追加するだけ
  - ハンドオーバー予測・受信サーバも ON/OFF 可
  - 切替は即時反映・**再起動不要**。状態は保存され再起動後も引き継ぎ
    （`config.yaml` は書き換えません）
- **ネットワーク情報**: 自局のホスト名・ローカル IP を自動表示、グローバル IP を
  ボタンで取得。回線種別（DHCP / 固定グローバル / CGNAT）・対向グローバル IP・メモを
  入力・保存できる。※CGNAT 配下（Starlink 標準プラン等）は外部から着信できないため、
  その PC は測定主体とし、対向側を固定グローバル + ポート開放にする
- **負荷耐性テスト（レートスイープ）**: 測定先・方向（上り/下り）・レート段
  （例: 1,2,5,10,20,50 Mbps）・ステップ秒を指定して実行。各段の
  **実効レート / ロス率 / 負荷時 RTT / ジッタ**をグラフと表で表示し、
  「どのレートから損失が立ち上がるか」「負荷で遅延がどれだけ膨らむか
  （バッファブロート）」が分かる。結果は DB にも保存。
  ※実運用トラフィックに影響し、衛星回線ではデータ量を消費するため実施タイミングに注意
- **現在の RF 状態**: SINR / 方位角 / 仰角 / 周波数 / 障害物 / 端末遅延
- **SINR vs スループット**、**SINR vs RTT・ジッタ**: 電波品質と通信品質の対比(二軸)
- **パケットロス**の推移
- **接続中と推定される衛星 / 次のハンドオーバー**: Starlink・OneWeb 別
- **ハンドオーバー予測タイムライン**: 過去記録 + 近未来予測

---

## 時間解像度と設定範囲

記録の時間解像度は測定の種類ごとに UI（計測コントロールの「間隔」欄と
「測定設定」カード）から変更できます。設定範囲・既定値のメモは UI 上にも
表示されます。

| 項目 | 既定値 | 設定範囲 | 備考 |
|---|---|---|---|
| RF コレクタ実行間隔 | Starlink 5 秒 / 他 10 秒 | 1〜3600 秒 | **Starlink は `get_history` により実行間隔に関わらず 1 秒解像度の履歴を差分回収**（ソース名 `<名前>:1s`）。Kymeta/Intellian は端末 Web への負荷を考え 5 秒以上推奨 |
| 通信品質 測定間隔 | 30 秒 | 5〜3600 秒 | 1 サイクルに「スループット測定時間×2 + プローブ約 4 秒」かかるため、それ未満に縮めても詰まるだけ |
| スループット測定時間 | 5 秒 | 1〜30 秒 | 長いほど正確・回線占有も増 |
| UDP プローブ数 / 間隔 | 200 個 / 20ms | 10〜1000 個 / 5〜100ms | ロス率の分解能を決める（200 個 → 0.5% 刻み） |
| **常時 RTT モニタ** | OFF（プローブ 200ms・集計 1 秒） | プローブ 50〜1000ms・集計 1〜60 秒 | **1 秒解像度**で RTT/ジッタ/ロスを連続記録。帯域負荷は約 50kbps で常時 ON でも実害なし。瞬断・ハンドオーバー時の挙動把握に有効 |
| ハンドオーバー予測間隔 | 30 秒 | 10〜3600 秒 | 予測の時間刻みは `step_s`（config、既定 15 秒） |

UI で変更した間隔・設定は保存され、再起動後も引き継がれます。

---

## データの保存形式

取得データは **SQLite 1 ファイル**（`data/bdp.sqlite`）に蓄積されます。
**どのネットワーク・どの機器で測っても列構成は同じ**で、回線や機器の違いは
`session`（測定先ラベル）・`source`/`kind`（機器名/種別）の値で区別します。

- **CSV でダウンロード**: ダッシュボード右上の「CSV: RF / 通信品質 / 負荷試験 / HO」。
  Excel でそのまま開けます（ISO 形式の時刻列付き・文字化け対策済み）
- **JSON API**: `/api/rf` `/api/net` `/api/loadtests` `/api/handovers` など
- **SQLite 直接**: pandas や DB Browser for SQLite で分析可能

列の意味・単位の一覧は **[docs/data_format.md](docs/data_format.md)** を参照。

---

## 機器のつなぎ方（測定 PC をどこに接続するか）

### Starlink Mini の場合

**基本は「Mini の回線に PC をつなぐだけ」で OK です。**
Mini はルータ一体型なので、開通済みの Mini の Wi-Fi（または本体の LAN ポート）に
測定 PC を接続すれば、インターネット側の品質測定はそのまま、アンテナ情報も
`192.168.100.1` 経由で拾えます。

手順:
1. Mini を通常どおり開通し、測定 PC を **Mini の Wi-Fi か LAN ポートに接続**する。
2. アンテナに届いているか確認: 黒い画面で `ping 192.168.100.1` →応答があれば OK。
3. **`grpcurl` を PC に導入**する（アンテナ情報の取得にこれだけ必要です）:
   - Windows: <https://github.com/fullstorydev/grpcurl/releases> から
     `grpcurl_x.x.x_windows_x86_64.zip` を取得 → 展開した `grpcurl.exe` を
     本ソフトのフォルダに置く（か PATH の通った場所へ）。フォルダに置いた場合は
     `config.yaml` の starlink 項目に `grpcurl: ".\\grpcurl.exe"` を追記。
   - Mac: `brew install grpcurl`
4. `config.yaml` の `starlink-mini-1` を `enabled: true` にするか、
   ダッシュボードの計測コントロールでトグルを ON。

注意:
- **自前ルータを Mini の下に挟む場合**や**バイパスモード**では、PC から
  `192.168.100.1` に届くようスタティックルートが必要になることがあります。
  まずは PC を Mini に直接つなぐ構成が確実です。
- Starlink の標準プランは **CGNAT** のため外部から着信できません。ネット品質測定は
  この PC を測定主体（相手に向かって測る側）にし、**対向 PC はグローバル IP 側**に
  置いてください（上り/下りとも測定主体側から測れるので機能上の不足はありません）。
- Starlink は**使用周波数と数値 SINR を公開していません**。取得できる項目は
  向き（方位/仰角）・SNR 良否・スループット・遅延・障害物率などです
  （詳細は [docs/devices.md](docs/devices.md)）。

### OneWeb（Kymeta / Intellian）の場合

データ回線と**端末の管理ネットワークが分かれている構成がある**点が Starlink と
違います。RF 情報の取得は「端末の管理画面（LUI/WebGUI）に PC から届くこと」が条件です。

手順:
1. 測定 PC を端末（CNX 等）の LAN に接続し、インターネットに出られることを確認。
2. **ブラウザで `https://<端末の管理IP>` を開き、管理画面が表示されるか確認**する。
   - 表示されればその IP を `config.yaml` の kymeta / intellian 項目に設定。
   - 表示されない場合、管理用ポート（admin Ethernet）が別になっている機器構成です。
     施工資料・機器設定書で管理 IP を確認し、PC を管理側にも接続してください
     （RF 情報とネット品質測定の両方を行う場合、PC が両ネットワークに
     到達できる必要があります。有線+無線の 2 系統接続でも可）。
3. 管理画面のログイン情報（ユーザ/パスワード）を config に設定。
4. Kymeta / Intellian は機種・ファームで画面構成が異なるため、
   **どの表示項目をどのメトリクスとして拾うか（field_map / SNMP OID）を
   実機に合わせて一度設定**します。手順は [docs/devices.md](docs/devices.md) を参照。
   管理画面のスクリーンショットや API 応答例があれば、マッピングの作成を支援できます。

補足:
- OneWeb 系サービスは**グローバル IP が払い出される構成が多く**、その場合この PC は
  受信側（対向から測定される側）としても使えます。OS のファイアウォールで
  TCP 5301 / UDP 5302 の受信を許可してください。
- Intellian は公開 API が無いため、SNMP が使えない機体では LUI スクレイプ設定が
  必要です。取れない項目（周波数等）はハンドオーバー予測（TLE）で補完されます。

---

## 機器ごとの接続方法・注意点

実機のフィールドマッピングや取得可否は **[docs/devices.md](docs/devices.md)** を参照。
受信側に使う回線の種別(固定/動的/MAP-E/DS-Lite/CGNAT)の調べ方と対処は
**[docs/network_check.md](docs/network_check.md)**。
アーキテクチャ詳細は **[docs/architecture.md](docs/architecture.md)**。

---

## テスト

```bash
python -m pytest tests/ -q
```
HW / ネットワーク不要のループバックまで含めて検証します。
