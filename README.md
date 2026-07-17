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
| スループット・ジッタ・RTT の測定 | **netqual**（送信/受信 2 台構成の自己完結測定。**並列ストリーム / スロースタート除外 / 順序逆転**など iperf 相当項目を網羅。`engine: iperf3` で**スループットのみ iperf3 に委譲**も可） |
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

### オフライン / クローズド網での動作

- **受信専用（`receiver` / role=receiver）はインターネット接続不要**。送信側からの
  接続に応答するだけで、外向き通信は一切行いません。
- ダッシュボード（`monitor`）もオフラインで起動・記録・グラフ表示できます
  （Chart.js は**ローカル同梱**、CSV ビューアも外部 CDN 不使用）。ただし
  **ハンドオーバー予測（TLE 取得）と天気は外部 API が必要**で、ネットが無い場合は
  自動的に無効化して他機能は継続します（TLE はキャッシュがあれば利用）。

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

- **表示モード（送信側 / 受信側）**: ヘッダで役割に応じたビューに切替。
  **衛星の状況・スカイプロット・ハンドオーバー予測・天気・SINR 実測 vs 理論は
  どちらのモードでも表示**され、それ以外は役割に応じて整理されます:
  - 送信（測定）側: RF 状態、SINR vs スループット/RTT/ロス、負荷・耐久テスト、測定設定
  - 受信側: **受信サーバ状態**（稼働・ポート・許可送信元・直近の測定要求ログ・拒否件数）
  - 選択は URL（`?mode=sender` / `?mode=receiver`）に保持されるため、
    拠点ごとにブックマークすれば実質「役割別アプリ」として運用できます。
    既定は config の `netqual.role` から自動判定（receiver なら受信側ビュー）
- **検証対象セレクタ**: ヘッダで「両方 / Starlink / OneWeb」を切替。RF 状態・SINR
  グラフ・実測 vs 理論・衛星/ハンドオーバー表示が選択したコンステレーションだけに
  絞られ、画面上部のバッジで「どちらの検証中か」が常に分かります。
  選択は **URL（`?view=starlink` / `?view=oneweb`）に保持**されるため、
  **同じアプリを複数のブラウザ窓で開き、窓ごとに別のアンテナを表示**できます
  （例: 干渉実験で Kymeta と Starlink が同一ネットワークにいる場合、
  `http://localhost:8080/?view=starlink` と `http://localhost:8080/?view=oneweb`
  を並べる。収集は 1 プロセスで両方同時に行われ、表示だけが分かれます）。
  ハンドオーバー予測はもともと **Starlink・OneWeb の両方**を対象にしています。
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
- **負荷・耐久テスト**: 2 種類を選択可能。結果は DB にも保存。
  - **レートスイープ**: レート段（例: 1,2,5,10,20,50 Mbps）を順に送出し、
    各段の**実効レート / ロス率 / 負荷時 RTT / ジッタ**から
    「どのレートから損失が立ち上がるか」＝実効容量と飽和挙動を測る
  - **耐久（一定レート）**: 例「**30 Mbps を 30 秒間**」のように一定レートを
    流し続け、10 秒刻みで実効レート・ロス・RTT の**時間推移**を記録
    （最長 1 時間。ハンドオーバーをまたいだ持続品質の確認に有効）
  ※実運用トラフィックに影響し、衛星回線ではデータ量を消費するため実施タイミングに注意
  （30Mbps×60 秒 ≒ 225MB）
- **現在の RF 状態**: SINR / 方位角 / 仰角 / 周波数 / 障害物 / 端末遅延
- **SINR vs スループット**、**SINR vs RTT・ジッタ**: 電波品質と通信品質の対比(二軸)
- **パケットロス**の推移
- **接続中と推定される衛星 / 次のハンドオーバー**: Starlink・OneWeb 別
  （次の切替先の衛星名と残り秒数を表示）
