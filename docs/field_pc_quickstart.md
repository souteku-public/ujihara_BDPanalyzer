# 出先PC クイックスタート（Python を入れられない / DHCP のみ）

社用 PC など「**Python をインストールできない**」「**DHCP のみ**」「対向(受信側)PC も
用意しない」場合の、いちばん簡単な使い方です。**インストール不要・管理者権限不要・
追加パッケージ不要**で、衛星回線(Starlink 等)の実効速度を測れます。

測定方式は `inettest`（単独端末インターネット速度測定）。公開エンドポイント
(Cloudflare)へ上り/下りを指定時間流し、結果を CSV に保存します。あとで別 PC の
CSV ビューアでグラフ化できます。

---

## 1. ポータブル Python を用意（インストール不要）

1. 別のネットに繋がる PC で <https://www.python.org/downloads/windows/> を開く。
2. 「**Windows embeddable package (64-bit)**」の ZIP をダウンロード
   （インストーラではなく zip 版。展開するだけで動き、管理者権限不要）。
3. ZIP を展開 → 中に `python.exe` が入ったフォルダができる。

## 2. 本ソフトを同じフォルダに置く

1. 本リポジトリの最新 ZIP を展開（`bdp_analyzer` フォルダが入っている版）。
2. その中の **`bdp_analyzer` フォルダごと、python.exe と同じフォルダにコピー**する。
   ※ 同じ場所に置くのがコツ（embeddable Python は同フォルダを自動で探すため）。
3. 同じフォルダに、下記の `run_field_test.bat`（リポジトリ同梱)も置く。

フォルダの中身の例:
```
C:\bdp\
  ├── python.exe            ← ポータブル Python
  ├── python311.zip 等      ← （embeddable 付属ファイル）
  ├── bdp_analyzer\         ← 本ソフト本体
  └── run_field_test.bat    ← ダブルクリック用
```

USB メモリに入れて社用 PC で動かすことも可能です。

## 3. 測定する

`run_field_test.bat` を**ダブルクリック**するだけ。既定で「上り/下り各 30 秒を
60 秒ごとに繰り返し、`sat_speed.csv` に記録」します。止めるときはウィンドウで
`Ctrl + C`。

コマンドで直接実行する場合（`python.exe` のあるフォルダで）:
```bat
python.exe -m bdp_analyzer inettest --seconds 30 --streams 4 --loop --interval 60 --csv sat_speed.csv
```
- `--streams 4`: 並列接続（衛星は単一接続だと速度を出しきれないため）
- `--csv sat_speed.csv`: 結果を CSV 保存
- 1 回だけ測るなら `--loop` を外す

## 4. 結果を見る

`sat_speed.csv` を、別 PC（インストール可能な方）の **CSV ビューア**にドラッグ&
ドロップするとグラフ化されます:
- アプリ起動中なら `http://localhost:8080/viewer`
- もしくは `bdp_analyzer/webapp/templates/csv_viewer.html` をブラウザで直接開く

（任意）本社に受信側があるなら、CSV ではなく本社へ直接送って中央記録もできます:
```bat
python.exe -m bdp_analyzer inettest --seconds 30 --loop --post http://<本社IP>:8080/api/ingest
```

---

## 注意・限界

- `inettest` は「PC → 公開 CDN」の経路全体（衛星区間＋地上）を測る近似です。
  通常は衛星区間がボトルネックなので実効速度の目安になります。
- ポータブル Python すら実行できない超厳格な PC（実行ファイルを一切起動できない
  ポリシー等）では動きません。その場合は別の実行可能な PC を同じ回線に繋いで測るか、
  端末自身の統計（Starlink アプリ等）をご利用ください。
- DHCP で問題ありません（`inettest` はアウトバウンド HTTPS のみ・着信不要）。
