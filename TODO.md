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
- [ ] ハンドオーバー予測時刻の前後で RTT/ロス/ジッタ変化を自動集計する相関分析
- [ ] 片道遅延(OWD)・上り/下り個別ジッタ（receiver を NTP/PTP 同期、`owd_ms` フィールドは器のみ実装済み）
- [ ] アラート/通知（SINR 低下・ロス急増・障害物率上昇・HO 直前に Slack/メール）
- [ ] CSV / Prometheus エクスポート（Grafana 連携。`Storage` 差し替えで対応）
- [ ] スカイプロット表示（可視衛星の方位-仰角極座標に RF 実測の向きを重ねる）
- [ ] 降雨減衰と SINR の相関（気象 API 連携）
- [ ] 複数拠点集約 / 測定スケジューラ（時間帯・トラフィック連動で測定強度調整）

## ❓ 要確認（依頼元へ）
- [ ] Kymeta / Intellian の WebGUI 画面・API レスポンス例の共有（field_map / OID をこちらで確定するため）
- [ ] 「使用周波数」の取得要件（Starlink は非公開。Kymeta/Intellian も LUI 表示時のみ取得可）
- [ ] PR 作成の要否（現状はブランチ push のみ）