- **上空の衛星（スカイプロット）**: 上空の可視衛星を方位-仰角の極座標
  （中心=天頂、外周=地平線、上=北）に描画。**接続中の衛星は白リング、
  次に切替わる予定の衛星は橙点線リング + 切替方向の矢印**で強調表示。
  アンテナ実測の向き（＋マーク）も重ねて表示され、指向の健全性確認にも使える。
  検証対象セレクタに連動して Starlink / OneWeb を切替可能
- **ハンドオーバー予測タイムライン**: 過去記録 + 近未来予測

---

## SINR 理論値との対比（アンテナ間干渉の実験向け）

Starlink / OneWeb のアンテナ間距離を変えて干渉量を評価する実験のために、
**理論 SINR の推定値を実測と並べて描画・記録**できます。

- ハンドオーバー予測が算出する「接続中と推定される衛星の**距離・仰角**」から、
  簡易リンクバジェット（EIRP + G/T − 自由空間損失 − 大気損失 − 帯域）で
  理論 SINR を 30 秒ごとに計算
- `source="model:starlink"` / `"model:oneweb"` の行として**実測と同じテーブル・
  同じ CSV に記録**される（`kind="model"` で区別）
- ダッシュボードの「**SINR 実測 vs 理論値（干渉評価）**」グラフに実線（実測）と
  破線（理論）で重ね描きされ、**Δ（実測 − 理論）**を数値表示。
  この Δ がアンテナ間干渉・降雨・遮蔽などによる劣化の推定量になります
- **実験マーカー**: 「アンテナ間距離 1.5m に変更」などをワンクリックで時刻付き記録。
  グラフ下に一覧表示され、CSV（`/export/markers.csv`）にも出力されるため、
  条件変更点とデータを突き合わせて分析できます

### 上空の天気の取り込み（降雨減衰の自動反映）

