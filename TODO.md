# TODO

BDP Analyzer の残作業・実機導入時の対応事項。チェックを入れながら進めてください。
（機能提案の一覧は `docs/proposals.md`、機器接続の詳細は `docs/devices.md`）

## ✅ 実装済み（基盤）
- [x] RF コレクタ基盤（プラグイン式）＋ Starlink(gRPC) / Kymeta(WebGUI) / Intellian(SNMP・LUI)
- [x] ネット品質測定 sender / receiver（TCP スループット + UDP RTT/ジッタ/ロス、時刻同期不要）
- [x] ハンドオーバー予測（CelesTrak TLE + SGP4、サービス衛星推定・切替時刻先読み）
- [x] SQLite 時系列 + SINR とネット品質の時間窓突き合わせ
- [x] Flask + Chart.js ダッシュボード（SINR vs スループット/RTT/ジッタ 対比）
- [x] CLI（monitor / receiver / sender / predict）、pytest 一式、docs
- [x] UI 計測コントロール（機器・測定先・予測を画面上で ON/OFF、再起動不要・状態保存）
- [x] 測定先の複数登録＋ラベル別グラフ表示（Ether / Wi-Fi 品質チェック対応、UI から追加/削除）
- [x] ネットワーク情報パネル（自局 IP 自動検出・グローバル IP 取得・回線種別/対向 IP の入力保存）
- [x] 負荷耐性テスト＝レートスイープ（実効レート・ロス・負荷時 RTT・ジッタ、上り/下り、UI 実行）
- [x] マルチ NIC 同時測定（測定先ごとの送信元アダプタ固定 `bind_ip`、UI から指定可・アダプタ一覧表示）
- [x] SINR 理論値の推定と実測対比（リンクバジェット、model:* 行として記録、Δ表示、実験マーカー）
- [x] 気象（雨雲）モニタ（Open-Meteo から降水・雲量等を定量記録、ITU-R P.838 降雨減衰を理論 SINR に自動反映）
- [x] 表示モード（送信側/受信側ビュー、URL 保持、受信サーバ状態カード）
- [x] 耐久テスト（一定レート×一定時間、例 30Mbps×30 秒、チャンク刻みで時系列記録）
- [x] iperf 相当項目の拡充（並列ストリーム -P / スロースタート除外 --omit / UDP 順序逆転）
      ※ TCP 再送数(Retr)は Linux 専用(TCP_INFO)で Windows 送信側では取得不可のため非対応
- [x] iperf3 バックエンド（engine: iperf3 でスループットのみ委譲、受信側で iperf3 サーバ自動起動、
- [x] インストール制限 PC 対応（送信 CLI は標準ライブラリのみ・DHCP 可。--csv ローカル保存 / --post で受信側 /api/ingest へ中央記録、portable Python 案内）
      不在時は内蔵にフォールバック。RTT/ジッタ/ロスは常に内蔵。実測: iperf3≈9.9G / 内蔵≈7.3G@単一TCP）
- [x] Starlink get_history 対応（1 秒解像度履歴の差分回収、ソース名 `<name>:1s`）
- [x] 常時 RTT モニタ（1 秒集計で RTT/ジッタ/ロスを連続記録、direction='rtt'）
- [x] 時間解像度の UI 設定（各ジョブの実行間隔＋測定パラメータ、範囲・既定値メモ付き）

## 🔧 実機導入時にやること（設定・確認）
- [ ] `config.example.yaml` を `config.yaml` にコピーし、自局の緯度経度・標高・仰角マスクを設定
- [ ] **Starlink**: 動作 PC から `192.168.100.1:9200` への到達性確保、OS に `grpcurl` 導入、`predict`/`monitor` で取得確認
- [ ] **Kymeta**: WebGUI を開発者ツールで調査し `endpoints.status_json` / `field_map` を実機に合わせて確定
      （JSON API が無ければ HTML 用の正規表現マッピングに切替）
- [ ] **Intellian**: SNMP 可否を確認。可なら MIB から OID を特定、不可なら LUI の HTML/JSON をスクレイプ設定
- [ ] receiver 側 PC で TCP(5301)/UDP(5302) をインターネットから到達可能に（NAT/FW ポート開放）
- [ ] TLE 取得（CelesTrak）へのアウトバウンド HTTPS 到達性を確認（ダーク環境ならキャッシュ運用）
- [ ] 実測に基づき netqual の測定間隔・スループット時間・UDP プローブ数を回線負荷と相談して調整

## 📌 未実装（提案から着手候補・優先度順）
- ~~ランデブーモード（UDP 同時オープン）~~ → **対象外と決定**（両側 DHCP/着信不可の
  構成は運用上考慮しない。依頼元確認 2026-07。経緯は docs/network_check.md 参照）
- [ ] ハンドオーバー予測時刻の前後で RTT/ロス/ジッタ変化を自動集計する相関分析
- [ ] 片道遅延(OWD)・上り/下り個別ジッタ（receiver を NTP/PTP 同期、`owd_ms` フィールドは器のみ実装済み）
- [ ] アラート/通知（SINR 低下・ロス急増・障害物率上昇・HO 直前に Slack/メール）
- [x] CSV エクスポート（UI から期間指定ダウンロード、Excel 対応、docs/data_format.md 付き）
- [ ] Prometheus エクスポート（Grafana 連携。`Storage` 差し替えで対応）
- [x] スカイプロット表示（可視衛星・接続中・次の切替先・アンテナ実測の向きを極座標描画）→ 実装済み
- [ ] 降雨減衰と SINR の相関（気象 API 連携）
- [ ] 複数拠点集約 / 測定スケジューラ（時間帯・トラフィック連動で測定強度調整）

## ❓ 要確認（依頼元へ）
- [ ] Kymeta / Intellian の WebGUI 画面・API レスポンス例の共有（field_map / OID をこちらで確定するため）
- [ ] 「使用周波数」の取得要件（Starlink は非公開。Kymeta/Intellian も LUI 表示時のみ取得可）
- [ ] PR 作成の要否（現状はブランチ push のみ）