- **気象（雨雲）モニタ**が [Open-Meteo](https://open-meteo.com)（無料・API キー不要）から
  自局上空の**降水強度 [mm/h]・雲量 [%]・気温 [℃]・湿度 [%]・風速 [m/s]・天気コード**を
  定期取得（既定 5 分間隔）し、`weather_samples` テーブルに**数値として記録**
  （CSV: `/export/weather.csv`）
- 降水強度は **ITU-R P.838 ベースの降雨減衰**（γ=k·R^α × 雨域斜め路長 × セル補正）
  として**理論 SINR に自動反映**されます（`sinr_model.use_weather: false` で無効化可）
- ダッシュボード:
  - 「上空の天気」カードに現在値 KPI（降水強度・**雨減衰の参考値（Ku, 仰角45°換算）**・雲量など）
  - 「SINR 実測 vs 理論値」グラフに**降水強度を第 2 軸で重ね描き** — 雨と SINR 低下の
    相関が一目で分かり、干渉起因（Δの変化が降水と無相関）との切り分けができます

> ⚠️ 使い方の前提: 衛星 EIRP・端末 G/T は事業者非公開のため、既定値は代表値です。
> **絶対値ではなく傾向を見る道具**として使い、まず晴天・アンテナ十分離隔の基準状態で
> 実測と理論が一致するよう `config.yaml` の `sinr_model` の `eirp_dbw`（または
> `losses_db`）を較正してください。較正後の Δ の変化が干渉の増減として読めます。
> なお Starlink は数値 SINR を出さないため、実測側は Kymeta/Intellian の SINR、
> Starlink は SNR 良否フラグ・スループット低下との突き合わせが中心になります。

---

## マルチ NIC 同時測定（USB-Ether で複数回線を 1 台の PC に接続）

1 台の PC に内蔵 Ether + USB-Ether アダプタで **Starlink / OneWeb / 社内 LAN などを
同時接続し、回線ごとに並行測定**できます。

### 使い方

1. 各回線を各アダプタに接続（それぞれ DHCP で IP を取得）。
2. 測定先を追加するとき「**送信元 IP**」欄に、その回線につながっている
   **アダプタの自分側 IP** を指定（ネットワーク情報パネルのアダプタ一覧から選べます。
   `pip install psutil` でアダプタ名付き表示になります）。
3. これで測定先ごとに送信元アダプタが固定され、
   「Starlink 経由」「OneWeb 経由」「有線 LAN」を**同じ対向 PC に対して同時に**
   測定・比較できます。グラフはラベル別に色分けされます。

config で書く場合は各 target に `bind_ip:` を指定します（config.example.yaml 参照）。

### IP アドレス数などの制約について

- **PC が持てる IP 数は実質制約になりません。** アダプタ 1 本につき IPv4 が 1 つ
  付くだけで、Windows / Linux とも USB-Ether 数本程度は全く問題ありません。
- **本当の注意点は「どのアダプタから出ていくか」（ルーティング）**です。
  複数アダプタが各々デフォルトゲートウェイを持つと、通常の通信は
  メトリック最小の 1 本からしか出ません。本ソフトは測定ソケットの送信元を
  `bind_ip` で固定することでこれを解決しています
  （Windows Vista 以降は strong host model が既定のため、送信元 IP を固定すれば
  そのアダプタから送信されます）。
- **RF コレクタ（管理 IP へのアクセス）は宛先ルーティング依存**です。
  例えば Starlink の `192.168.100.1` は、マルチ NIC 環境では意図しないアダプタへ
  向かうことがあるため、必要に応じてホストルートを追加してください:
  ```bat
  :: Windows の例: 192.168.100.1 宛を Starlink 側アダプタのゲートウェイへ
  route add 192.168.100.1 mask 255.255.255.255 <Starlink側GW> -p
  ```
  Kymeta / Intellian の管理 IP も同様（各アダプタのサブネット内なら自動で
  on-link 経路が付くので通常は不要）。

### 同時測定の注意

- **USB アダプタは USB3 対応の GbE を推奨**（USB2 は実効 ~300Mbps で頭打ちになり、
  回線ではなくアダプタを測ってしまう）。
- スループット測定が複数回線で同時に走ると PC の CPU と対向側回線を奪い合うため、
  **負荷耐性テストは 1 回線ずつ**実施してください（通常測定の周期実行は
  開始タイミングが自然にずれるため問題になりにくい）。
- 対向（受信側）は 1 台で共用できます。全回線が同じ受信サーバへ測ることで、
  受信側条件を揃えた公平な回線間比較になります。

---

## 単独端末でインターネット速度を測る（受信側 PC 不要）

対向 PC を用意できない場合でも、**測定端末 1 台を DHCP 回線（Starlink 等）に繋ぐだけ**で、
公開エンドポイント（既定 Cloudflare、アカウント不要）に対して「PC → インターネット」の
スループットを**指定時間**測れます。衛星区間がボトルネックになるのが通常なので、
**衛星回線の実効速度の目安**になります（アウトバウンド HTTPS のみ・追加インストール不要）。

```bash
# 例: 30 秒測定を 60 秒ごとに繰り返し、CSV に記録
python -m bdp_analyzer inettest --seconds 30 --streams 4 --loop --interval 60 --csv sat_speed.csv
```
- `--streams 4`: 高 BDP 対策の並列接続（単一接続では衛星回線を埋めきれないため）
- `--csv` でローカル保存 / `--post http://<受信IP>:8080/api/ingest` で中央記録も可
- `monitor` に組み込む場合は `netqual.targets` に `mode: internet` の測定先を追加
  （`config.example.yaml` 参照）。ダッシュボード/CSV に他の測定と同じ形で載る

> ⚠️ これは PC→CDN の経路全体（衛星区間＋地上バックホール＋CDN）を測る一般的な
> スピードテスト方式です。衛星区間のみを厳密に切り出すものではありませんが、
> 通常は衛星区間がボトルネックのため良い近似になります。厳密な区間分離が必要なら
> 対向 PC（固定 IP 受信側）方式を併用してください。

---

## 送信 PC に何もインストールできない場合（社用 PC など）

送信（測定）側は **Python 標準ライブラリだけで動作**します。追加の pip パッケージ
（Flask/requests 等）も grpcurl も iperf3 も不要で、**DHCP のままで問題ありません**
（送信側はアウトバウンド通信のみ・固定 IP もポート開放も不要）。受信側だけを
固定 IP + ポート開放にしてください。

- **Python すら入れられない**: 公式の「embeddable package（zip 版）」を展開すれば
  インストーラ不要・管理者権限なしで `python.exe` を実行できます（USB からでも可）。
- **測定結果の扱い（インストール不要のまま）**:
  ```bash
  # ① ローカル CSV に保存（CSV ビューアで後から可視化）
  python -m bdp_analyzer sender <受信側IP> --loop --csv result.csv
  # ② 受信側へ送って中央のダッシュボード/DB に記録
  python -m bdp_analyzer sender <受信側IP> --loop --post http://<受信側IP>:8080/api/ingest --token <任意>
  ```
  `--csv` は標準ライブラリの csv、`--post` は urllib のみを使うため、送信 PC に
  何も追加せず動きます。受信側 `config.yaml` の `netqual.ingest_token` を設定すると
  `--token` 一致を要求します（未設定なら不要）。
- ダッシュボードや衛星予測・天気など「見る」機能を使うのは、インストール可能な
  受信側 PC 側に任せる構成が確実です。

---

## スループット測定エンジン（内蔵 / iperf3）

スループットは 2 つのエンジンから選べます（`config.yaml` の `netqual.engine`）。
RTT・ジッタ・ロス・順序逆転・SINR/天気との相関・記録は **engine に関わらず本体が担当**します。

| engine | 精度・速度 | 前提 | 用途 |
|---|---|---|---|
| `builtin`（既定） | LEO 実速度域（〜数百Mbps）では iperf3 と誤差レベルで一致。実効上限は環境依存で数 Gbps | 追加不要 | 通常はこれで十分 |
| `iperf3` | C 実装で高速リンクでも高精度・高スループット | 対向に iperf3 サーバ（本アプリ受信側が `iperf_port` 指定で自動起動）。無ければ内蔵に自動フォールバック | 1Gbps 超や iperf 準拠の数値が必要な場合 |

実測比較（同一ループバック、参考値）: TCP スループットは iperf3 の方が内蔵より
高速に出る（例: 単一ストリームで iperf3 ≈ 9.9 Gbps / 内蔵 ≈ 7.3 Gbps、
4 並列で iperf3 ≈ 52 Gbps / 内蔵 ≈ 8 Gbps=Python の GIL で頭打ち）。一方
UDP の既知オファーレート 50Mbit に対する実効レート・ロス・ジッタは両者ほぼ同一で、
**LEO の実速度域では内蔵で精度上の問題はありません**。

使い方（iperf3 委譲）:
```yaml
netqual: { engine: iperf3, iperf_port: 5201 }
```
```bash
# 受信側 (単体起動時): iperf3 サーバも一緒に上げる
python -m bdp_analyzer receiver --iperf-port 5201
# 送信側 CLI で試す
python -m bdp_analyzer sender <host> --engine iperf3 --streams 4
```

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
受信拠点ルータ(富士通 Si-R G210 / NURO Biz)のセキュア設定は
**[docs/router_sir-g210.md](docs/router_sir-g210.md)**
(あわせて `netqual.allowed_sources` で**アプリ側の送信元 IP 制限**も設定推奨)。
アーキテクチャ詳細は **[docs/architecture.md](docs/architecture.md)**。

---

## テスト

```bash
python -m pytest tests/ -q
```
HW / ネットワーク不要のループバックまで含めて検証します。
